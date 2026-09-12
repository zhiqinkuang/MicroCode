"""校验 images.py 的分支覆盖率下限。

coverage.py 的 --cov-fail-under 把行与分支机会合在一起算，无法单独约束分支，
所以 CI 在 `pytest --cov=images --cov-branch` 之后读 coverage.json 做精确门禁。

Run: uv run coverage json -o coverage.json && uv run python scripts/check_image_coverage.py
"""
import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("check_image_coverage")

MIN_BRANCH_PERCENT = 85.0


def branch_coverage(coverage_json: Path, target: str) -> tuple[int, int, float]:
    """返回 (已覆盖分支, 分支总数, 百分比)。"""
    data = json.loads(coverage_json.read_text(encoding="utf-8"))
    files = data.get("files", {})
    if target not in files:
        raise SystemExit(f"{coverage_json} 里没有 {target}：请先运行 pytest --cov=images 生成覆盖率数据")
    summary = files[target]["summary"]
    total = int(summary["num_branches"])
    covered = int(summary["covered_branches"])
    percent = 100.0 if total == 0 else covered * 100 / total
    return covered, total, percent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-json", type=Path, default=Path("coverage.json"))
    parser.add_argument("--target", default="images.py")
    parser.add_argument("--min-branch", type=float, default=MIN_BRANCH_PERCENT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    covered, total, percent = branch_coverage(args.coverage_json, args.target)
    logger.info("%s 分支覆盖率：%s/%s = %.1f%%（下限 %.1f%%）", args.target, covered, total, percent, args.min_branch)
    if percent < args.min_branch:
        logger.error("%s 分支覆盖率不足：%.1f%% < %.1f%%", args.target, percent, args.min_branch)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
