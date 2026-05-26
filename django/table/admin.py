from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import Table, TableGroup, TableUsage


class TableInline(admin.TabularInline):
    model = Table
    fields = ('table_num', 'status')
    extra = 0
    readonly_fields = ('table_num', 'status')
    can_delete = False
    show_change_link = True


class TableUsageInline(admin.TabularInline):
    model = TableUsage
    fields = ('started_at', 'ended_at', 'usage_minutes', 'accumulated_amount')
    extra = 0
    readonly_fields = ('started_at', 'ended_at', 'usage_minutes', 'accumulated_amount')
    can_delete = False
    ordering = ('-started_at',)


@admin.register(TableGroup)
class TableGroupAdmin(admin.ModelAdmin):
    list_display = ('pk', 'booth_name', 'representative_table_num', 'table_count', 'merged_at')
    list_filter = ('representative_table__booth__name',)
    search_fields = ('representative_table__booth__name',)
    readonly_fields = ('merged_at',)
    inlines = [TableInline]

    @admin.display(description='부스', ordering='representative_table__booth__name')
    def booth_name(self, obj):
        return obj.representative_table.booth.name if obj.representative_table else '-'

    @admin.display(description='대표 테이블', ordering='representative_table__table_num')
    def representative_table_num(self, obj):
        return obj.representative_table.table_num if obj.representative_table else '-'

    @admin.display(description='병합 테이블 수')
    def table_count(self, obj):
        return obj.tables.count()


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ('pk', 'booth_link', 'table_num', 'status_badge', 'group', 'current_amount')
    list_filter = ('status', 'booth__name')
    search_fields = ('booth__name', 'table_num')
    list_select_related = ('booth', 'group', 'group__representative_table')
    inlines = [TableUsageInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('booth', 'group').prefetch_related('usages')

    @admin.display(description='부스 (ID)', ordering='booth__name')
    def booth_link(self, obj):
        url = reverse('admin:booth_booth_change', args=[obj.booth.pk])
        return format_html('<a href="{}">[{}] {}</a>', url, obj.booth.pk, obj.booth.name)

    @admin.display(description='상태')
    def status_badge(self, obj):
        colors = {
            'AVAILABLE': '#28a745',
            'IN_USE': '#fd7e14',
            'INACTIVE': '#6c757d',
        }
        color = colors.get(obj.status, '#000')
        return format_html(
            '<span style="color:{}; font-weight:bold;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description='현재 누적금액')
    def current_amount(self, obj):
        usage = obj.usages.filter(ended_at__isnull=True).first()
        if usage:
            return f'{usage.accumulated_amount:,}원'
        return '-'


@admin.register(TableUsage)
class TableUsageAdmin(admin.ModelAdmin):
    list_display = (
        'pk', 'booth_link', 'table_num',
        'started_at', 'ended_at', 'usage_minutes',
        'accumulated_amount_display', 'order_count', 'is_active',
    )
    list_filter = ('table__booth__name',)
    search_fields = ('table__booth__name', 'table__table_num')
    readonly_fields = ('started_at',)
    list_select_related = ('table', 'table__booth')
    date_hierarchy = 'started_at'

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('orders')

    @admin.display(description='부스 (ID)', ordering='table__booth__name')
    def booth_link(self, obj):
        booth = obj.table.booth
        url = reverse('admin:booth_booth_change', args=[booth.pk])
        return format_html('<a href="{}">[{}] {}</a>', url, booth.pk, booth.name)

    @admin.display(description='테이블', ordering='table__table_num')
    def table_num(self, obj):
        return obj.table.table_num

    @admin.display(description='누적 금액', ordering='accumulated_amount')
    def accumulated_amount_display(self, obj):
        return f'{obj.accumulated_amount:,}원'

    @admin.display(description='주문 수')
    def order_count(self, obj):
        return obj.orders.count()

    @admin.display(description='사용중', boolean=True)
    def is_active(self, obj):
        return obj.ended_at is None
