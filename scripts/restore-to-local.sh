#!/bin/bash
# 프로덕션 백업을 로컬 개발 환경에 복원.
#
# 전제:
#   - backup-from-prod.sh 로 backups/<날짜>/ 에 백업이 있어야 함
#   - 로컬 docker-compose.local_env.yml 의 컨테이너가 떠 있어야 함
#
# 실행:
#   ./scripts/restore-to-local.sh                    # 가장 최근 백업 복원
#   ./scripts/restore-to-local.sh 2026-05-29         # 특정 날짜 복원

set -uo pipefail   # -e 제거: grep no-match 로 죽지 않도록

# ── 설정 ──────────────────────────────────────────────
BACKUP_ROOT="${BACKUP_ROOT:-./backup}"
LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-./.env.local}"

echo "▶ BACKUP_ROOT: $BACKUP_ROOT"

# 경로 존재 확인
if [ ! -d "$BACKUP_ROOT" ]; then
  echo "❌ 백업 디렉토리 없음: $BACKUP_ROOT"
  exit 1
fi

# 인자 처리 (날짜) — 없으면 dorder-*.dump 중 가장 최근 날짜 자동 감지
if [ -n "${1:-}" ]; then
  DATE="$1"
else
  # 1차: BACKUP_ROOT 바로 아래 dorder-YYYY-MM-DD.dump
  DATE=$(ls -1 "$BACKUP_ROOT" 2>/dev/null \
    | grep -oE 'dorder-[0-9]{4}-[0-9]{2}-[0-9]{2}\.dump' 2>/dev/null \
    | sed 's/dorder-\(.*\)\.dump/\1/' \
    | sort -r | head -1)

  # 2차: 서브디렉토리 패턴 (BACKUP_ROOT/YYYY-MM-DD/dorder-*.dump)
  if [ -z "$DATE" ]; then
    DATE=$(ls -1 "$BACKUP_ROOT" 2>/dev/null \
      | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' 2>/dev/null \
      | sort -r | head -1)
    if [ -n "$DATE" ] && [ -f "${BACKUP_ROOT}/${DATE}/dorder-${DATE}.dump" ]; then
      BACKUP_ROOT="${BACKUP_ROOT}/${DATE}"
      echo "▶ 서브디렉토리 패턴 감지 → $BACKUP_ROOT"
    fi
  fi
fi

if [ -z "${DATE:-}" ]; then
  echo "❌ ${BACKUP_ROOT}/ 에 dorder-YYYY-MM-DD.dump 형식의 백업이 없음"
  echo ""
  echo "현재 디렉토리 내용:"
  ls -la "$BACKUP_ROOT/" 2>/dev/null || echo "  (디렉토리 비어 있거나 접근 불가)"
  echo ""
  echo "먼저 ./scripts/backup-from-prod.sh 로 백업을 받아오세요."
  exit 1
fi

echo "▶ 감지된 백업 날짜: $DATE"
BACKUP_DIR="${BACKUP_ROOT}"
DUMP_FILE="${BACKUP_DIR}/dorder-${DATE}.dump"
RDB_FILE="${BACKUP_DIR}/redis-${DATE}.rdb"
ENV_FILE="${BACKUP_DIR}/env-${DATE}.bak"

for f in "$DUMP_FILE" "$RDB_FILE" "$ENV_FILE"; do
  if [ ! -f "$f" ]; then
    echo "❌ 파일 없음: $f"
    exit 1
  fi
done

echo "▶ 복원 대상 백업: $BACKUP_DIR"
echo "  - DB:    $(du -h "$DUMP_FILE" | cut -f1)"
echo "  - Redis: $(du -h "$RDB_FILE" | cut -f1)"
echo "  - env:   $(du -h "$ENV_FILE" | cut -f1)"
echo ""
read -p "진행할까요? (yes 입력) " confirm
[ "$confirm" = "yes" ] || { echo "취소"; exit 1; }

# ── Step 1. 로컬 컨테이너 떠 있는지 확인 ──────────────
echo ""
echo "▶ [1/4] 로컬 컨테이너 확인..."

PG_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E 'postgres|d-order.*postgres' | head -1)
REDIS_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E 'redis|d-order.*redis' | head -1)

if [ -z "$PG_CONTAINER" ] || [ -z "$REDIS_CONTAINER" ]; then
  echo "❌ 로컬 PostgreSQL/Redis 컨테이너가 떠 있지 않음."
  echo "   먼저 실행: docker compose -f docker-compose.local_env.yml up -d"
  exit 1
fi

echo "  PostgreSQL: $PG_CONTAINER"
echo "  Redis:      $REDIS_CONTAINER"

# .env 에서 자격증명 추출
PG_USER=$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
PG_DB=$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
REDIS_PW=$(grep -E '^REDIS_PASSWORD=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')

echo "  DB: $PG_DB / User: $PG_USER"

# ── Step 2. PostgreSQL 복원 ───────────────────────────
echo ""
echo "▶ [2/4] PostgreSQL 복원 중..."
echo "  ⚠ 기존 데이터는 모두 덮어쓰여집니다."

docker cp "$DUMP_FILE" "${PG_CONTAINER}:/tmp/restore.dump"
docker exec "$PG_CONTAINER" pg_restore \
  -U "$PG_USER" -d "$PG_DB" \
  --clean --if-exists --no-owner --no-acl \
  /tmp/restore.dump 2>&1 | tail -20 || true
docker exec "$PG_CONTAINER" rm -f /tmp/restore.dump
echo "✅ PostgreSQL 복원 완료"

# ── Step 3. Redis 복원 ─────────────────────────────────
echo ""
echo "▶ [3/4] Redis 복원 중..."
# RDB 파일은 Redis 시작 시 로드되므로 stop → 교체 → start
docker stop "$REDIS_CONTAINER" >/dev/null
docker cp "$RDB_FILE" "${REDIS_CONTAINER}:/data/dump.rdb"
docker start "$REDIS_CONTAINER" >/dev/null
sleep 2

# 로컬 Redis 비밀번호는 프로젝트 루트 .env 에 있을 가능성. 없으면 prod 와 같다고 가정.
LOCAL_REDIS_PW="$REDIS_PW"
if [ -f "./.env" ]; then
  PW_FROM_ROOT=$(grep -E '^REDIS_PASSWORD=' ./.env 2>/dev/null | cut -d= -f2 | tr -d '"')
  [ -n "$PW_FROM_ROOT" ] && LOCAL_REDIS_PW="$PW_FROM_ROOT"
fi
DBSIZE=$(docker exec "$REDIS_CONTAINER" redis-cli -a "$LOCAL_REDIS_PW" --no-auth-warning DBSIZE 2>/dev/null || echo "?")
echo "✅ Redis 복원 완료 (DBSIZE=${DBSIZE})"

# ── Step 4. .env 복사 (django/ 디렉토리로) ─────────────
echo ""
echo "▶ [4/4] .env 복사 및 로컬용 조정..."

DJANGO_ENV="./django/.env"
cp "$ENV_FILE" "$DJANGO_ENV"

# 헬퍼: KEY=VALUE 형태로 set or replace
set_env() {
  local key="$1" value="$2" file="$3"
  if grep -qE "^${key}=" "$file"; then
    # macOS/Linux 공통 호환을 위해 임시파일 사용
    sed -e "s|^${key}=.*|${key}=${value}|" "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

# 로컬용으로 자동 조정 (없으면 추가)
set_env ENVIRONMENT local         "$DJANGO_ENV"
set_env DEBUG       True          "$DJANGO_ENV"
set_env DB_ENGINE   django.db.backends.postgresql "$DJANGO_ENV"
set_env DB_HOST     localhost     "$DJANGO_ENV"
set_env DB_PORT     5432          "$DJANGO_ENV"
set_env DB_NAME     "$PG_DB"      "$DJANGO_ENV"
set_env DB_USER     "$PG_USER"    "$DJANGO_ENV"
# DB_PASSWORD 는 원본 .env 에 POSTGRES_PASSWORD 로 있음 → 그대로 가져옴
PG_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
set_env DB_PASSWORD "$PG_PASSWORD" "$DJANGO_ENV"
set_env REDIS_HOST  localhost     "$DJANGO_ENV"
set_env REDIS_PORT  6379          "$DJANGO_ENV"

echo "✅ ${DJANGO_ENV} 생성 (로컬 모드로 조정됨)"
echo "  필수 변수 확인:"
grep -E '^(ENVIRONMENT|DEBUG|DB_HOST|DB_PORT|DB_NAME|DB_USER|REDIS_HOST|REDIS_PORT)=' "$DJANGO_ENV" | sed 's/^/    /'

# ── Step 5. S3 미디어 → 로컬 media/ 복사 ───────────────
echo ""
echo "▶ [5/5] S3 미디어 → django/media/ 복사..."

S3_BACKUP_DIR="${BACKUP_DIR}/s3-media"
S3_ZIP="${BACKUP_DIR}/s3-backup.zip"
LOCAL_MEDIA_DIR="./django/media"

# s3-backup.zip 이 있고 디렉토리가 없으면 자동 해제
if [ -f "$S3_ZIP" ] && [ ! -d "$S3_BACKUP_DIR" ]; then
  echo "  s3-backup.zip 발견 — 해제 중..."
  mkdir -p "$S3_BACKUP_DIR"
  unzip -q "$S3_ZIP" -d "$S3_BACKUP_DIR"
fi

if [ ! -d "$S3_BACKUP_DIR" ]; then
  echo "⚠ S3 백업 없음 (${S3_BACKUP_DIR} 또는 ${S3_ZIP})"
  echo "   먼저 ./scripts/backup-from-prod.sh --with-s3 로 받아오세요."
  echo "   미디어 없이도 동작은 가능 (이미지만 안 보임)."
else
  mkdir -p "$LOCAL_MEDIA_DIR"

  # S3 백업 구조 자동 탐지 — 가장 안쪽의 media/ 디렉토리 찾기
  # 가능한 구조:
  #   1) s3-media/<files>                      → SRC=s3-media
  #   2) s3-media/media/<files>                → SRC=s3-media/media
  #   3) s3-media/s3-backup/media/<files>      → SRC=s3-media/s3-backup/media  (zip 압축 시)
  #   4) s3-media/<버킷명>/media/<files>       → SRC=s3-media/<버킷명>/media
  SRC=$(find "$S3_BACKUP_DIR" -type d -name media 2>/dev/null | head -1)
  if [ -z "$SRC" ]; then
    SRC="$S3_BACKUP_DIR"
  fi
  echo "  소스: $SRC"

  # rsync 가 있으면 사용. macOS BSD rsync 는 --info=progress2 미지원이므로 호환 옵션만
  if command -v rsync >/dev/null; then
    rsync -ah "${SRC}/" "${LOCAL_MEDIA_DIR}/"
  else
    cp -R "${SRC}/." "${LOCAL_MEDIA_DIR}/"
  fi

  MEDIA_SIZE=$(du -sh "$LOCAL_MEDIA_DIR" | cut -f1)
  FILE_COUNT=$(find "$LOCAL_MEDIA_DIR" -type f | wc -l | tr -d ' ')
  echo "✅ ${LOCAL_MEDIA_DIR}/ 에 ${FILE_COUNT}개 파일 (${MEDIA_SIZE}) 복원"
fi

# ── 요약 ───────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 복원 완료 — 프로덕션과 동일 상태 (Option B: 미디어 로컬 복사)"
echo ""
echo "Django 실행:"
echo "  cd django && python manage.py runserver"
echo ""
echo "동작 원리:"
echo "  - ENVIRONMENT=local 이라 settings.py 가 FileSystemStorage 사용"
echo "  - DB 안의 ImageField 는 상대경로 ('menu_images/x.jpg') 만 저장"
echo "  - Django 가 MEDIA_URL=/media/ 로 자동 변환하여 로컬 파일 서빙"
echo "  - 즉 별도 URL 재작성 불필요"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
