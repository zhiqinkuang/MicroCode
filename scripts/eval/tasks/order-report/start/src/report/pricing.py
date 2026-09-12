"""订单汇总。

注意这里只做汇总，不管明细怎么算、税怎么算——那些在各自模块里。
"""
from dataclasses import dataclass

from .line_items import LineItem, apply_discount, subtotal_before_discount_cents
from .tax import DEFAULT_TAX_RATE, tax_cents


@dataclass(frozen=True)
class OrderTotals:
    subtotal_cents: int
    tax_cents: int
    total_cents: int


def summarize_order(
    items: list[LineItem],
    discount_rate: float = 0.0,
    tax_rate: float = DEFAULT_TAX_RATE,
) -> OrderTotals:
    """
    汇总一笔订单。

    报表口径见项目 README：total = 折后小计 + 税。
    """
    gross = subtotal_before_discount_cents(items)
    net = apply_discount(gross, discount_rate)
    tax = tax_cents(gross, tax_rate)
    return OrderTotals(subtotal_cents=net, tax_cents=tax, total_cents=net + tax)
