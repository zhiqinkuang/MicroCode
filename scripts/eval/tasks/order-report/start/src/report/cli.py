"""命令行入口：读一份订单 JSON，打印汇总。"""
import json
import sys

from .line_items import parse_line_items
from .formatting import format_totals
from .pricing import summarize_order


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("用法：python -m report.cli <orders.json>", file=sys.stderr)
        return 2
    payload = json.loads(open(argv[0], encoding="utf-8").read())
    items = parse_line_items(payload.get("items", []))
    totals = summarize_order(items, discount_rate=payload.get("discount_rate", 0.0))
    print(format_totals(totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
