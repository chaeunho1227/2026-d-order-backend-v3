/**
 * D-Order 고객 주문 플로우 부하테스트 (WebSocket 감시 포함)
 *
 * GA 실측 기반 (2026-05-25~26):
 *   활성 사용자 435명, 세션당 조회 10.63회, 평균 참여시간 63초
 *   Cloudflare 1,000 visits/hour → Little's Law → 동시 접속 ~18명
 *
 * VU 설정:
 *   기준  : 18 VU  (실측값)
 *   엄격  : 35 VU  (×2배, 피크 버스트 반영) ← 기본값
 *   스트레스: 55 VU (×3배, 장애 임계점 탐색)
 *
 * 시나리오 구성:
 *   A. peak_load        — 손님 HTTP 주문 플로우 (35→55 VU, 부스 분산)
 *   B. admin_ws_listener — 어드민 N명이 각자 자기 부스 WS 연결 유지
 *      어드민마다 다른 부스를 감시하므로 orders_received ≈ orders_created
 *      (누락 건수 = orders_created − orders_received)
 *
 * 실행 방법 (EC2 서버 위에서 실행):
 *   # 단일 부스 (기본)
 *   k6 run -e BASE_URL=https://localhost ~/load-test.js
 *
 *   # 다중 부스 (어드민 계정 목록을 콤마로 구분, user:pass 형식)
 *   k6 run -e BASE_URL=https://localhost \
 *           -e ADMIN_CREDS='test77:test,test78:test,test79:test,...' \
 *           ~/load-test.js
 */

import http from 'k6/http';
import ws   from 'k6/ws';
import { check, sleep, group } from 'k6';
import { Counter, Trend } from 'k6/metrics';

// ──────────────────────────────────────────────
// 환경변수
// ──────────────────────────────────────────────
const BASE_URL = __ENV.BASE_URL || 'https://dorder-api.shop';
// https → wss, http → ws  (WebSocket URL 자동 변환)
const WS_BASE  = BASE_URL.replace(/^https:\/\//, 'wss://').replace(/^http:\/\//, 'ws://');

// 어드민 계정 목록 — 환경변수 ADMIN_CREDS로 주입, 기본값 test77:test
// 형식: 'user1:pass1,user2:pass2,...'
const ADMIN_CREDS = (__ENV.ADMIN_CREDS || 'test77:test')
  .split(',')
  .map(s => {
    const sep = s.indexOf(':');
    return { username: s.slice(0, sep), password: s.slice(sep + 1) };
  });

const ADMIN_COUNT = ADMIN_CREDS.length;

// ──────────────────────────────────────────────
// 커스텀 메트릭
// ──────────────────────────────────────────────
const paymentConfirmDuration = new Trend('payment_confirm_ms', true);
const paymentConfirmErrors   = new Counter('payment_confirm_errors');
const ordersCreated          = new Counter('orders_created');    // 고객 결제 완료
const tableEnterErrors       = new Counter('table_enter_errors');
const ordersReceived         = new Counter('orders_received');   // 어드민 WS 실제 수신
const wsDisconnects          = new Counter('ws_disconnects');    // 비정상 WS 종료

// ──────────────────────────────────────────────
// 시나리오 & 임계값
// ──────────────────────────────────────────────
export const options = {
  // localhost 실행 시 TLS 인증서 호스트명 불일치 우회 (HTTP + WS 공통 적용)
  insecureSkipTLSVerify: true,

  scenarios: {
    /**
     * A. 손님 주문 플로우
     * ramp-up 2m → 피크 10m → 버스트 3m → ramp-down 1m (총 18m)
     * VU는 __VU % ADMIN_COUNT 로 부스를 분산 배정
     */
    peak_load: {
      executor:         'ramping-vus',
      exec:             'customerFlow',
      startVUs:         0,
      stages: [
        { duration: '2m',  target: 35 }, // ramp-up
        { duration: '10m', target: 35 }, // 피크 유지 (엄격 기준 35 VU)
        { duration: '2m',  target: 55 }, // 버스트: ×3배
        { duration: '3m',  target: 55 }, // 버스트 유지
        { duration: '1m',  target: 0  }, // ramp-down
      ],
      gracefulRampDown: '30s',
    },

    /**
     * B. 어드민 WebSocket 감시
     * ADMIN_COUNT VU 각자 자기 부스 WS 연결 유지 → ADMIN_NEW_ORDER 이벤트 계수
     * 부스가 다르므로 이벤트 중복 없음 → orders_received ≈ orders_created
     */
    admin_ws_listener: {
      executor:     'constant-vus',
      exec:         'adminWsListener',
      vus:          ADMIN_COUNT,
      duration:     '18m30s',  // peak_load 전 구간 커버 (+30s 여유)
      gracefulStop: '10s',
    },
  },

  thresholds: {
    // 전체 요청
    http_req_duration:      ['p(95)<2000', 'p(99)<5000'],
    http_req_failed:        ['rate<0.01'],

    // payment-confirm 전용 (주문 누락 핵심 지표)
    payment_confirm_ms:     ['p(95)<3000', 'p(99)<8000'],
    payment_confirm_errors: ['count<5'],

    // 주문 생성 최소 건수
    orders_created:         ['count>100'],

    // WS 비정상 종료 허용 한도 (어드민 수 × 1회 재시작 여유)
    ws_disconnects:         [`count<${ADMIN_COUNT}`],
  },
};

// ──────────────────────────────────────────────
// 부스 1개 setup 헬퍼
// ──────────────────────────────────────────────
function setupBooth(cred) {
  const jar = http.cookieJar();
  const { username, password } = cred;

  // ── 1. 로그인 (CSRF 불필요 — JWT 없는 익명 POST는 CSRF 체크 skip)
  const loginRes = http.post(
    `${BASE_URL}/api/v3/django/auth/`,
    JSON.stringify({ username, password }),
    { headers: { 'Content-Type': 'application/json' }, jar },
  );
  if (loginRes.status !== 200) {
    throw new Error(`setup [${username}]: 로그인 실패 (HTTP ${loginRes.status})\n${loginRes.body}`);
  }
  const loginData = JSON.parse(loginRes.body).data;
  console.log(`setup [${username}]: 로그인 성공 — booth_name="${loginData.booth_name}"`);

  // JWT access_token 추출 (WS 핸드셰이크 Cookie 헤더에 사용)
  // access_token은 HttpOnly → jar.cookiesForURL()로는 읽히지 않으므로
  // response.cookies에서 직접 추출한다.
  let accessToken = '';
  if (loginRes.cookies && loginRes.cookies.access_token) {
    for (const c of loginRes.cookies.access_token) {
      if (c.value) accessToken = c.value;
    }
  }
  if (!accessToken) {
    throw new Error(`setup [${username}]: access_token 쿠키를 찾을 수 없습니다.`);
  }

  // ── 2. QR URL 조회 → booth UUID 추출
  const qrRes = http.get(`${BASE_URL}/api/v3/django/booth/mypage/qr-download/`, { jar });
  if (qrRes.status !== 200) {
    throw new Error(`setup [${username}]: QR URL 조회 실패 (HTTP ${qrRes.status})`);
  }
  const qrImageUrl = JSON.parse(qrRes.body).data.qr_image_url;
  const uuidMatch  = qrImageUrl.match(
    /booth_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_qr/i,
  );
  if (!uuidMatch) {
    throw new Error(`setup [${username}]: QR URL에서 UUID를 찾을 수 없습니다. URL: ${qrImageUrl}`);
  }
  const boothUuid = uuidMatch[1];
  console.log(`setup [${username}]: BOOTH_UUID = ${boothUuid}`);

  // ── 3. mypage → table_max_cnt
  const mypageRes = http.get(`${BASE_URL}/api/v3/django/booth/mypage/`, { jar });
  if (mypageRes.status !== 200) {
    throw new Error(`setup [${username}]: mypage 조회 실패 (HTTP ${mypageRes.status})`);
  }
  const tableMaxCnt = JSON.parse(mypageRes.body).data.table_max_cnt || 10;
  console.log(`setup [${username}]: TABLE_COUNT = ${tableMaxCnt}`);

  // ── 4. 테이블 데이터 리셋 (이전 테스트 잔류 세션 제거)
  //    DELETE는 JWT 쿠키가 있으면 CSRF 검사 → X-CSRFToken 헤더 필요
  const csrfRes = http.get(`${BASE_URL}/api/v3/django/auth/csrf-token/`, { jar });
  let csrfToken = '';
  if (csrfRes.cookies && csrfRes.cookies.csrftoken) {
    for (const c of csrfRes.cookies.csrftoken) {
      if (c.value) csrfToken = c.value;
    }
  }
  if (csrfToken) {
    const resetRes = http.del(
      `${BASE_URL}/api/v3/django/booth/mypage/reset-table-data/`,
      null,
      { headers: { 'X-CSRFToken': csrfToken }, jar },
    );
    console.log(`setup [${username}]: 테이블 리셋 — HTTP ${resetRes.status}`);
  } else {
    console.warn(`setup [${username}]: CSRF 토큰 획득 실패 — 테이블 리셋 건너뜀`);
  }

  // ── 5. 고객용 메뉴 조회 (주문 가능 메뉴 ID 사전 수집)
  const menuRes = http.get(`${BASE_URL}/api/v3/django/booth/${boothUuid}/menu-list/`);
  if (menuRes.status !== 200) {
    throw new Error(`setup [${username}]: menu-list 조회 실패 (HTTP ${menuRes.status})`);
  }
  const d = JSON.parse(menuRes.body).data;
  const menuIds = [
    ...(d.MENU  || []).filter(m => !m.is_soldout && m.stock > 0).map(m => m.id),
    ...(d.DRINK || []).filter(m => !m.is_soldout && m.stock > 0).map(m => m.id),
  ];
  const setIds = (d.SET || []).filter(s => !s.is_soldout && s.stock > 0).map(s => s.id);

  if (menuIds.length === 0) {
    throw new Error(`setup [${username}]: 주문 가능한 메뉴가 없습니다. 재고를 확인하세요.`);
  }
  console.log(`setup [${username}]: 메뉴 ${menuIds.length}개, 세트 ${setIds.length}개`);

  return { boothUuid, tableMaxCnt, menuIds, setIds, accessToken };
}

// ──────────────────────────────────────────────
// setup: 전체 어드민 계정으로 부스 데이터 수집
// ──────────────────────────────────────────────
export function setup() {
  const booths = ADMIN_CREDS.map(cred => setupBooth(cred));
  console.log(`setup 완료 — 총 ${booths.length}개 부스`);
  return { booths };
}

// ──────────────────────────────────────────────
// 기본 헤더
// ──────────────────────────────────────────────
const JSON_HEADERS = { 'Content-Type': 'application/json' };

// ──────────────────────────────────────────────
// 시나리오 A: 손님 전체 주문 플로우
// (고객 엔드포인트는 모두 authentication_classes=[] — 인증 불필요)
// ──────────────────────────────────────────────
export function customerFlow(data) {
  // VU → 부스 분산: 모든 부스에 손님이 고르게 들어가도록 모듈러 배정
  const booth      = data.booths[(__VU - 1) % data.booths.length];
  const { boothUuid, tableMaxCnt, menuIds, setIds } = booth;

  // 부스 내 테이블 고정 배정 — 동일 VU는 항상 같은 테이블 → 세션 충돌 방지
  const tableNum = (Math.floor((__VU - 1) / data.booths.length) % tableMaxCnt) + 1;

  // ── Step 1. 테이블 입장 ──────────────────────
  let tableUsageId;
  group('01_table_enter', () => {
    const res = http.post(
      `${BASE_URL}/api/v3/django/booth/${boothUuid}/table/`,
      JSON.stringify({ table_num: tableNum }),
      { headers: JSON_HEADERS },
    );
    const ok = check(res, { 'table_enter 200': (r) => r.status === 200 });
    if (!ok) {
      tableEnterErrors.add(1);
      return;
    }
    tableUsageId = JSON.parse(res.body).data.table_usage_id;
  });

  if (!tableUsageId) {
    sleep(rand(2, 5));
    return;
  }

  // payment_info 가 PENDING 상태를 확정했을 때만 payment_confirm 진행
  let cartIsPending = false;

  // ── Step 2. 메뉴판 조회 ─────────────────────
  group('02_menu_list', () => {
    const res = http.get(
      `${BASE_URL}/api/v3/django/booth/${boothUuid}/menu-list/?table_num=${tableNum}`,
    );
    check(res, { 'menu_list 200': (r) => r.status === 200 });
  });

  sleep(rand(3, 7));

  // ── Step 3. 장바구니 담기 (1~3개) ───────────
  group('03_add_to_cart', () => {
    const count = randomInt(1, 3);
    for (let i = 0; i < count; i++) {
      let body;
      if (setIds.length > 0 && Math.random() < 0.2) {
        body = {
          table_usage_id: tableUsageId,
          type:           'setmenu',
          set_menu_id:    pickRandom(setIds),
          quantity:       1,
        };
      } else {
        body = {
          table_usage_id: tableUsageId,
          type:           'menu',
          menu_id:        pickRandom(menuIds),
          quantity:       randomInt(1, 2),
        };
      }
      const res = http.post(
        `${BASE_URL}/api/v3/django/cart/`,
        JSON.stringify(body),
        { headers: JSON_HEADERS },
      );
      check(res, { 'add_to_cart 200': (r) => r.status === 200 });
      sleep(rand(0.3, 0.8));
    }
  });

  // ── Step 4. 장바구니 조회 ────────────────────
  group('04_cart_detail', () => {
    const res = http.get(
      `${BASE_URL}/api/v3/django/cart/detail/?table_usage_id=${tableUsageId}`,
    );
    check(res, { 'cart_detail 200': (r) => r.status === 200 });
  });

  sleep(rand(2, 5));

  // ── Step 5. 결제 정보 진입 ──────────────────
  group('05_payment_info', () => {
    const res = http.post(
      `${BASE_URL}/api/v3/django/cart/payment-info/`,
      JSON.stringify({ table_usage_id: tableUsageId }),
      { headers: JSON_HEADERS },
    );
    const ok = check(res, { 'payment_info 200': (r) => r.status === 200 });
    if (ok) cartIsPending = true;
  });

  sleep(rand(1, 3));

  // ── Step 6. 결제 확인 (payment-confirm) ─────
  // 핵심 엔드포인트 — 주문 누락 원인 지점
  group('06_payment_confirm', () => {
    if (!cartIsPending) {
      // payment_info 실패 또는 다른 VU가 이미 처리 — 스킵
      return;
    }

    const start = Date.now();
    const res = http.post(
      `${BASE_URL}/api/v3/django/cart/payment-confirm/`,
      JSON.stringify({ table_usage_id: tableUsageId }),
      { headers: JSON_HEADERS },
    );
    paymentConfirmDuration.add(Date.now() - start);

    // 409 CART_NOT_PENDING: 버스트 구간 VU > 테이블 수 경합 → 실제 장애 아님
    if (res.status === 409) {
      console.log(
        `[payment_confirm 409-skip] table_usage_id=${tableUsageId} — VU 경합 스킵`,
      );
      return;
    }

    const ok = check(res, {
      'payment_confirm 200': (r) => r.status === 200,
    });

    if (ok) {
      ordersCreated.add(1);
    } else {
      paymentConfirmErrors.add(1);
      console.warn(
        `[payment_confirm FAIL] table_usage_id=${tableUsageId} ` +
        `status=${res.status} body=${res.body.slice(0, 200)}`,
      );
    }
  });

  // ── Step 7. 주문 내역 조회 ──────────────────
  group('07_order_history', () => {
    const res = http.get(
      `${BASE_URL}/api/v3/django/order/table/${tableUsageId}/`,
    );
    check(res, { 'order_history 200': (r) => r.status === 200 });
  });

  sleep(rand(5, 15));
}

// ──────────────────────────────────────────────
// 시나리오 B: 어드민 WebSocket 수신 감시
//
// 어드민 N명이 각자 자기 부스 WS에 연결해 ADMIN_NEW_ORDER 이벤트를 계수.
// 부스가 서로 다르므로 이벤트 중복 없음.
//
//   orders_received ≈ orders_created   → 완전 전달
//   orders_created − orders_received   → 실제 누락 건수
// ──────────────────────────────────────────────
export function adminWsListener(data) {
  // 어드민 VU → 부스 분산 (손님과 동일한 모듈러 방식)
  const booth       = data.booths[(__VU - 1) % data.booths.length];
  const { accessToken, boothUuid } = booth;
  const wsUrl       = `${WS_BASE}/ws/django/booth/orders/management/`;

  const res = ws.connect(
    wsUrl,
    // JWT 쿠키를 WebSocket 핸드셰이크 Cookie 헤더에 포함
    { headers: { Cookie: `access_token=${accessToken}` } },
    function (socket) {

      socket.on('open', () => {
        console.log(`[Admin WS] booth=${boothUuid.slice(0, 8)}… 연결됨`);
      });

      socket.on('message', (raw) => {
        let msg;
        try { msg = JSON.parse(raw); } catch { return; }

        switch (msg.type) {
          case 'ADMIN_NEW_ORDER': {
            const orders = (msg.data && msg.data.orders) || [];
            if (orders.length > 0) {
              ordersReceived.add(orders.length);
              console.log(`[Admin WS] booth=${boothUuid.slice(0, 8)}… ADMIN_NEW_ORDER ${orders.length}건`);
            } else {
              // orders 배열이 빈 경우 = DB 조회 실패 (잠재적 누락)
              console.warn(`[Admin WS] booth=${boothUuid.slice(0, 8)}… ADMIN_NEW_ORDER 수신 — orders 비어 있음`);
            }
            break;
          }
          case 'PONG':
            // 서버 heartbeat — 무시
            break;
          default:
            // ADMIN_ORDER_SNAPSHOT, ADMIN_ORDER_UPDATE 등 — 무시
            break;
        }
      });

      socket.on('close', (code) => {
        // 1000 = 정상 종료, 1001 = Going Away (서버 재시작 등)
        if (code !== 1000 && code !== 1001) {
          wsDisconnects.add(1);
          console.warn(`[Admin WS] booth=${boothUuid.slice(0, 8)}… 비정상 종료 — code=${code}`);
        } else {
          console.log(`[Admin WS] booth=${boothUuid.slice(0, 8)}… 정상 종료 — code=${code}`);
        }
      });

      socket.on('error', (e) => {
        wsDisconnects.add(1);
        console.error(`[Admin WS] booth=${boothUuid.slice(0, 8)}… 에러:`, e);
      });

      // 30초마다 PING 전송 → 서버 PONG 응답으로 연결 생존 확인
      socket.setInterval(() => {
        socket.send(JSON.stringify({ type: 'PING' }));
      }, 30000);

      // 테스트 종료 시 정상 닫기 (18m 20s)
      socket.setTimeout(() => {
        socket.close(1000);
      }, (18 * 60 + 20) * 1000);
    },
  );

  check(res, { 'admin_ws 101': (r) => r && r.status === 101 });
}

// ──────────────────────────────────────────────
// 유틸
// ──────────────────────────────────────────────
function rand(min, max)      { return Math.random() * (max - min) + min; }
function randomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
function pickRandom(arr)     { return arr[Math.floor(Math.random() * arr.length)]; }
