"""明细行：解析与折后小计。"""
from dataclasses import dataclass

CENTS_PER_YUAN = 100


@dataclass(frozen=True)
class LineItem:
    """一条订单明细。单价与折后小计都用整数分。"""

    sku: str
    quantity: int
    unit_price_cents: int

    def subtotal_cents(self) -> int:
        """未打折的明细小计。"""
        return self.quantity * self.unit_price_cents


def parse_line_items(raw_items: list[dict]) -> list[LineItem]:
    """
    把接口原始数据转成明细。缺字段或类型不对的直接跳过——
    上游脏数据不该让整张报表挂掉，这是与财务对齐过的行为。
    """
    parsed = []
    for raw in raw_items:
        try:
            parsed.append(LineItem(
                sku=str(raw["sku"]),
                quantity=int(raw["quantity"]),
                unit_price_cents=int(raw["unit_price_cents"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return parsed


def subtotal_before_discount_cents(items: list[LineItem]) -> int:
    """所有明细的未打折小计之和。"""
    return sum(item.subtotal_cents() for item in items)


def apply_discount(amount_cents: int, discount_rate: float) -> int:
    """
    对金额打折，四舍五入到整数分。

    折扣率形如 0.15 表示 85 折。越界值按边界截断，不抛异常——
    上游传错折扣率时宁可少打折也不要让整张报表失败。
    """
    rate = min(max(discount_rate, 0.0), 1.0)
    return round(amount_cents * (1 - rate))
