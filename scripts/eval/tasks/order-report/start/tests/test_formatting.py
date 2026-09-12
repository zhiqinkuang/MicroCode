"""格式化的可见测试。"""
from src.report.formatting import format_cents
from src.report.line_items import LineItem
from src.report.pricing import summarize_order
from src.report.formatting import format_totals


def test_format_cents():
    assert format_cents(123456) == "¥1,234.56"
    assert format_cents(0) == "¥0.00"
    assert format_cents(-5) == "-¥0.05"


def test_format_totals_line():
    totals = summarize_order([LineItem(sku="A", quantity=1, unit_price_cents=10000)], discount_rate=0.0)
    assert format_totals(totals) == "subtotal=¥100.00 tax=¥6.00 total=¥106.00"
