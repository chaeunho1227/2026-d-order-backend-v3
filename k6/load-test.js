/**
 * D-Order 고객 주문 플로우 부하테스트
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
 * 실행 방법 (EC2 서버 위에서 실행):
 *   # k6 설치
 *   sudo gpg -k
 *   sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
 *     --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
 *   echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
 *     | sudo tee /etc/apt/sources.list.d/k6.list
 *   sudo apt-get update && sudo apt-get install k6
 *
 *   # nginx 로컬 직접 호출 (보안그룹 우회, Cloudflare 불필요)
 *   k6 run -e BASE_URL=http://localhost k6/load-test.js
 *
 *   # 외부에서 실행 시 (Cloudflare 경유)
 *   k6 run k6/load-test.js
 */

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter, Trend } from 'k6/metrics';

// ──────────────────────────────────────────────
// 환경변수 (BASE_URL만 오버라이드 가능)
// ──────────────────────────────────────────────
const BASE_URL = __ENV.BASE_URL || 'https://dorder-api.shop';

// 부스 어드민 계정 (setup에서 BOOTH_UUID + TABLE_COUNT 자동 조회용)
const ADMIN_USERNAME = 'test77';
const ADMIN_PASSWORD = 'test';

// ──────────────────────────────────────────────
// 커스텀 메트릭 (payment-confirm 집중 관측)
// ──────────────────────────────────────────────
const paymentConfirmDuration = new Trend('payment_confirm_ms', true);
const paymentConfirmErrors   = new Counter('payment_confirm_errors');
const ordersCreated          = new Counter('orders_created');
const tableEnterErrors       = new Counter('table_enter_errors');

// ──────────────────────────────────────────────
// 시나리오 & 임계값
// ──────────────────────────────────────────────
export const options = {
  scenarios: {
    /**
     * 엄격 피크 시나리오 (GA 실측 ×2)
     * ramp-up 2m → 피크 10m → 버스트 3m → ramp-down 1m (총 18분)
     */
    peak_load: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '2m',  target: 35 }, // ramp-up
        { duration: '10m', target: 35 }, // 피크 유지 (엄격 기준 35 VU)
        { duration: '2m',  target: 55 }, // 버스트: ×3배
        { duration: '3m',  target: 55 }, // 버스트 유지
        { duration: '1m',  target: 0  }, // ramp-down
      ],
      gracefulRampDown: '30s',
    },
  },

  thresholds: {
    // 전체 요청
    http_req_duration:      ['p(95)<2000', 'p(99)<5000'],
    http_req_failed:        ['rate<0.01'],

    // payment-confirm 전용 (주문 누락 핵심 지표)
    payment_confirm_ms:     ['p(95)<3000', 'p(99)<8000'],
    payment_confirm_errors: ['count<5'],

    // 최소 주문 생성 확인
    orders_created:         ['count>100'],
  },
};

// ──────────────────────────────────────────────
// setup: 어드민 로그인으로 BOOTH_UUID + TABLE_COUNT 자동 조회
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
    throw new Error(
      `setup: 로그인 실패 (HTTP ${loginRes.status})\n${loginRes.body}`,
    );
  }

  const loginData = JSON.parse(loginRes.body).data;
  console.log(`setup: 로그인 성공 — booth_name="${loginData.booth_name}"`);

  // ── 2. QR URL 조회 (access_token 쿠키 자동 전송)
  const qrRes = http.get(
    `${BASE_URL}/api/v3/django/booth/mypage/qr-download/`,
    { jar },
  );

  if (qrRes.status !== 200) {
    throw new Error(
      `setup: QR URL 조회 실패 (HTTP ${qrRes.status})\n${qrRes.body}`,
    );
  }

  const qrImageUrl = JSON.parse(qrRes.body).data.qr_image_url;

  // ── 3. QR 이미지 URL에서 booth UUID 추출
  //    URL 패턴: .../booth_<uuid>_qr.png
  const uuidMatch = qrImageUrl.match(
    /booth_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_qr/i,
  );
  if (!uuidMatch) {
    throw new Error(`setup: QR URL에서 UUID를 찾을 수 없습니다. URL: ${qrImageUrl}`);
  }
  const boothUuid = uuidMatch[1];
  console.log(`setup: BOOTH_UUID = ${boothUuid}`);

  // ── 4. mypage에서 table_max_cnt 조회
  const mypageRes = http.get(
    `${BASE_URL}/api/v3/django/booth/mypage/`,
    { jar },
  );

  if (mypageRes.status !== 200) {
    throw new Error(
      `setup: mypage 조회 실패 (HTTP ${mypageRes.status})\n${mypageRes.body}`,
    );
  }

  const tableMaxCnt = JSON.parse(mypageRes.body).data.table_max_cnt || 10;
  console.log(`setup: TABLE_COUNT = ${tableMaxCnt}`);

  // ── 5. 고객용 메뉴 조회 (주문 가능 메뉴 ID 사전 수집)
  const menuRes = http.get(
    `${BASE_URL}/api/v3/django/booth/${boothUuid}/menu-list/`,
  );

  if (menuRes.status !== 200) {
    throw new Error(
      `setup: menu-list 조회 실패 (HTTP ${menuRes.status})\n${menuRes.body}`,
    );
  }

  const d = JSON.parse(menuRes.body).data;

  const menuIds = [
    ...( d.MENU  || [] ).filter(m => !m.is_soldout && m.stock > 0).map(m => m.id),
    ...( d.DRINK || [] ).filter(m => !m.is_soldout && m.stock > 0).map(m => m.id),
  ];
  const setIds = ( d.SET || [] ).filter(s => !s.is_soldout && s.stock > 0).map(s => s.id);

  if (menuIds.length === 0) {
    throw new Error('setup: 주문 가능한 메뉴가 없습니다. 재고를 확인하세요.');
  }

  console.log(`setup 완료 — 메뉴 ${menuIds.length}개, 세트 ${setIds.length}개`);

  return { boothUuid, tableMaxCnt, menuIds, setIds };
}

// ──────────────────────────────────────────────
// 기본 헤더
// ──────────────────────────────────────────────
const JSON_HEADERS = { 'Content-Type': 'application/json' };

// ──────────────────────────────────────────────
// 메인 VU 시나리오: 고객 전체 주문 플로우
// (고객 엔드포인트는 모두 authentication_classes=[] — 인증 불필요)
// ──────────────────────────────────────────────
export default function (data) {
  const { boothUuid, tableMaxCnt, menuIds, setIds } = data;
  const tableNum = randomInt(1, tableMaxCnt);

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
    check(res, { 'payment_info 200': (r) => r.status === 200 });
  });

  sleep(rand(1, 3));

  // ── Step 6. 결제 확인 (payment-confirm) ─────
  // 핵심 엔드포인트 — 주문 누락 원인 지점
  group('06_payment_confirm', () => {
    const start = Date.now();
    const res = http.post(
      `${BASE_URL}/api/v3/django/cart/payment-confirm/`,
      JSON.stringify({ table_usage_id: tableUsageId }),
      { headers: JSON_HEADERS },
    );
    paymentConfirmDuration.add(Date.now() - start);

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
// 유틸
// ──────────────────────────────────────────────
function rand(min, max)      { return Math.random() * (max - min) + min; }
function randomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
function pickRandom(arr)     { return arr[Math.floor(Math.random() * arr.length)]; }
