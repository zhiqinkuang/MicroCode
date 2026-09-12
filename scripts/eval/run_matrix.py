"""Eval 多版本编排器：把「夹具 × 版本 × 重复」全部跑一遍，产出可比较的汇总表。

版本定义就是三个开关的组合，没有别的魔法——它直接对应「同系统切开关」的消融设计：

  V0 base          : 无 skill、无 subagent、权限 bypass   ← 基线
  V1 skills-only   : 有 skill、无 subagent               ← 量 skill 的增量
  V2 subagent-only : 无 skill、有 subagent               ← 量上下文隔离的增量
  V3 full          : 全开                                 ← 量叠加效果

权限分级不在本阶段消融（它需要自动应答人工审批，是独立的一块），
所有版本统一 bypass，并把 mode 记进记录以便事后核对。

Run:
  PYTHONPATH=. .venv/bin/python scripts/eval/run_matrix.py \
      --task scripts/eval/tasks/example-fix-addition \
      --versions v0-base,v1-skills,v2-subagents,v3-full \
      --repeats 1 --out /tmp/eval-pilot
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("eval.run_matrix")

REPO_ROOT = Path(__file__).resolve().parents[2]

# 版本 → 生效的开关。用 dict 而不是把这些散落在命令行里：
# 消融矩阵是实验设计的一部分，必须能被一眼读出来、也必须只在一处定义。
VERSIONS: dict[str, dict[str, str]] = {
    "v0-base": {"CODING_AGENT_DISABLE_SKILLS": "1", "CODING_AGENT_DISABLE_SUBAGENTS": "1"},
    "v1-skills": {"CODING_AGENT_DISABLE_SKILLS": "0", "CODING_AGENT_DISABLE_SUBAGENTS": "1"},
    "v2-subagents": {"CODING_AGENT_DISABLE_SKILLS": "1", "CODING_AGENT_DISABLE_SUBAGENTS": "0"},
    "v3-full": {"CODING_AGENT_DISABLE_SKILLS": "0", "CODING_AGENT_DISABLE_SUBAGENTS": "0"},
}


def run_version(task: Path, version: str, repeats: int, out_dir: Path, permission_mode: str) -> list[Path]:
    """跑一个版本的全部重复次数，返回记录路径列表。"""
    switches = VERSIONS[version]
    records = []
    for repeat in range(1, repeats + 1):
        env = os.environ.copy()
        env.update(switches)
        command = [
            sys.executable, str(REPO_ROOT / "scripts" / "eval" / "run_task.py"),
            "--task", str(task), "--version", version, "--repeat", str(repeat),
            "--out", str(out_dir), "--permission-mode", permission_mode,
        ]
        logger.info("→ %s 第 %d/%d 次  switches=%s", version, repeat, repeats, switches)
        started = time.monotonic()
        completed = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
        elapsed = time.monotonic() - started
        record_path = out_dir / task.name / version / f"{repeat}.json"
        records.append(record_path)
        logger.info("  %s  用时 %.1fs", "记录已生成" if record_path.exists() else "记录缺失", elapsed)
        if completed.returncode not in (0, 1):
            # 0 = PASS、1 = FAIL，都不是运行器故障；其它返回码说明运行器本身炸了
            logger.error("  运行器异常退出 %d：%s", completed.returncode, completed.stderr[-500:])
    return records


def summarize(records: list[Path]) -> list[dict]:
    """把记录压成一行一行的可比数据。缺记录的格子如实标出来，不要静默跳过。"""
    from scripts.eval.judge import judge

    rows = []
    for path in records:
        if not path.exists():
            rows.append({"version": path.parent.name, "repeat": path.stem, "missing": True})
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        verdict = judge(record)
        usage = record["run"].get("usage", {})
        tool_calls = [e for e in record["run"].get("events", []) if e["kind"] == "tool-call"]
        rows.append({
            "version": record["meta"]["version"],
            "repeat": record["meta"]["repeat"],
            "passed": verdict["passed"],
            "reasons": verdict["reasons"],
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
            "tool_calls": len(tool_calls),
            "interventions": record["run"].get("interventions"),
            "changed": record["run"].get("changed_paths"),
            "duration": record["meta"].get("duration_seconds"),
            "switches": record["meta"].get("switches"),
            "error": record["run"].get("error"),
        })
    return rows


def print_table(rows: list[dict]) -> None:
    """
    逐次明细 + 按版本聚合。

    聚合必须给出**极差**而不只是均值：单看均值会把「同配置的抖动」误读成机制效应。
    极差是判断「这次测出的差异有没有意义」的第一道尺子——当版本间差异小于同版本内的
    极差时，这个差异什么都不说明。
    """
    for row in rows:
        if row.get("missing"):
            print(f"  缺记录：{row['version']} 第 {row['repeat']} 次")
        elif not row["passed"]:
            print(f"  FAIL  {row['version']} 第 {row['repeat']} 次：{'；'.join(row['reasons'])}")

    print()
    header = f"{'版本':<14}{'次':>3}{'通过':>5}{'输入tok均值':>12}{'总tok均值':>10}{'极差':>9}{'工具均值':>9}{'秒均值':>8}"
    print(header)
    print("-" * len(header))
    by_version: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("missing"):
            continue
        by_version.setdefault(row["version"], []).append(row)

    for version, group in by_version.items():
        total = [row["total_tokens"] for row in group]
        passed = sum(1 for row in group if row["passed"])
        mean_in = sum(row["input_tokens"] for row in group) / len(group)
        mean_total = sum(total) / len(group)
        spread = f"{max(total) - min(total):,}"
        mean_tools = sum(row["tool_calls"] for row in group) / len(group)
        mean_secs = sum(row["duration"] for row in group) / len(group)
        print(
            f"{version:<14}{len(group):>3}{passed:>5}{mean_in:>12,.0f}{mean_total:>10,.0f}"
            f"{spread:>9}{mean_tools:>9.1f}{mean_secs:>8.1f}"
        )
    if by_version:
        print()
        print("注：极差 = 同版本内最大与最小的总 token 之差。")
        print("    版本间差异若小于同版本内的极差，则该差异无法与模型随机性区分。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--versions", default=",".join(VERSIONS))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--permission-mode", default="bypass")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    unknown = [v for v in versions if v not in VERSIONS]
    if unknown:
        parser.error(f"未知版本 {unknown}；可用：{', '.join(VERSIONS)}")

    all_records: list[Path] = []
    started = time.monotonic()
    for version in versions:
        all_records.extend(run_version(args.task, version, args.repeats, args.out, args.permission_mode))

    rows = summarize(all_records)
    print()
    print(f"=== 汇总（{args.task.name}）===")
    print_table(rows)
    print()
    print(f"总用时 {time.monotonic() - started:.0f}s；记录目录 {args.out}")

    summary_path = args.out / f"summary-{args.task.name}.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总已写入 {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
