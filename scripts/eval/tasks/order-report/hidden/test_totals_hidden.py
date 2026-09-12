"""隐藏用例：模型看不到，用来抓「只对可见用例硬编码」。

覆盖可见用例没测到的量级、边界与取整，尤其是「折扣恰好把金额打成整数分」的情形。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.report.line_items import LineItem, apply_discount  # noqa: E402
from src.report.pricing import summarize_order  # noqa: E402
from src.report.tax import tax_cents  # noqa: E402


def _totals(unit_price_cents, quantity=1, discount_rate=0.0):
    return summarize_order([LineItem(sku="X", quantity=quantity, unit_price_cents=unit_price_cents)],
                           discount_rate=discount_rate)


def test_no_discount_matches_gross():
    totals = _totals(12345)
    assert totals.subtotal_cents == 12345
    assert totals.tax_cents == tax_cents(12345)
    assert totals.total_cents == 12345 + tax_cents(12345)


def test_discount_applied_before_tax():
    totals = _totals(10000, discount_rate=0.10)
    assert totals.subtotal_cents == 9000
    assert totals.tax_cents == tax_cents(9000) == 540
    assert totals.total_cents == 9540


def test_discount_that_produces_fractional_cents():
    """折扣后出现小数分：必须在折后步骤取整，而不是拖到最后。"""
    totals = _totals(9999, discount_rate=0.075)
    expected_net = apply_discount(9999, 0.075)
    assert totals.subtotal_cents == expected_net
    assert totals.tax_cents == tax_cents(expected_net)
    assert totals.total_cents == expected_net + tax_cents(expected_net)


def test_full_discount_yields_only_zero_tax():
    totals = _totals(5000, discount_rate=1.0)
    assert totals.subtotal_cents == 0
    assert totals.tax_cents == 0
    assert totals.total_cents == 0


def test_large_order_stays_integer_cents():
    """大额订单不能出现浮点误差。"""
    totals = _totals(987654, quantity=7, discount_rate=0.33)
    assert isinstance(totals.total_cents, int)
    expected_net = apply_discount(987654 * 7, 0.33)
    assert totals.total_cents == expected_net + tax_cents(expected_net)


def test_multi_item_order_with_discount():
    items = [
        LineItem(sku="A", quantity=3, unit_price_cents=1999),
        LineItem(sku="B", quantity=2, unit_price_cents=450),
    ]
    totals = summarize_order(items, discount_rate=0.2)
    gross = 3 * 1999 + 2 * 450
    expected_net = apply_discount(gross, 0.2)
    assert totals.subtotal_cents == expected_net
    assert totals.total_cents == expected_net + tax_cents(expected_net)
