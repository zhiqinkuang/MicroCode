"""明细解析的可见测试：脏数据不该让报表挂掉。"""
from src.report.line_items import parse_line_items


def test_parse_skips_malformed_rows():
    raw = [
        {"sku": "A", "quantity": 1, "unit_price_cents": 100},
        {"sku": "B", "quantity": "x", "unit_price_cents": 100},   # 数量不是数字
        {"quantity": 1, "unit_price_cents": 100},                 # 缺 sku
        {"sku": "C", "quantity": 2, "unit_price_cents": 250},
    ]
    parsed = parse_line_items(raw)
    assert [item.sku for item in parsed] == ["A", "C"]


def test_parse_empty_input():
    assert parse_line_items([]) == []
