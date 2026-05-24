import logging

from booth.models import Booth
from menu.models import Menu
from table.models import Table, TableGroup, TableUsage
from django.db import transaction
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


class BoothService:

    @staticmethod
    def create_booth_for_user(user, booth_data):
        """유저 생성 시 부스 데이터 만드는 함수
        Args:
            user (User): 유저 객체
            booth_data (dict): 부스 데이터
        Returns:
            생성한 Booth 객체
        """
        from table.models import Table

        logger.debug(
            "[BoothService.create_booth_for_user] Booth 생성 시도 | user_id=%s | name=%s | seat_type=%s | table_max_cnt=%s",
            user.id, booth_data.get('name'), booth_data.get('seat_type'), booth_data.get('table_max_cnt')
        )

        # Booth 객체 생성
        booth = Booth.objects.create(
            user=user,
            name=booth_data['name'],
            account=booth_data['account'],
            depositor=booth_data['depositor'],
            bank=booth_data['bank'],
            table_max_cnt=booth_data['table_max_cnt'],
            table_limit_hours=booth_data['table_limit_hours'],
            seat_type=booth_data['seat_type'],
            seat_fee_person=booth_data.get('seat_fee_person'),
            seat_fee_table=booth_data.get('seat_fee_table'),
        )
        logger.debug("[BoothService.create_booth_for_user] Booth 생성 완료 | booth_id=%s", booth.pk)

        # 테이블 이용료 메뉴 자동 생성
        if booth.seat_type == "PP":
            Menu.objects.create(
                booth=booth,
                name="테이블 이용료",
                category="FEE",
                description="인원 수",
                price=booth.seat_fee_person or 0,
                stock=9999
            )
            logger.debug("[BoothService.create_booth_for_user] FEE 메뉴 생성 (PP) | booth_id=%s | price=%s", booth.pk, booth.seat_fee_person)
        elif booth.seat_type == "PT":
            Menu.objects.create(
                booth=booth,
                name="테이블 이용료",
                category="FEE",
                description="테이블",
                price=booth.seat_fee_table or 0,
                stock=9999
            )
            logger.debug("[BoothService.create_booth_for_user] FEE 메뉴 생성 (PT) | booth_id=%s | price=%s", booth.pk, booth.seat_fee_table)
        else:
            Menu.objects.create(
                booth=booth,
                name="테이블 이용료",
                category="FEE",
                description="FREE",
                price=0,
                stock=9999
            )
            logger.debug("[BoothService.create_booth_for_user] FEE 메뉴 생성 (FREE) | booth_id=%s", booth.pk)

        # 테이블 생성
        logger.debug(
            "[BoothService.create_booth_for_user] 테이블 %s개 생성 시작 | booth_id=%s",
            booth.table_max_cnt, booth.pk
        )
        for i in range(1, booth.table_max_cnt + 1):
            Table.objects.create(
                booth=booth,
                table_num=i
            )
        logger.info(
            "[BoothService.create_booth_for_user] 완료 | booth_id=%s | tables=%s | seat_type=%s",
            booth.pk, booth.table_max_cnt, booth.seat_type
        )
        return booth

    @staticmethod
    def update_booth(booth, booth_data):
        """부스 마이페이지 데이터 업데이트 및 FEE 메뉴 동기화
        Args:
            booth_data (dict): 변경할 부스 데이터
        """
        # 기존 값 변경
        for key, value in booth_data.items():
            setattr(booth, key, value)
        booth.save()

        # FEE 메뉴 동기화
        fee_menu = Menu.objects.filter(booth=booth, category="FEE").first()
        if fee_menu:
            if booth.seat_type == "PP":
                fee_menu.price = booth.seat_fee_person or 0
                fee_menu.description = "인원 수"
            elif booth.seat_type == "PT":
                fee_menu.price = booth.seat_fee_table or 0
                fee_menu.description = "테이블"
            else:
                fee_menu.price = 0
                fee_menu.description = "FREE"
            fee_menu.save()

    @staticmethod
    @transaction.atomic
    def reset_booth_table_usage(booth):
        """부스의 모든 TableUsage 삭제 및 테이블 초기화
        Args:
            booth (Booth): 초기화할 부스

        Returns:
            int: 삭제된 TableUsage 개수

        Raises:
            ValidationError: IN_USE 상태의 테이블이 하나라도 있을 때
        """
        # 1. IN_USE 테이블 존재 여부 확인
        if Table.objects.filter(booth=booth, status=Table.Status.IN_USE).exists():
            raise ValidationError('사용 중인 테이블이 있어 초기화할 수 없습니다.')

        # 2. 모든 그룹 해제 및 삭제
        group_ids = list(
            TableGroup.objects
            .filter(tables__booth=booth)
            .values_list('pk', flat=True)
            .distinct()
        )
        if group_ids:
            Table.objects.filter(booth=booth).update(group=None)
            TableGroup.objects.filter(pk__in=group_ids).delete()

        # 3. Order 먼저 삭제 (Order.cart가 PROTECT이므로 Cart 삭제 전에 제거 필요)
        #    Order 삭제 시 OrderItem은 CASCADE로 자동 삭제됨
        from order.models import Order
        Order.objects.filter(table_usage__table__booth=booth).delete()

        # 4. 모든 TableUsage 삭제
        #    Cart.table_usage가 CASCADE이므로 Cart도 함께 삭제됨
        #    CartCouponApply도 Cart → CASCADE로 함께 삭제됨
        deleted_count, _ = TableUsage.objects.filter(table__booth=booth).delete()

        # 5. 이 부스의 쿠폰 코드 사용 이력 초기화
        #    결제 시 CouponCode.used_at이 설정되는데, 포맷 시 주문/카트는
        #    CASCADE로 삭제되지만 CouponCode.used_at은 직접 초기화해야 함
        from coupon.models import CouponCode
        CouponCode.objects.filter(
            coupon__booth=booth, used_at__isnull=False
        ).update(used_at=None)

        # 6. 커밋 후: 매출 캐시 무효화 + 총매출 0 WebSocket 전송
        def _send_ws_after_commit():
            try:
                from order.cache import invalidate_today_revenue
                invalidate_today_revenue(booth.pk)
            except Exception:
                logger.exception("[부스 초기화] 매출 캐시 무효화 실패")

            try:
                from channels.layers import get_channel_layer
                from asgiref.sync import async_to_sync
                channel_layer = get_channel_layer()
                group_name = f"booth_{booth.pk}.order"
                async_to_sync(channel_layer.group_send)(
                    group_name,
                    {"type": "total_sales_update", "data": {"today_revenue": 0}}
                )
            except Exception:
                logger.exception("[부스 초기화] 총매출 WebSocket 전송 실패")

        transaction.on_commit(_send_ws_after_commit)

        return deleted_count


class BoothStatisticsService:

    @staticmethod
    def get_statistics(booth):
        from django.db.models import Sum, Avg, Count
        from django.db.models.functions import TruncDate, ExtractHour
        from django.utils import timezone
        from order.models import Order, OrderItem
        from table.models import TableUsage

        tz = timezone.get_current_timezone()

        booth_orders = Order.objects.filter(
            table_usage__table__booth=booth
        ).exclude(order_status='CANCELLED')

        booth_items = OrderItem.objects.filter(
            order__table_usage__table__booth=booth
        ).exclude(status='CANCELLED').exclude(order__order_status='CANCELLED')

        # 총 주문 건수
        total_orders = booth_orders.count()

        # 평균 조리시간 (분)
        cooked = list(
            booth_items.filter(cooked_at__isnull=False)
            .values_list('created_at', 'cooked_at')
        )
        avg_cooking_minutes = (
            round(sum((c - a).total_seconds() for a, c in cooked) / len(cooked) / 60, 1)
            if cooked else None
        )

        # 평균 서빙시간 (분)
        served = list(
            booth_items.filter(served_at__isnull=False, status='SERVED')
            .values_list('created_at', 'served_at')
        )
        avg_serving_minutes = (
            round(sum((s - a).total_seconds() for a, s in served) / len(served) / 60, 1)
            if served else None
        )

        # 평균 테이블 이용시간 (분)
        usage_avg = TableUsage.objects.filter(
            table__booth=booth, usage_minutes__isnull=False
        ).aggregate(avg=Avg('usage_minutes'))
        avg_table_usage_minutes = (
            round(usage_avg['avg'], 1) if usage_avg['avg'] is not None else None
        )

        daily_revenue_map = {d: 0 for d in booth.location.keys()}
        for row in (
            booth_orders
            .annotate(order_date=TruncDate('created_at', tzinfo=tz))
            .values('order_date')
            .annotate(revenue=Sum('order_price'))
        ):
            key = row['order_date'].strftime('%Y-%m-%d')
            daily_revenue_map[key] = row['revenue'] or 0
        daily_revenue = [
            {'date': k, 'revenue': v} for k, v in daily_revenue_map.items()
        ]

        # 시간별 매출 (17~23시)
        HOURS = list(range(17, 24))
        hourly_map = {h: 0 for h in HOURS}
        for row in (
            booth_orders
            .annotate(hour=ExtractHour('created_at', tzinfo=tz))
            .filter(hour__in=HOURS)
            .values('hour')
            .annotate(revenue=Sum('order_price'))
        ):
            hourly_map[row['hour']] = row['revenue'] or 0
        hourly_revenue = [
            {'hour': f"{h:02d}:00", 'revenue': hourly_map[h]} for h in HOURS
        ]

        # 피크타임 (주문 건수 기준)
        peak_row = (
            booth_orders
            .annotate(hour=ExtractHour('created_at', tzinfo=tz))
            .values('hour')
            .annotate(cnt=Count('id'))
            .order_by('-cnt')
            .first()
        )
        peak_time = f"{peak_row['hour']:02d}:00" if peak_row else None

        menu_stats = BoothStatisticsService._get_menu_stats(booth)

        return {
            'booth_stats': {
                'total_orders': total_orders,
                'avg_cooking_minutes': avg_cooking_minutes,
                'avg_serving_minutes': avg_serving_minutes,
                'avg_table_usage_minutes': avg_table_usage_minutes,
                'table_count': booth.table_max_cnt,
                'total_revenue': booth.total_revenues,
                'daily_revenue': daily_revenue,
                'hourly_revenue': hourly_revenue,
                'peak_time': peak_time,
            },
            'menu_stats': menu_stats,
        }

    @staticmethod
    def _get_menu_stats(booth):
        from order.models import OrderItem

        base_qs = (
            OrderItem.objects.filter(
                order__table_usage__table__booth=booth,
                parent=None,
            )
            .exclude(status='CANCELLED')
            .exclude(order__order_status='CANCELLED')
            .select_related('menu', 'setmenu')
        )

        menu_data = {}

        for item in base_qs:
            if item.menu_id and item.menu.category == 'FEE':
                continue

            if item.menu_id:
                key = ('menu', item.menu_id)
                name = item.menu.name
                stock = item.menu.stock
            elif item.setmenu_id:
                key = ('set', item.setmenu_id)
                name = item.setmenu.name
                stock = None
            else:
                continue

            if key not in menu_data:
                menu_data[key] = {
                    'menu_id': item.menu_id,
                    'name': name,
                    'stock': stock,
                    'sold_quantity': 0,
                    'total_revenue': 0,
                    '_serving_secs': [],
                }

            menu_data[key]['sold_quantity'] += item.quantity
            menu_data[key]['total_revenue'] += item.fixed_price * item.quantity
            if item.served_at and item.status == 'SERVED':
                secs = (item.served_at - item.created_at).total_seconds()
                menu_data[key]['_serving_secs'].append(secs)

        result = []
        for data in menu_data.values():
            secs = data.pop('_serving_secs')
            data['avg_serving_minutes'] = (
                round(sum(secs) / len(secs) / 60, 1) if secs else None
            )
            result.append(data)

        with_stock = [m for m in result if m['stock'] is not None]
        with_serving = [m for m in result if m['avg_serving_minutes'] is not None]

        return {
            'least_sold': sorted(result, key=lambda m: m['sold_quantity'])[:5],
            'slowest_served': sorted(with_serving, key=lambda m: m['avg_serving_minutes'], reverse=True)[:5],
            'least_stock': sorted(with_stock, key=lambda m: m['stock'])[:5],
            'highest_revenue': sorted(result, key=lambda m: m['total_revenue'], reverse=True)[:5],
        }
