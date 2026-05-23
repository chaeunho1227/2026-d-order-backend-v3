import logging
from django.test import override_settings, TransactionTestCase
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from channels.testing import WebsocketCommunicator
from channels.routing import URLRouter
from channels.layers import get_channel_layer
from django.urls import re_path
from asgiref.sync import sync_to_async

from booth.models import Booth
from menu.models import Menu
from order.models import Order, OrderItem
from order.consumers import AdminOrderManagementConsumer, BoothSalesConsumer
from order.services import OrderService
from table.models import Table, TableUsage
from core.test_utils import IN_MEMORY_STORAGES, suppress_request_warnings

User = get_user_model()
logger = logging.getLogger(__name__)

SIGNUP_URL     = '/api/v3/django/auth/signup/'
ORDER_STATUS_URL = '/api/v3/django/order/status/'

VALID_SIGNUP_DATA = {
    "username": "testuser",
    "password": "testpass123",
    "booth_data": {
        "name": "테스트 부스",
        "table_max_cnt": 10,
        "account": "1234567890",
        "depositor": "홍길동",
        "bank": "신한은행",
        "seat_type": "NO",
        "seat_fee_person": 0,
        "seat_fee_table": 0,
        "table_limit_hours": 2.0,
    },
}

# ─── WebSocket Test App ────────────────────────────────────────────────────
WS_MGMT_URL  = '/ws/django/booth/orders/management/'
WS_SALES_URL = '/ws/django/booth/sales/'

WS_TEST_APP = URLRouter([
    re_path(r'^ws/django/booth/orders/management/$', AdminOrderManagementConsumer.as_asgi()),
    re_path(r'^ws/django/booth/sales/$', BoothSalesConsumer.as_asgi()),
])


async def _receive_until_type(communicator, target_type, max_attempts=10):
    """특정 타입의 메시지가 올 때까지 수신 (다른 타입은 skip)"""
    for _ in range(max_attempts):
        payload = await communicator.receive_json_from(timeout=3)
        if payload.get('type') == target_type:
            return payload
    raise AssertionError(f"'{target_type}' 메시지를 받지 못했습니다.")


# ─── REST API Tests ────────────────────────────────────────────────────────

@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class OrderItemStatusUpdateTestCase(APITestCase):
    """PATCH /api/v3/django/order/status/ 테스트"""

    def setUp(self):
        self.client = APIClient()
        self.client.post(SIGNUP_URL, VALID_SIGNUP_DATA, format='json')
        self.user = User.objects.get(username='testuser')
        self.booth = Booth.objects.get(user=self.user)
        self.client.force_authenticate(user=self.user)

        self.table = Table.objects.get(booth=self.booth, table_num=1)
        self.table.status = Table.Status.IN_USE
        self.table.save()
        self.usage = TableUsage.objects.create(table=self.table, started_at=now())
        self.menu = Menu.objects.create(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )
        self.order = Order.objects.create(
            table_usage=self.usage,
            order_price=8000, original_price=8000, order_status='PAID',
        )
        self.item = OrderItem.objects.create(
            order=self.order, menu=self.menu,
            quantity=2, fixed_price=4000, status='COOKING',
        )

    def test_COOKING_to_COOKED_성공(self):
        """COOKING → COOKED 상태 변경 성공, cooked_at 기록"""
        response = self.client.patch(ORDER_STATUS_URL, {
            'order_item_id': self.item.pk,
            'target_status': 'COOKED',
        }, format='json')

        logger.info("[StatusUpdate] COOKING→COOKED 응답: %s", response.data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'COOKED')
        self.assertIsNotNone(response.data['data']['cooked_at'])

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'COOKED')
        self.assertIsNotNone(self.item.cooked_at)

    def test_COOKED_to_SERVED_단일아이템_주문완료(self):
        """단일 아이템 SERVED → all_items_served=True, 주문 COMPLETED"""
        self.item.status = 'COOKED'
        self.item.save()

        response = self.client.patch(ORDER_STATUS_URL, {
            'order_item_id': self.item.pk,
            'target_status': 'SERVED',
        }, format='json')

        logger.info("[StatusUpdate] COOKED→SERVED 응답: %s", response.data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['all_items_served'])

        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, 'COMPLETED')

    def test_같은_상태로_변경시_400(self):
        """이미 COOKING 상태인 아이템을 COOKING으로 변경 → 400"""
        with suppress_request_warnings():
            response = self.client.patch(ORDER_STATUS_URL, {
                'order_item_id': self.item.pk,
                'target_status': 'COOKING',
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_유효하지않은_상태값_400(self):
        """유효하지 않은 target_status → 400"""
        with suppress_request_warnings():
            response = self.client.patch(ORDER_STATUS_URL, {
                'order_item_id': self.item.pk,
                'target_status': 'INVALID',
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_존재하지않는_아이템_404(self):
        """존재하지 않는 order_item_id → 404"""
        with suppress_request_warnings():
            response = self.client.patch(ORDER_STATUS_URL, {
                'order_item_id': 99999,
                'target_status': 'COOKED',
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_다른부스_아이템_변경시_403(self):
        """다른 부스 소유의 아이템 상태 변경 시도 → 403"""
        other_user = User.objects.create_user(username='other', password='pass')
        other_booth = Booth.objects.create(
            user=other_user, name='다른부스', account='0',
            depositor='x', bank='x', table_max_cnt=5,
            table_limit_hours=2, seat_type='NO',
        )
        other_table = Table.objects.create(booth=other_booth, table_num=1)
        other_usage = TableUsage.objects.create(table=other_table, started_at=now())
        other_menu = Menu.objects.create(booth=other_booth, name='다른메뉴', price=3000, stock=5)
        other_order = Order.objects.create(
            table_usage=other_usage,
            order_price=3000, original_price=3000, order_status='PAID',
        )
        other_item = OrderItem.objects.create(
            order=other_order, menu=other_menu,
            quantity=1, fixed_price=3000, status='COOKING',
        )

        with suppress_request_warnings():
            response = self.client.patch(ORDER_STATUS_URL, {
                'order_item_id': other_item.pk,
                'target_status': 'COOKED',
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_인증없이_요청시_401(self):
        """인증 없이 요청 → 401"""
        self.client.force_authenticate(user=None)
        with suppress_request_warnings():
            response = self.client.patch(ORDER_STATUS_URL, {
                'order_item_id': self.item.pk,
                'target_status': 'COOKED',
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class OrderItemCancelTestCase(APITestCase):
    """PATCH /api/v3/django/order/{id}/cancel/ 테스트"""

    def setUp(self):
        self.client = APIClient()
        self.client.post(SIGNUP_URL, VALID_SIGNUP_DATA, format='json')
        self.user = User.objects.get(username='testuser')
        self.booth = Booth.objects.get(user=self.user)
        self.client.force_authenticate(user=self.user)

        self.table = Table.objects.get(booth=self.booth, table_num=1)
        self.table.status = Table.Status.IN_USE
        self.table.save()
        self.usage = TableUsage.objects.create(
            table=self.table, started_at=now(), accumulated_amount=8000
        )
        self.menu = Menu.objects.create(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )
        self.order = Order.objects.create(
            table_usage=self.usage,
            order_price=8000, original_price=8000, order_status='PAID',
        )
        self.item = OrderItem.objects.create(
            order=self.order, menu=self.menu,
            quantity=2, fixed_price=4000, status='COOKING',
        )

    def _cancel_url(self, item_id):
        return f'/api/v3/django/order/{item_id}/cancel/'

    def test_전체수량_취소_아이템CANCELLED_주문CANCELLED(self):
        """전체 수량 취소 → 아이템 CANCELLED, 주문도 CANCELLED"""
        response = self.client.patch(self._cancel_url(self.item.pk), {
            'cancel_quantity': 2,
        }, format='json')

        logger.info("[Cancel] 전체취소 응답: %s", response.data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['remaining_quantity'], 0)
        self.assertEqual(response.data['data']['refund_amount'], 8000)

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'CANCELLED')

        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, 'CANCELLED')

    def test_부분수량_취소_수량감소_재고복구(self):
        """부분 수량 취소 → 수량 1 감소, 메뉴 재고 +1"""
        response = self.client.patch(self._cancel_url(self.item.pk), {
            'cancel_quantity': 1,
        }, format='json')

        logger.info("[Cancel] 부분취소 응답: %s", response.data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['remaining_quantity'], 1)
        self.assertEqual(response.data['data']['refund_amount'], 4000)

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)
        self.assertEqual(self.item.status, 'COOKING')

        self.menu.refresh_from_db()
        self.assertEqual(self.menu.stock, 11)

    def test_초과수량_취소시_400(self):
        """보유 수량 초과 취소 요청 → 400"""
        with suppress_request_warnings():
            response = self.client.patch(self._cancel_url(self.item.pk), {
                'cancel_quantity': 5,
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_이미_취소된_아이템_400(self):
        """CANCELLED 상태 아이템 취소 재시도 → 400"""
        self.item.status = 'CANCELLED'
        self.item.save()

        with suppress_request_warnings():
            response = self.client.patch(self._cancel_url(self.item.pk), {
                'cancel_quantity': 1,
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_존재하지않는_아이템_404(self):
        """존재하지 않는 아이템 ID → 404"""
        with suppress_request_warnings():
            response = self.client.patch(self._cancel_url(99999), {
                'cancel_quantity': 1,
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_다른부스_아이템_취소시_403(self):
        """다른 부스 소유의 아이템 취소 시도 → 403"""
        other_user = User.objects.create_user(username='other', password='pass')
        other_booth = Booth.objects.create(
            user=other_user, name='다른부스', account='0',
            depositor='x', bank='x', table_max_cnt=5,
            table_limit_hours=2, seat_type='NO',
        )
        other_table = Table.objects.create(booth=other_booth, table_num=1)
        other_usage = TableUsage.objects.create(
            table=other_table, started_at=now(), accumulated_amount=3000
        )
        other_menu = Menu.objects.create(booth=other_booth, name='다른메뉴', price=3000, stock=5)
        other_order = Order.objects.create(
            table_usage=other_usage,
            order_price=3000, original_price=3000, order_status='PAID',
        )
        other_item = OrderItem.objects.create(
            order=other_order, menu=other_menu,
            quantity=1, fixed_price=3000, status='COOKING',
        )

        with suppress_request_warnings():
            response = self.client.patch(self._cancel_url(other_item.pk), {
                'cancel_quantity': 1,
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_인증없이_요청시_401(self):
        """인증 없이 요청 → 401"""
        self.client.force_authenticate(user=None)
        with suppress_request_warnings():
            response = self.client.patch(self._cancel_url(self.item.pk), {
                'cancel_quantity': 1,
            }, format='json')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class TableOrderHistoryTestCase(APITestCase):
    """GET /api/v3/django/order/table/{table_usage_id}/ 테스트"""

    def setUp(self):
        self.client = APIClient()
        self.client.post(SIGNUP_URL, VALID_SIGNUP_DATA, format='json')
        self.user = User.objects.get(username='testuser')
        self.booth = Booth.objects.get(user=self.user)

        self.table = Table.objects.get(booth=self.booth, table_num=1)
        self.table.status = Table.Status.IN_USE
        self.table.save()
        self.usage = TableUsage.objects.create(table=self.table, started_at=now())
        self.menu = Menu.objects.create(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )

    def _history_url(self, usage_id):
        return f'/api/v3/django/order/table/{usage_id}/'

    def test_주문내역_정상조회(self):
        """활성 테이블 세션 주문 내역 조회 성공"""
        order = Order.objects.create(
            table_usage=self.usage,
            order_price=8000, original_price=8000, order_status='PAID',
        )
        OrderItem.objects.create(
            order=order, menu=self.menu,
            quantity=2, fixed_price=4000, status='COOKING',
        )

        response = self.client.get(self._history_url(self.usage.pk))

        logger.info("[History] 주문내역 응답: table_total=%s, orders=%s",
                    response.data['data']['table_total_price'],
                    len(response.data['data']['order_list']))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data['data']
        self.assertEqual(data['table_usage_id'], self.usage.pk)
        self.assertEqual(data['table_number'], '1')
        self.assertEqual(len(data['order_list']), 1)
        self.assertEqual(data['order_list'][0]['order_id'], order.pk)
        self.assertEqual(len(data['order_list'][0]['order_items']), 1)

    def test_주문없는_테이블_빈목록(self):
        """주문 없는 활성 세션 → order_list 빈 배열"""
        response = self.client.get(self._history_url(self.usage.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['order_list'], [])

    def test_존재하지않는_세션_404(self):
        """존재하지 않는 table_usage_id → 404"""
        with suppress_request_warnings():
            response = self.client.get(self._history_url(99999))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_종료된_세션_403(self):
        """ended_at 설정된 세션 → 403"""
        self.usage.ended_at = now()
        self.usage.save()

        with suppress_request_warnings():
            response = self.client.get(self._history_url(self.usage.pk))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_CANCELLED_주문_제외(self):
        """CANCELLED 상태 주문은 내역에서 제외"""
        Order.objects.create(
            table_usage=self.usage,
            order_price=4000, original_price=4000, order_status='CANCELLED',
        )

        response = self.client.get(self._history_url(self.usage.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['data']['order_list']), 0)

    def test_여러주문_금액_합산(self):
        """여러 주문의 order_price 합산이 table_total_price에 반영"""
        Order.objects.create(
            table_usage=self.usage,
            order_price=4000, original_price=4000, order_status='PAID',
        )
        Order.objects.create(
            table_usage=self.usage,
            order_price=6000, original_price=6000, order_status='PAID',
        )

        response = self.client.get(self._history_url(self.usage.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['table_total_price'], 10000)

    def test_인증없이_조회_가능(self):
        """인증 없이도 주문 내역 조회 가능 (AllowAny)"""
        response = self.client.get(self._history_url(self.usage.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)


# ─── WebSocket Tests ──────────────────────────────────────────────────────

@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class AdminOrderConsumerConnectTest(TransactionTestCase):
    """AdminOrderManagementConsumer 연결/인증 테스트"""

    def setUp(self):
        client = APIClient()
        client.post(SIGNUP_URL, VALID_SIGNUP_DATA, format='json')
        self.user = User.objects.get(username='testuser')
        self.booth = Booth.objects.get(user=self.user)

    async def test_인증없이_연결시_4001_거부(self):
        """scope에 user 없으면 4001로 연결 거부"""
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        connected, code = await communicator.connect()

        self.assertFalse(connected)
        self.assertEqual(code, 4001)

    async def test_booth없는_유저_4003_거부(self):
        """Booth가 없는 유저는 4003으로 연결 거부"""
        user_no_booth = await sync_to_async(User.objects.create_user)(
            username='nobooth', password='pass'
        )
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = user_no_booth

        connected, code = await communicator.connect()

        self.assertFalse(connected)
        self.assertEqual(code, 4003)

    async def test_정상_연결시_ADMIN_ORDER_SNAPSHOT_수신(self):
        """인증된 유저 연결 시 ADMIN_ORDER_SNAPSHOT 수신"""
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')
        logger.info("[ConnectTest] 스냅샷 수신: %d개 주문", len(response['data']['orders']))

        self.assertEqual(response['type'], 'ADMIN_ORDER_SNAPSHOT')
        self.assertIn('orders', response['data'])
        self.assertIn('timestamp', response)

        await communicator.disconnect()

    async def test_연결_후_MENU_AGGREGATION_수신(self):
        """연결 시 ADMIN_ORDER_SNAPSHOT 직후 MENU_AGGREGATION 수신"""
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user

        await communicator.connect()
        await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')

        response = await communicator.receive_json_from(timeout=3)
        logger.info("[ConnectTest] 두 번째 메시지 타입: %s", response['type'])
        self.assertEqual(response['type'], 'MENU_AGGREGATION')

        await communicator.disconnect()

    async def test_PING_전송시_PONG_응답(self):
        """클라이언트 PING → PONG 응답"""
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user
        await communicator.connect()
        await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')
        await communicator.receive_json_from(timeout=2)  # MENU_AGGREGATION 소비

        await communicator.send_json_to({'type': 'PING'})

        response = await communicator.receive_json_from(timeout=3)
        self.assertEqual(response['type'], 'PONG')

        await communicator.disconnect()

    async def test_알수없는_메시지_error_응답(self):
        """알 수 없는 메시지 전송 시 error 응답"""
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user
        await communicator.connect()
        await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')
        await communicator.receive_json_from(timeout=2)  # MENU_AGGREGATION 소비

        await communicator.send_json_to({'type': 'UNKNOWN_TYPE'})

        response = await communicator.receive_json_from(timeout=3)
        self.assertEqual(response['type'], 'error')

        await communicator.disconnect()


@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class AdminOrderConsumerEventTest(TransactionTestCase):
    """AdminOrderManagementConsumer 이벤트 핸들러 테스트 (group_send → 클라이언트 수신)"""

    def setUp(self):
        client = APIClient()
        client.post(SIGNUP_URL, VALID_SIGNUP_DATA, format='json')
        self.user = User.objects.get(username='testuser')
        self.booth = Booth.objects.get(user=self.user)

    async def _connect(self):
        """연결 후 초기 메시지(SNAPSHOT + MENU_AGGREGATION) 소비까지 처리"""
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user
        await communicator.connect()
        await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')
        await communicator.receive_json_from(timeout=2)  # MENU_AGGREGATION 소비
        return communicator

    async def _make_order(self):
        """테스트용 주문/아이템 생성 헬퍼"""
        table = await sync_to_async(
            Table.objects.get
        )(booth=self.booth, table_num=1)
        table.status = Table.Status.IN_USE
        await sync_to_async(table.save)()
        usage = await sync_to_async(TableUsage.objects.create)(
            table=table, started_at=now()
        )
        menu = await sync_to_async(Menu.objects.create)(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )
        order = await sync_to_async(Order.objects.create)(
            table_usage=usage,
            order_price=4000, original_price=4000, order_status='PAID',
        )
        item = await sync_to_async(OrderItem.objects.create)(
            order=order, menu=menu,
            quantity=1, fixed_price=4000, status='COOKING',
        )
        return order, item, usage

    async def test_admin_new_order_이벤트_수신(self):
        """group_send admin_new_order → ADMIN_NEW_ORDER"""
        order, _, _ = await self._make_order()
        communicator = await self._connect()
        channel_layer = get_channel_layer()

        await channel_layer.group_send(
            f'booth_{self.booth.pk}.order',
            {'type': 'admin_new_order', 'data': {'order_id': order.pk}},
        )

        response = await _receive_until_type(communicator, 'ADMIN_NEW_ORDER')
        logger.info("[EventTest] ADMIN_NEW_ORDER 수신: orders=%d",
                    len(response['data']['orders']))

        self.assertEqual(response['type'], 'ADMIN_NEW_ORDER')
        self.assertEqual(response['data']['orders'][0]['order_id'], order.pk)

        await communicator.disconnect()

    async def test_admin_order_update_이벤트_수신(self):
        """group_send admin_order_update → ADMIN_ORDER_UPDATE"""
        order, item, _ = await self._make_order()
        communicator = await self._connect()
        channel_layer = get_channel_layer()

        await channel_layer.group_send(
            f'booth_{self.booth.pk}.order',
            {
                'type': 'admin_order_update',
                'data': {
                    'order_id': order.pk,
                    'items': [{'order_item_id': item.pk, 'status': 'COOKED'}],
                },
            },
        )

        response = await _receive_until_type(communicator, 'ADMIN_ORDER_UPDATE')
        logger.info("[EventTest] ADMIN_ORDER_UPDATE 수신: order_id=%s",
                    response['data']['order_id'])

        self.assertEqual(response['data']['order_id'], order.pk)
        self.assertEqual(response['data']['items'][0]['status'], 'COOKED')

        await communicator.disconnect()

    async def test_admin_order_cancelled_이벤트_수신(self):
        """group_send admin_order_cancelled → ADMIN_ORDER_CANCELLED"""
        order, item, _ = await self._make_order()
        communicator = await self._connect()
        channel_layer = get_channel_layer()

        await channel_layer.group_send(
            f'booth_{self.booth.pk}.order',
            {
                'type': 'admin_order_cancelled',
                'data': {
                    'order_id': order.pk,
                    'item_id': item.pk,
                    'refund_amount': 4000,
                    'new_total_sales': 0,
                },
            },
        )

        response = await _receive_until_type(communicator, 'ADMIN_ORDER_CANCELLED')
        logger.info("[EventTest] ADMIN_ORDER_CANCELLED 수신: order_id=%s, refund=%s",
                    response['data']['order_id'], response['data']['refund_amount'])

        self.assertEqual(response['data']['order_id'], order.pk)
        self.assertEqual(response['data']['refund_amount'], 4000)

        await communicator.disconnect()

    async def test_admin_order_completed_이벤트_수신(self):
        """group_send admin_order_completed → ORDER_COMPLETED"""
        order, _, usage = await self._make_order()
        communicator = await self._connect()
        channel_layer = get_channel_layer()

        await channel_layer.group_send(
            f'booth_{self.booth.pk}.order',
            {
                'type': 'admin_order_completed',
                'data': {
                    'order_id': order.pk,
                    'table_num': 1,
                    'table_usage_id': usage.pk,
                    'order_status': 'COMPLETED',
                    'updated_at': now().isoformat(),
                },
            },
        )

        response = await _receive_until_type(communicator, 'ORDER_COMPLETED')
        logger.info("[EventTest] ORDER_COMPLETED 수신: order_id=%s", response['data']['order_id'])

        self.assertEqual(response['data']['order_id'], order.pk)
        self.assertEqual(response['data']['order_status'], 'COMPLETED')
        self.assertIn('table_usage_id', response['data'])

        await communicator.disconnect()

    async def test_다른_부스_이벤트는_수신_안됨(self):
        """다른 부스의 group_send는 수신하지 않음"""
        communicator = await self._connect()
        channel_layer = get_channel_layer()

        await channel_layer.group_send(
            'booth_99999.order',
            {'type': 'admin_new_order', 'data': {'order_id': 1}},
        )

        self.assertTrue(await communicator.receive_nothing(timeout=1))

        await communicator.disconnect()


@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class AdminOrderConsumerServiceIntegrationTest(TransactionTestCase):
    """Service → WebSocket 통합 테스트 (transaction.on_commit 포함)"""

    def setUp(self):
        client = APIClient()
        client.post(SIGNUP_URL, VALID_SIGNUP_DATA, format='json')
        self.user = User.objects.get(username='testuser')
        self.booth = Booth.objects.get(user=self.user)

    async def _connect(self):
        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user
        await communicator.connect()
        await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')
        await communicator.receive_json_from(timeout=2)  # MENU_AGGREGATION 소비
        return communicator

    async def test_아이템_상태변경시_ADMIN_ORDER_UPDATE_발송(self):
        """update_order_item_status → on_commit → ADMIN_ORDER_UPDATE 수신"""
        table = await sync_to_async(Table.objects.get)(booth=self.booth, table_num=1)
        table.status = Table.Status.IN_USE
        await sync_to_async(table.save)()
        usage = await sync_to_async(TableUsage.objects.create)(table=table, started_at=now())
        menu = await sync_to_async(Menu.objects.create)(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )
        order = await sync_to_async(Order.objects.create)(
            table_usage=usage,
            order_price=4000, original_price=4000, order_status='PAID',
        )
        item = await sync_to_async(OrderItem.objects.create)(
            order=order, menu=menu,
            quantity=1, fixed_price=4000, status='COOKING',
        )

        communicator = await self._connect()

        await sync_to_async(OrderService.update_order_item_status)(
            order_item_id=item.pk,
            target_status='COOKED',
            booth_id=self.booth.pk,
        )

        response = await _receive_until_type(communicator, 'ADMIN_ORDER_UPDATE')
        logger.info("[Integration] ADMIN_ORDER_UPDATE 수신: order_id=%s",
                    response['data']['order_id'])

        self.assertEqual(response['data']['order_id'], order.pk)

        await communicator.disconnect()

    async def test_단일아이템_SERVED시_ORDER_COMPLETED_발송(self):
        """단일 아이템 SERVED → ORDER_COMPLETED WebSocket 발송"""
        table = await sync_to_async(Table.objects.get)(booth=self.booth, table_num=1)
        table.status = Table.Status.IN_USE
        await sync_to_async(table.save)()
        usage = await sync_to_async(TableUsage.objects.create)(table=table, started_at=now())
        menu = await sync_to_async(Menu.objects.create)(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )
        order = await sync_to_async(Order.objects.create)(
            table_usage=usage,
            order_price=4000, original_price=4000, order_status='PAID',
        )
        item = await sync_to_async(OrderItem.objects.create)(
            order=order, menu=menu,
            quantity=1, fixed_price=4000, status='COOKED',
        )

        communicator = await self._connect()

        await sync_to_async(OrderService.update_order_item_status)(
            order_item_id=item.pk,
            target_status='SERVED',
            booth_id=self.booth.pk,
        )

        response = await _receive_until_type(communicator, 'ORDER_COMPLETED')
        logger.info("[Integration] ORDER_COMPLETED 수신: order_id=%s, status=%s",
                    response['data']['order_id'], response['data']['order_status'])

        self.assertEqual(response['data']['order_id'], order.pk)
        self.assertEqual(response['data']['order_status'], 'COMPLETED')

        await communicator.disconnect()

    async def test_아이템취소시_ADMIN_ORDER_CANCELLED_발송(self):
        """cancel_order_item → on_commit → ADMIN_ORDER_CANCELLED 수신"""
        table = await sync_to_async(Table.objects.get)(booth=self.booth, table_num=1)
        table.status = Table.Status.IN_USE
        await sync_to_async(table.save)()
        usage = await sync_to_async(TableUsage.objects.create)(
            table=table, started_at=now(), accumulated_amount=4000
        )
        menu = await sync_to_async(Menu.objects.create)(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )
        order = await sync_to_async(Order.objects.create)(
            table_usage=usage,
            order_price=4000, original_price=4000, order_status='PAID',
        )
        item = await sync_to_async(OrderItem.objects.create)(
            order=order, menu=menu,
            quantity=1, fixed_price=4000, status='COOKING',
        )

        communicator = await self._connect()

        await sync_to_async(OrderService.cancel_order_item)(
            order_item_id=item.pk,
            cancel_quantity=1,
            booth_id=self.booth.pk,
        )

        response = await _receive_until_type(communicator, 'ADMIN_ORDER_CANCELLED')
        logger.info("[Integration] ADMIN_ORDER_CANCELLED 수신: order_id=%s, refund=%s",
                    response['data']['order_id'], response['data']['refund_amount'])

        self.assertEqual(response['data']['order_id'], order.pk)
        self.assertEqual(response['data']['refund_amount'], 4000)

        await communicator.disconnect()


@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class AdminOrderSnapshotFilterTest(TransactionTestCase):
    """ADMIN_ORDER_SNAPSHOT 필터링 테스트"""

    def setUp(self):
        client = APIClient()
        client.post(SIGNUP_URL, VALID_SIGNUP_DATA, format='json')
        self.user = User.objects.get(username='testuser')
        self.booth = Booth.objects.get(user=self.user)

    async def test_PAID_주문만_스냅샷에_포함(self):
        """PAID 주문만 스냅샷에 포함, COMPLETED 주문 제외"""
        table = await sync_to_async(Table.objects.get)(booth=self.booth, table_num=1)
        table.status = Table.Status.IN_USE
        await sync_to_async(table.save)()
        usage = await sync_to_async(TableUsage.objects.create)(table=table, started_at=now())
        menu = await sync_to_async(Menu.objects.create)(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )

        paid_order = await sync_to_async(Order.objects.create)(
            table_usage=usage,
            order_price=4000, original_price=4000, order_status='PAID',
        )
        await sync_to_async(OrderItem.objects.create)(
            order=paid_order, menu=menu,
            quantity=1, fixed_price=4000, status='COOKING',
        )
        completed_order = await sync_to_async(Order.objects.create)(
            table_usage=usage,
            order_price=4000, original_price=4000, order_status='COMPLETED',
        )
        await sync_to_async(OrderItem.objects.create)(
            order=completed_order, menu=menu,
            quantity=1, fixed_price=4000, status='SERVED',
        )

        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user
        await communicator.connect()

        snapshot = await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')
        order_ids = [o['order_id'] for o in snapshot['data']['orders']]
        logger.info("[SnapshotFilter] 스냅샷 order_ids: %s", order_ids)

        self.assertIn(paid_order.pk, order_ids)
        self.assertNotIn(completed_order.pk, order_ids)

        await communicator.disconnect()

    async def test_종료된_테이블_주문은_스냅샷에서_제외(self):
        """ended_at 설정된 테이블의 주문은 스냅샷에서 제외"""
        table = await sync_to_async(Table.objects.get)(booth=self.booth, table_num=1)
        table.status = Table.Status.IN_USE
        await sync_to_async(table.save)()
        usage = await sync_to_async(TableUsage.objects.create)(table=table, started_at=now())
        menu = await sync_to_async(Menu.objects.create)(
            booth=self.booth, name='아메리카노', price=4000, stock=10
        )
        order = await sync_to_async(Order.objects.create)(
            table_usage=usage,
            order_price=4000, original_price=4000, order_status='PAID',
        )
        await sync_to_async(OrderItem.objects.create)(
            order=order, menu=menu,
            quantity=1, fixed_price=4000, status='COOKING',
        )

        # 테이블 세션 종료
        await sync_to_async(
            lambda: TableUsage.objects.filter(pk=usage.pk).update(ended_at=now())
        )()

        communicator = WebsocketCommunicator(WS_TEST_APP, WS_MGMT_URL)
        communicator.scope['user'] = self.user
        await communicator.connect()

        snapshot = await _receive_until_type(communicator, 'ADMIN_ORDER_SNAPSHOT')
        order_ids = [o['order_id'] for o in snapshot['data']['orders']]
        logger.info("[SnapshotFilter] 테이블 종료 후 스냅샷: %s", order_ids)

        self.assertNotIn(order.pk, order_ids)

        await communicator.disconnect()
