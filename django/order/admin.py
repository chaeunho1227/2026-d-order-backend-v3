from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fields = ('menu', 'setmenu', 'parent', 'quantity', 'fixed_price', 'status', 'cooked_at', 'served_at')
    readonly_fields = ('menu', 'setmenu', 'parent', 'quantity', 'fixed_price', 'status', 'cooked_at', 'served_at')
    extra = 0
    can_delete = False
    show_change_link = True


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'booth_link', 'table_num', 'order_price_display',
        'original_price_display', 'total_discount_display',
        'order_status', 'created_at',
    )
    list_filter = ('order_status', 'table_usage__table__booth__name')
    search_fields = ('id', 'table_usage__table__booth__name', 'table_usage__table__table_num')
    list_select_related = ('table_usage__table__booth',)
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'
    inlines = [OrderItemInline]

    @admin.display(description='부스 (ID)', ordering='table_usage__table__booth__name')
    def booth_link(self, obj):
        if not obj.table_usage:
            return '-'
        booth = obj.table_usage.table.booth
        url = reverse('admin:booth_booth_change', args=[booth.pk])
        return format_html('<a href="{}">[{}] {}</a>', url, booth.pk, booth.name)

    @admin.display(description='테이블', ordering='table_usage__table__table_num')
    def table_num(self, obj):
        return obj.table_usage.table.table_num if obj.table_usage else '-'

    @admin.display(description='결제금액', ordering='order_price')
    def order_price_display(self, obj):
        return f'{obj.order_price:,}원'

    @admin.display(description='원가', ordering='original_price')
    def original_price_display(self, obj):
        return f'{obj.original_price:,}원' if obj.original_price is not None else '-'

    @admin.display(description='할인', ordering='total_discount')
    def total_discount_display(self, obj):
        return f'{obj.total_discount:,}원' if obj.total_discount else '-'


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'booth_link', 'table_num', 'order_id',
        'item_name', 'quantity', 'fixed_price_display',
        'status', 'created_at', 'cooked_at', 'served_at',
    )
    list_filter = ('status', 'order__table_usage__table__booth__name')
    search_fields = ('order__id', 'menu__name', 'setmenu__name', 'order__table_usage__table__booth__name')
    list_select_related = ('order__table_usage__table__booth', 'menu', 'setmenu', 'parent')
    date_hierarchy = 'created_at'

    @admin.display(description='부스 (ID)', ordering='order__table_usage__table__booth__name')
    def booth_link(self, obj):
        try:
            booth = obj.order.table_usage.table.booth
            url = reverse('admin:booth_booth_change', args=[booth.pk])
            return format_html('<a href="{}">[{}] {}</a>', url, booth.pk, booth.name)
        except AttributeError:
            return '-'

    @admin.display(description='테이블', ordering='order__table_usage__table__table_num')
    def table_num(self, obj):
        try:
            return obj.order.table_usage.table.table_num
        except AttributeError:
            return '-'

    @admin.display(description='메뉴명')
    def item_name(self, obj):
        if obj.menu:
            return obj.menu.name
        if obj.setmenu:
            return f'[세트] {obj.setmenu.name}'
        return '-'

    @admin.display(description='단가', ordering='fixed_price')
    def fixed_price_display(self, obj):
        return f'{obj.fixed_price:,}원'
