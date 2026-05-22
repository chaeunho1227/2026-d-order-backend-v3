# PgBouncer 도입 가이드

**날짜**: 2026-05-22
**관련 PR**: #447
**관련 Issue**: #447 (배경: #445 / PR #446 의 ASGI thread connection 누수)

---

## 배경

PR #446 에서 Django 워커를 Daphne 단일 프로세스 → Gunicorn+UvicornWorker 다중 워커로 교체하면서 부수 회귀로 **Django 6.0 + UvicornWorker(ASGI) + `CONN_MAX_AGE>0` 조합에서 thread-local DB connection 이 정리되지 않고 누적**되는 누수가 드러났다. dev 부하 테스트에서 idle connection 이 183개까지 쌓이며 `FATAL: sorry, too many clients already` 발생. 응급 처방으로 `DB_CONN_MAX_AGE=0` 으로 끄고 머지했다.

이 상태는 두 가지 비용이 있다.
1. 매 요청마다 PG connect/disconnect → 트래픽 증가 시 latency/CPU 부담
2. 동시 요청 시 PG max_connections 한계 위험 상존

**정석 해법은 PgBouncer + transaction pooling 도입**이다. Django/Spring 은 PgBouncer 에 connect 하고, PgBouncer 가 자체 풀에서 PG 백엔드 connection 을 트랜잭션 단위로 빌려준다. PG 쪽 활성 connection 수는 워커/스레드와 무관하게 풀 사이즈로 고정된다.

---

## Transaction Pooling 호환성 점검 결과

`session` 모드는 풀링 효과가 작아 `transaction` 모드를 택했다. transaction pooling 은 세션 전역 상태(SET, LISTEN, prepared statement cache, server-side cursor 등)와 비호환인데, **현재 코드는 모두 호환**임을 확인.

### Django 측

| 항목 | 결과 | 근거 |
|---|---|---|
| `CONN_MAX_AGE` | 0 (호환) | `django/project/settings.py` |
| psycopg | 2.9.11 — 명시적 prepare 없음 | `django/requirements.txt` |
| Server-side cursor `.iterator()` | 미사용 | 전역 grep |
| LISTEN/NOTIFY | 미사용 (Redis pub/sub 사용) | `django/core/redis_client.py` |
| `pg_advisory_lock` | 미사용 | 전역 grep |
| Temp table / WITH HOLD cursor | 미사용 | 전역 grep |
| `SET LOCAL` | 트랜잭션 내 사용 — 호환 | `django/table/services.py` (`lock_timeout`, `statement_timeout`) |
| 트랜잭션 밖 `SET`/`SET SESSION` | 미사용 | 전역 grep |
| `@transaction.atomic` / `select_for_update` | 트랜잭션 스코프 — 호환 | `order/`, `cart/`, `coupon/`, `table/` 등 |

### Spring 측

| 항목 | 결과 | 근거 |
|---|---|---|
| HikariCP | max 10, idle 10m, max-life 30m | `application.yml` |
| JDBC server-side prepare | 비활성 (기본 prepareThreshold=5 → 도입 시 0 으로 강제) | application.yml |
| Hibernate batch / scrollable | 비활성 | application.yml |
| 명시적 PreparedStatement 캐싱 | 미사용 (`JdbcTemplate` 1곳 매개변수화 쿼리) | `CartPayableAmountService.java` |
| `@Async` / `@Scheduled` long-lived | 미사용 | 전역 grep |

### 유일한 주의점

**JDBC URL 에 `prepareThreshold=0` 필수.** PG JDBC 기본값 5 → 동일 쿼리 5회 후 server-side prepare 시도. transaction pooling 에서 다른 백엔드 connection 으로 redispatch 되면 `prepared statement does not exist` 오류 발생.

---

## 도입 구성

### pgbouncer 컨테이너 (dev/prod compose 공통)

이미지: `edoburu/pgbouncer:1.23` (경량). 인증은 `auth_query` 로 PG 에서 동적 조회해 userlist.txt 관리 부담 제거.

```yaml
pgbouncer:
  image: edoburu/pgbouncer:1.23
  environment:
    DB_HOST: postgres
    DB_PORT: 5432
    DB_USER: ${POSTGRES_USER}
    DB_PASSWORD: ${POSTGRES_PASSWORD}
    AUTH_TYPE: scram-sha-256
    AUTH_QUERY: "SELECT usename, passwd FROM pg_shadow WHERE usename=$1"
    AUTH_USER: ${POSTGRES_USER}
    AUTH_DBNAME: ${POSTGRES_DB}
    POOL_MODE: transaction
    MAX_CLIENT_CONN: 500
    DEFAULT_POOL_SIZE: 25
    MIN_POOL_SIZE: 10
    RESERVE_POOL_SIZE: 5
    RESERVE_POOL_TIMEOUT: 3
    SERVER_RESET_QUERY: DISCARD ALL
    ADMIN_USERS: ${POSTGRES_USER}
    STATS_USERS: ${POSTGRES_USER}
  expose: ['6432']
  depends_on:
    postgres:
      condition: service_healthy
  healthcheck:
    test: ['CMD-SHELL', 'pg_isready -h localhost -p 6432 -U postgres']
    interval: 10s
    timeout: 5s
    retries: 5
```

### Django 전환

compose 의 `DB_HOST: postgres` → `DB_HOST: pgbouncer`, `DB_PORT: 5432` → `DB_PORT: 6432`. `depends_on` 도 `postgres` → `pgbouncer` 로 교체. settings 는 그대로.

### Spring 전환

`application.yml` dev/prod 모두 datasource URL 변경.

```yaml
datasource:
  url: jdbc:postgresql://${DB_HOST}:6432/${POSTGRES_DB}?prepareThreshold=0
```

compose 의 `DB_HOST` 도 `pgbouncer` 로. Spring 컨테이너 환경변수에 `DB_HOST` 가 없으면 추가.

### PostgreSQL 환원

`max_connections=200` → `max_connections=100` 으로 줄임. PgBouncer 풀 25 + 예비라 PG 쪽 실제 활성은 30~40 수준이므로 100 으로 충분. PG 재시작 필요.

---

## 리소스 영향 (m7i-flex.large, RAM 8GB 기준)

| 항목 | 도입 전 | 도입 후 |
|---|---|---|
| postgres RSS | ~600MB | ~400MB (max_conn 환원 + 실 활성 감소) |
| pgbouncer RSS | — | +5~30MB (idle~peak) |
| 호스트 합계 | ~2.6~2.8GB | **~2.4~2.6GB** (~150MB 절감) |
| pgbouncer CPU | — | 평상시 0%, 부하 시 1~3% |
| 요청당 추가 latency | — | < 0.1ms (동일 docker 네트워크) |

PgBouncer 는 단일 프로세스 async I/O 라 비용이 매우 작고, PG 환원 효과가 더 커서 전체 메모리는 약간 줄어든다.

---

## 검증 절차

### dev 배포 후 단계별

1. 컨테이너 상태
   ```bash
   docker compose -f docker-compose.dev.yml ps | grep -E "pgbouncer|postgres|django|spring"
   ```
   pgbouncer healthy 여부 확인.

2. PgBouncer 로그
   ```bash
   docker logs d-order-pgbouncer-dev | tail -50
   ```
   PG 인증 성공, 풀 생성 메시지 확인.

3. 기능 회귀
   - `/health/django`, `/health/spring` 200
   - 로그인/JWT 흐름
   - 테이블 입장 (`SELECT FOR UPDATE`)
   - 주문 생성/취소
   - WebSocket `/ws/django/booth/tables/` 핸드셰이크 + `enter_table` 수신

4. 부하 테스트 (PR #446 시나리오 재실행)
   ```bash
   k6 run /tmp/k6_enter_spread.js
   k6 run /tmp/k6_enter_same.js
   ```
   기대: 5xx 0%, p95 동등 이하.

5. 부하 중 PG 활성 connection
   ```bash
   docker exec d-order-postgres-dev psql -U postgres -tA \
     -c "SELECT count(*), state FROM pg_stat_activity GROUP BY 2;"
   ```
   기대: 활성 connection 이 30~40 이내, idle accumulation 없음.

6. PgBouncer 풀 통계
   ```bash
   docker exec -it d-order-pgbouncer-dev \
     psql -h localhost -p 6432 -U postgres pgbouncer \
     -c "SHOW POOLS; SHOW STATS;"
   ```

### 알려진 함정 회귀 점검

- **`prepareThreshold=0` 누락 시**: Spring 동일 쿼리 5회 반복 후 `prepared statement "S_1" does not exist`.
- **마이그레이션**: `manage.py migrate` 는 짧은 트랜잭션 다수라 transaction pooling 에서도 동작하나, 큰 마이그레이션은 PG 직접 연결 권장.

---

## 롤백

가장 안전 — compose 의 `DB_HOST: pgbouncer` → `postgres`, `DB_PORT: 6432` → `5432` 로 되돌리고 워크플로 재실행. pgbouncer 컨테이너는 stop. `prepareThreshold=0` 은 회귀 없음이라 그대로 둬도 무관. `max_connections=100 → 200` 환원은 PG 재시작 필요.

---

## 향후 작업

- 안정화 후 `DB_CONN_MAX_AGE=60` 으로 다시 켜고 ASGI 누수 재발 안 하는지 dev 에서 검증
- PgBouncer 단일 인스턴스 SPOF 완화 (필요 시 다중 인스턴스 + LB)
- `SHOW POOLS / STATS` 를 활용한 PgBouncer 메트릭 CloudWatch 연동
