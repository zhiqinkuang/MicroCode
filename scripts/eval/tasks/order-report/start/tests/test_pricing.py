"""订单汇总的可见测试。"""
from src.report.line_items import LineItem, apply_discount, subtotal_before_discount_cents
from src.report.pricing import summarize_order
from src.report.tax import tax_cents

ITEMS = [
    LineItem(sku="A-1", quantity=2, unit_price_cents=5000),   # 100.00
    LineItem(sku="B-2", quantity=1, unit_price_cents=3000),   # 30.00
]


def test_subtotal_before_discount():
    assert subtotal_before_discount_cents(ITEMS) == 13000


def test_apply_discount_rounds_to_cents():
    assert apply_discount(13000, 0.15) == 11050


def test_tax_is_computed_on_the_discounted_amount():
    """税应当按折后小计算：13000 打 85 折 → 11050，税 6% → 663。"""
    discounted = apply_discount(13000, 0.15)
    assert tax_cents(discounted) == 663


def test_order_total_follows_the_documented_order():
    """
    业务口径：折扣先于税。
    total = 折后小计 + 按折后小计算的税 = 11050 + 663 = 11713
    """
    totals = summarize_order(ITEMS, discount_rate=0.15)
    assert totals.subtotal_cents == 11050
    assert totals.tax_cents == 663
    assert totals.total_cents == 11713


def test_order_without_discount_is_unchanged():
    """不打折时结果不该被这次修复改变。"""
    totals = summarize_order(ITEMS, discount_rate=0.0)
    assert totals.subtotal_cents == 13000
    assert totals.tax_cents == 780
    assert totals.total_cents == 13780
