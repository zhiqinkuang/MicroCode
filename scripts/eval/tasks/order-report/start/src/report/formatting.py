"""报表格式化：金额分转成展示用的元。"""
from .pricing import OrderTotals


def format_cents(amount_cents: int) -> str:
    """整数分格式化成「¥1,234.56」。"""
    sign = "-" if amount_cents < 0 else ""
    whole, cents = divmod(abs(amount_cents), 100)
    return f"{sign}¥{whole:,}.{cents:02d}"


def format_totals(totals: OrderTotals) -> str:
    """把一笔订单的汇总格式化成一行。"""
    return (
        f"subtotal={format_cents(totals.subtotal_cents)} "
        f"tax={format_cents(totals.tax_cents)} "
        f"total={format_cents(totals.total_cents)}"
    )
