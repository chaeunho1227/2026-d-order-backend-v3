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
 *   A. peak_load        — 손님 HTTP 주문 플로우 (35→55 VU)
 *   B. admin_ws_listener — 부스 어드민 10명 WS 동시 연결 유지 + 수신 이벤트 계수
 *      누락 건수 = orders_created − (orders_received ÷ 10)
 *
 * 실행 방법 (EC2 서버 위에서 실행):
 *   # nginx 로컬 직접 호출 (보안그룹 우회, Cloudflare 불필요)
 *   k6 run -e BASE_URL=https://localhost ~/load-test.js
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

// 부스 어드민 계정 (setup에서 BOOTH_UUID + TABLE_COUNT 자동 조회용)
const ADMIN_USERNAME = 'test77';
const ADMIN_PASSWORD = 'test';

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
     * A. 고객 주문 플로우
     * ramp-up 2m → 피크 10m → 버스트 3m → ramp-down 1m (총 16m)
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
     * B. 부스 어드민 WebSocket 감시
     * 10 VU가 테스트 전 구간 각자 연결 유지 → ADMIN_NEW_ORDER 이벤트 계수
     *
     * ⚠ orders_received 해석:
     *   10명의 어드민이 각각 동일한 ADMIN_NEW_ORDER 이벤트를 수신·집계하므로
     *   orders_received ≒ orders_created × 10  (완전 전달 시)
     *   실제 어드민 1인당 수신율 = orders_received ÷ 10
     *   누락 건수 = orders_created − (orders_received ÷ 10)
     */
    admin_ws_listener: {
      executor:     'constant-vus',
      exec:         'adminWsListener',
      vus:          10,
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

    // 주문 생성 최소 건수 확인
    orders_created:         ['count>100'],

    // WS 비정상 종료 허용 한도 (10명 기준, 워커 재시작 등 의도된 재연결 제외)
    ws_disconnects:         ['count<10'],
  },
};

// ──────────────────────────────────────────────
// setup: 어드민 로그인 → BOOTH_UUID·TABLE_COUNT·accessToken 자동 조회
// ──────────────────────────────────────────────
export function setup() {
  const jar = http.cookieJar();

  // ── 1. 로그인 (CSRF 불필요 — JWT 없는 익명 POST는 CSRF 체크 skip)
  const loginRes = http.post(
    `${BASE_URL}/api/v3/django/auth/`,
    JSON.stringify({ username: ADMIN_USERNAME, password: ADMIN_PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, jar },
  );
  if (loginRes.status !== 200) {
    throw new Error(`setup: 로그인 실패 (HTTP ${loginRes.status})\n${loginRes.body}`);
  }
  const loginData = JSON.parse(loginRes.body).data;
  console.log(`setup: 로그인 성공 — booth_name="${loginData.booth_name}"`);

  // JWT access_token 추출 (WS 핸드셰이크 Cookie 헤더에 사용)
  // access_token은 HttpOnly 쿠키 → jar.cookiesForURL()로는 읽히지 않음.
  // response.cookies는 k6 클라이언트 수준에서 모든 쿠키(HttpOnly 포함)를 반환한다.
  // StaleCookiePurgeMiddleware가 delete-cookie를 함께 부착하므로
  // value가 비어 있지 않은 마지막 항목을 선택한다.
  let accessToken = '';
  if (loginRes.cookies && loginRes.cookies.access_token) {
    for (const c of loginRes.cookies.access_token) {
      if (c.value) accessToken = c.value;
    }
  }
  if (!accessToken) {
    throw new Error('setup: access_token 쿠키를 찾을 수 없습니다.');
  }
  console.log('setup: access_token 획득 완료');

  // ── 2. QR URL 조회
  const qrRes = http.get(`${BASE_URL}/api/v3/django/booth/mypage/qr-download/`, { jar });
  if (qrRes.status !== 200) {
    throw new Error(`setup: QR URL 조회 실패 (HTTP ${qrRes.status})\n${qrRes.body}`);
  }
  const qrImageUrl = JSON.parse(qrRes.body).data.qr_image_url;

  // ── 3. QR URL에서 booth UUID 추출
  const uuidMatch = qrImageUrl.match(
    /booth_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_qr/i,
  );
  if (!uuidMatch) {
    throw new Error(`setup: QR URL에서 UUID를 찾을 수 없습니다. URL: ${qrImageUrl}`);
  }
  const boothUuid = uuidMatch[1];
  console.log(`setup: BOOTH_UUID = ${boothUuid}`);

  // ── 4. mypage에서 table_max_cnt 조회
  const mypageRes = http.get(`${BASE_URL}/api/v3/django/booth/mypage/`, { jar });
  if (mypageRes.status !== 200) {
    throw new Error(`setup: mypage 조회 실패 (HTTP ${mypageRes.status})\n${mypageRes.body}`);
  }
  const tableMaxCnt = JSON.parse(mypageRes.body).data.table_max_cnt || 10;
  console.log(`setup: TABLE_COUNT = ${tableMaxCnt}`);

  // ── 5. 테이블 데이터 리셋 (이전 테스트 잔류 세션 제거)
  //    DELETE는 JWT 쿠키가 있으면 CSRF 검사 — X-CSRFToken 헤더 필요
  const csrfRes = http.get(`${BASE_URL}/api/v3/django/auth/csrf-token/`, { jar });
  // csrftoken은 HttpOnly가 아니지만 response.cookies로 일관성 있게 읽는다.
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
    console.log(`setup: 테이블 데이터 리셋 — HTTP ${resetRes.status}`);
  } else {
    console.warn('setup: CSRF 토큰 획득 실패 — 테이블 리셋 건너뜀');
  }

  // ── 6. 고객용 메뉴 조회 (주문 가능 메뉴 ID 사전 수집)
  const menuRes = http.get(`${BASE_URL}/api/v3/django/booth/${boothUuid}/menu-list/`);
  if (menuRes.status !== 200) {
    throw new Error(`setup: menu-list 조회 실패 (HTTP ${menuRes.status})\n${menuRes.body}`);
  }
  const d = JSON.parse(menuRes.body).data;
  const menuIds = [
    ...(d.MENU  || []).filter(m => !m.is_soldout && m.stock > 0).map(m => m.id),
    ...(d.DRINK || []).filter(m => !m.is_soldout && m.stock > 0).map(m => m.id),
  ];
  const setIds = (d.SET || []).filter(s => !s.is_soldout && s.stock > 0).map(s => s.id);

  if (menuIds.length === 0) {
    throw new Error('setup: 주문 가능한 메뉴가 없습니다. 재고를 확인하세요.');
  }
  console.log(`setup 완료 — 메뉴 ${menuIds.length}개, 세트 ${setIds.length}개`);

  return { boothUuid, tableMaxCnt, menuIds, setIds, accessToken };
}

// ──────────────────────────────────────────────
// 기본 헤더
// ──────────────────────────────────────────────
const JSON_HEADERS = { 'Content-Type': 'application/json' };

// ──────────────────────────────────────────────
// 시나리오 A: 고객 전체 주문 플로우
// (고객 엔드포인트는 모두 authentication_classes=[] — 인증 불필요)
// ──────────────────────────────────────────────
export function customerFlow(data) {
  const { boothUuid, tableMaxCnt, menuIds, setIds } = data;
  // VU별 고정 테이블 배정 — 동일 VU는 항상 같은 테이블 → 세션 충돌 방지
  // VU 수 > tableMaxCnt 인 버스트 구간에서는 모듈러로 자연스럽게 순환
  const tableNum = ((__VU - 1) % tableMaxCnt) + 1;

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

  // 메뉴 탐색 시간
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
      // payment_info 가 실패했거나 다른 VU가 이미 처리한 케이스 — 스킵
      return;
    }

    const start = Date.now();
    const res = http.post(
      `${BASE_URL}/api/v3/django/cart/payment-confirm/`,
      JSON.stringify({ table_usage_id: tableUsageId }),
      { headers: JSON_HEADERS },
    );
    paymentConfirmDuration.add(Date.now() - start);

    // 409 CART_NOT_PENDING: 버스트 구간에서 VU 수 > 테이블 수로 인한 경합
    // → 실제 장애가 아니므로 에러로 집계하지 않고 조용히 스킵
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
// 시나리오 B: 부스 어드민 WebSocket 수신 감시
//
// 1 VU가 테스트 전 구간 연결을 유지하며 ADMIN_NEW_ORDER 이벤트를 계수한다.
// 테스트 종료 후 출력되는 두 지표를 비교하면 실제 누락 건수를 확인할 수 있다:
//   orders_created  — 고객이 결제 완료한 주문 수
//   orders_received — 어드민 화면에 실제로 도달한 주문 수
// ──────────────────────────────────────────────
export function adminWsListener(data) {
  const { accessToken } = data;
  const wsUrl = `${WS_BASE}/ws/django/booth/orders/management/`;

  const res = ws.connect(
    wsUrl,
    // JWT 쿠키를 WebSocket 핸드셰이크 Cookie 헤더에 포함
    { headers: { Cookie: `access_token=${accessToken}` } },
    function (socket) {

      socket.on('open', () => {
        console.log(`[Admin WS] 연결됨 → ${wsUrl}`);
      });

      socket.on('message', (raw) => {
        let msg;
        try { msg = JSON.parse(raw); } catch { return; }

        switch (msg.type) {
          case 'ADMIN_NEW_ORDER': {
            const orders = (msg.data && msg.data.orders) || [];
            if (orders.length > 0) {
              ordersReceived.add(orders.length);
              console.log(`[Admin WS] ADMIN_NEW_ORDER 수신 — ${orders.length}건`);
            } else {
              // orders 배열이 빈 경우 = DB에서 주문 조회 실패 (잠재적 누락)
              console.warn('[Admin WS] ADMIN_NEW_ORDER 수신 — orders 비어 있음 (조회 실패?)');
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
          console.warn(`[Admin WS] 비정상 종료 — code=${code}`);
        } else {
          console.log(`[Admin WS] 정상 종료 — code=${code}`);
        }
      });

      socket.on('error', (e) => {
        wsDisconnects.add(1);
        console.error('[Admin WS] 에러:', e);
      });

      // 30초마다 PING 전송 → 서버 PONG 응답으로 연결 생존 확인
      // (서버 측 HEARTBEAT_INTERVAL=25s 와 겹치지 않게 30s로 설정)
      socket.setInterval(() => {
        socket.send(JSON.stringify({ type: 'PING' }));
      }, 30000);

      // 테스트 종료 시 정상 닫기 (18m 20s — duration보다 10s 짧게)
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
