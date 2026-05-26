from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import Cart, CartItem


class CartItemInline(admin.TabularInline):
    model = CartItem
    fields = ('item_name', 'quantity', 'price_at_cart', 'created_at')
    readonly_fields = ('item_name', 'quantity', 'price_at_cart', 'created_at')
    extra = 0
    can_delete = False

    @admin.display(description='메뉴명')
    def item_name(self, obj):
        if obj.menu:
            return obj.menu.name
        if obj.setmenu:
            return f'[세트] {obj.setmenu.name}'
        return '-'


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'booth_link', 'table_num',
        'status', 'cart_price_display', 'round',
        'pending_expires_at', 'created_at',
    )
    list_filter = ('status', 'table_usage__table__booth__name')
    search_fields = ('id', 'table_usage__table__booth__name', 'table_usage__table__table_num')
    list_select_related = ('table_usage__table__booth',)
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'
    inlines = [CartItemInline]

    @admin.display(description='부스 (ID)', ordering='table_usage__table__booth__name')
    def booth_link(self, obj):
        try:
            booth = obj.table_usage.table.booth
            url = reverse('admin:booth_booth_change', args=[booth.pk])
            return format_html('<a href="{}">[{}] {}</a>', url, booth.pk, booth.name)
        except AttributeError:
            return '-'

    @admin.display(description='테이블', ordering='table_usage__table__table_num')
    def table_num(self, obj):
        try:
            return obj.table_usage.table.table_num
        except AttributeError:
            return '-'

    @admin.display(description='금액', ordering='cart_price')
    def cart_price_display(self, obj):
        return f'{obj.cart_price:,}원'


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'booth_link', 'table_num', 'cart_id',
        'item_name', 'quantity', 'price_at_cart_display', 'created_at',
    )
    list_filter = ('cart__table_usage__table__booth__name',)
    search_fields = ('cart__id', 'menu__name', 'setmenu__name', 'cart__table_usage__table__booth__name')
    list_select_related = ('cart__table_usage__table__booth', 'menu', 'setmenu')
    date_hierarchy = 'created_at'

    @admin.display(description='부스 (ID)', ordering='cart__table_usage__table__booth__name')
    def booth_link(self, obj):
        try:
            booth = obj.cart.table_usage.table.booth
            url = reverse('admin:booth_booth_change', args=[booth.pk])
            return format_html('<a href="{}">[{}] {}</a>', url, booth.pk, booth.name)
        except AttributeError:
            return '-'

    @admin.display(description='테이블', ordering='cart__table_usage__table__table_num')
    def table_num(self, obj):
        try:
            return obj.cart.table_usage.table.table_num
        except AttributeError:
            return '-'

    @admin.display(description='메뉴명')
    def item_name(self, obj):
        if obj.menu:
            return obj.menu.name
        if obj.setmenu:
            return f'[세트] {obj.setmenu.name}'
        return '-'

    @admin.display(description='단가', ordering='price_at_cart')
    def price_at_cart_display(self, obj):
        return f'{obj.price_at_cart:,}원'
