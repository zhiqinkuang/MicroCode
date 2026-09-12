# 订单报表服务

按订单明细出日报、月报，并给会员折扣。

## 业务口径（重要）

- 金额一律用 **整数分** 表示，禁止浮点。这条在 `.my-claude-code/skills/money-rules/SKILL.md` 里有详细约定。
- **折扣先于税**：先按明细算出折后小计，再对折后小计计税。反过来算出来的总额是错的。
- 报表里的 `total` 必须是「折后小计 + 税」。

## 目录

- `src/report/line_items.py`  明细的解析与折后小计
- `src/report/tax.py`         税与取整
- `src/report/pricing.py`     订单汇总
- `src/report/formatting.py`  报表格式化
- `src/report/cli.py`         命令行入口
- `tests/`                    测试
