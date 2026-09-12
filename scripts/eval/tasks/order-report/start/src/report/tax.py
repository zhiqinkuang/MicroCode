"""税与取整。"""
DEFAULT_TAX_RATE = 0.06


def tax_cents(amount_cents: int, rate: float = DEFAULT_TAX_RATE) -> int:
    """
    对给定金额算税，四舍五入到整数分。

    注意：这里的 amount 应该是**折后**小计。对未打折金额计税是本项目最常见的错误。
    """
    return round(amount_cents * rate)


def round_to_cents(value: float) -> int:
    """把可能带小数分的金额四舍五入到整数分。"""
    return round(value)
