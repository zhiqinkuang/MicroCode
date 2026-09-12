"""单任务运行器：把一个任务交给 agent 跑完，产出一条结构化运行记录。

记录里存的是**原始事实**（测试结果、文件哈希、diff、事件流、token），不是判定结论。
判定交给 scripts/eval/judge.py 这个纯函数，于是判定器改了可以对历史记录**复判**，
不必重跑——重跑是要烧 token 的。

隔离：每次运行都在临时目录里建一份工作区与临时 HOME，agent 的会话/任务/检查点/
job 日志全部落在临时 HOME 下，不污染真实环境，也不受上一次运行影响。

Run:
  PYTHONPATH=. .venv/bin/python scripts/eval/run_task.py \
      --task scripts/eval/tasks/example-fix-addition \
      --version base --repeat 1 --out runs/
"""
import argparse
import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("eval.run_task")

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------- 任务夹具 ----------

def load_task(task_dir: Path) -> dict:
    """读 task.json 并做最低限度的校验：缺关键字段的夹具应当当场炸，而不是跑出无意义的记录。"""
    config = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    required = ("id", "description", "test_command")
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"{task_dir}/task.json 缺少必填字段：{', '.join(missing)}")
    if not (task_dir / "start").is_dir():
        raise ValueError(f"{task_dir}/start 不存在：夹具必须提供起始代码目录")
    return config


def resolve_command(command: list[str], workspace: Path) -> list[str]:
    """
    把 {python} 占位符换成当前解释器，并把 {workspace} 换成工作区绝对路径。

    用占位符而不是让夹具写死 python 路径：夹具不该假设自己跑在哪个解释器/目录下。
    优先用工作区自带的 .venv（真实项目夹具会带），否则退回当前解释器。
    """
    workspace_python = workspace / ".venv" / "bin" / "python"
    python = str(workspace_python) if workspace_python.exists() else sys.executable
    return [part.replace("{python}", python).replace("{workspace}", str(workspace)) for part in command]


# ---------- 快照与 diff ----------

# 这些是**运行过程自己产生**的产物，不是 agent 的改动。
# 必须排除：基线 pytest 一跑就会写出 .pytest_cache，不排除的话每次运行都会多出
# 「改了 .pytest_cache/... 」这类噪声，判定器会把它们当成越权改动而全部误判为 FAIL
# （实现时踩到过，由 end-to-end 用例抓出）。
IGNORED_PATH_PARTS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", ".eval-cache",
})


def _is_ignored(relative: str) -> bool:
    if relative.endswith((".pyc", ".pyo", ".log")):
        return True
    return any(part in IGNORED_PATH_PARTS for part in relative.split("/"))


@dataclass
class FileSnapshot:
    """
    单个文件在快照时刻的状态：哈希用于比对，内容用于事后生成 diff。

    为什么连内容一起存：diff 必须在**不依赖磁盘现状**的前提下算出来。
    只存哈希的话，事后去读磁盘拿「旧内容」是不可能的——文件已经被改了或删了。
    第一版就栽在这里：删除的文件永远生成不出 diff，因为读的是已经不存在的路径。
    """

    digest: str
    content: str


def snapshot(root: Path) -> dict[str, FileSnapshot]:
    """工作区的路径 → 文件快照。用于判定「改了哪些文件」和「只读文件有没有被动过」。"""
    files: dict[str, FileSnapshot] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _is_ignored(relative):
            continue
        raw = path.read_bytes()
        files[relative] = FileSnapshot(
            digest=hashlib.sha256(raw).hexdigest(),
            # 非 UTF-8 的二进制文件留空内容：它们进不了 diff，但哈希仍然参与比对
            content=_read_text(path),
        )
    return files


def diff_since(before: dict[str, FileSnapshot], after: dict[str, FileSnapshot]) -> tuple[list[str], str]:
    """
    返回 (变动路径列表, 统一 diff 文本)。

    路径列表包含新增与删除——「把测试删掉」正是要抓的作弊手法之一，
    只看修改过的文件会漏掉它。diff 完全由两份快照算出，不读磁盘。
    """
    changed = sorted(
        path for path in set(before) | set(after)
        if before.get(path, EMPTY).digest != after.get(path, EMPTY).digest
    )

    chunks = []
    for path in changed:
        old = before.get(path, EMPTY).content
        new = after.get(path, EMPTY).content
        chunks.append("".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        )))
    return changed, "".join(chunks)


# 不存在的文件用一个固定空快照占位，省掉到处 if path in before 的分支
EMPTY = FileSnapshot(digest="", content="")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ---------- 测试执行 ----------

def run_tests(command: list[str], workspace: Path, timeout: int) -> dict:
    """
    跑一次测试并解析结果。解析失败时只报 returncode，不猜 collected/failed——
    判定器会因为缺字段而拒绝判定，这比编一个数字安全。
    """
    try:
        completed = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"returncode": None, "timeout": True, "output_tail": f"测试超时（{timeout}s）"}
    except OSError as exc:
        return {"returncode": None, "error": f"{type(exc).__name__}: {exc}"}

    result = {"returncode": completed.returncode, "output_tail": (completed.stdout + completed.stderr)[-3000:]}
    result.update(parse_pytest_summary(completed.stdout))
    return result


def parse_pytest_summary(output: str) -> dict:
    """
    从 pytest 的汇总行里取 passed / failed / skipped / collected 计数。

    只信汇总行（形如 "2 failed, 1 passed in 0.02s"）——那是 pytest 自己的口径，
    比我们自己数用例行可靠。解析不到就返回空字典：判定器会因为缺字段而拒绝判定，
    这比编一个数字安全。
    """
    for line in reversed(output.strip().splitlines()):
        stripped = line.strip()
        if " in " not in stripped:
            continue
        counts = {}
        for word, key in (("passed", "passed"), ("failed", "failed"), ("error", "failed"), ("skipped", "skipped")):
            match = re.search(rf"(\d+)\s+{word}", stripped)
            if match:
                counts[key] = counts.get(key, 0) + int(match.group(1))
        if not counts:
            continue
        counts.setdefault("passed", 0)
        counts.setdefault("failed", 0)
        # collected = 通过 + 失败 + 跳过：删测试会让这个数变小，所以它必须来自实际结果而非
        # 「N passed」里的那个 N（"1 passed" 很可能只是「剩下 1 个通过」）
        counts["collected"] = counts["passed"] + counts["failed"] + counts.get("skipped", 0)
        return counts
    return {}


# ---------- 驱动 agent ----------

async def drive_agent(prompt: str, workspace: Path, model=None) -> dict:
    """
    在工作区里跑一轮 agent，收集事件流与用量。

    这里自己驱动 agent.iter 而不是调用 main.run_agent_loop：eval 需要拿到**每一步**的
    工具调用与返回（判定器要据此检测作弊），而 run_agent_loop 只把 part 打给终端。
    """
    from pydantic_ai import Agent
    from pydantic_graph import End
    from agent import agent as main_agent
    from agent.deps import AgentDeps
    from agent.file_state import ReadFileState
    from agent.iteration import IterationState
    from background_jobs import JobRegistry
    from tasks_store import TasksStore

    events: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    job_registry = JobRegistry(session_id="eval")

    deps = AgentDeps(
        read_file_state=ReadFileState(),
        tasks_store=TasksStore("eval"),
        job_registry=job_registry,
        iteration=IterationState(),
    )

    async with main_agent.iter(
        prompt, deps=deps, message_history=[], model=model,
        toolsets=[],
    ) as run:
        node = run.next_node
        while not isinstance(node, End):
            node = await run.next(node)
            if Agent.is_call_tools_node(node):
                for part in node.model_response.parts:
                    events.append({"kind": part.part_kind, "tool": getattr(part, "tool_name", None),
                                   "content": _short(getattr(part, "content", None) or getattr(part, "args", None))})
            elif Agent.is_model_request_node(node):
                for part in node.request.parts:
                    if part.part_kind == "tool-return":
                        events.append({"kind": "tool-return", "tool": part.tool_name, "content": _short(part.content)})

    result = run.result
    usage["input_tokens"] = result.usage.input_tokens
    usage["output_tokens"] = result.usage.output_tokens

    # 子代理的用量不在上面的 result.usage 里，必须把已完成 job 的用量并进来，
    # 否则 token 指标会系统性偏低（子代理恰是 token 大户）
    for job in job_registry.settle_usage():
        job_usage = job.usage
        usage["input_tokens"] += getattr(job_usage, "input_tokens", 0) or 0
        usage["output_tokens"] += getattr(job_usage, "output_tokens", 0) or 0

    await job_registry.aclose()
    return {
        "events": events,
        "usage": usage,
        "output": result.output,
        "interventions": deps.iteration.interventions,
        "new_messages": len(result.new_messages()),
    }


def _short(value, limit: int = 400) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "...(截断)"


# ---------- 主流程 ----------

# eval 里没有人在终端前点审批，权限模式必须显式给定。
# 默认 bypass：消融实验要量的三个机制里，权限分级本身就是被消融的变量之一，
# 用 default 会让 agent 卡在无人应答的审批上（实测症状：工具报「用户拒绝」然后任务空跑）。
# 想量权限机制本身时用 --permission-mode default 并配合自动应答（后续阶段做）。
DEFAULT_PERMISSION_MODE = "bypass"


def run_one(
    task_dir: Path,
    version: str,
    repeat: int,
    out_dir: Path,
    model=None,
    keep: bool = False,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> Path:
    import permissions

    permissions.state.mode = permission_mode
    permissions.state.session_allowed.clear()

    config = load_task(task_dir)
    sandbox = Path(tempfile.mkdtemp(prefix=f"eval-{config['id']}-"))
    workspace = sandbox / "workspace"
    shutil.copytree(task_dir / "start", workspace)

    # 隐藏用例放在工作区之外：模型既看不到也改不到，才能用来抓硬编码
    hidden_dir = None
    if (task_dir / "hidden").is_dir():
        hidden_dir = sandbox / "hidden"
        shutil.copytree(task_dir / "hidden", hidden_dir)

    started = time.monotonic()
    record = {
        "task": {
            "id": config["id"],
            "description": config["description"],
            "writable_paths": config.get("writable_paths"),
            "readonly_paths": config.get("readonly_paths", []),
            "baseline_failed": False,
            "baseline_collected": None,
        },
        "meta": {"version": version, "repeat": repeat, "model": _model_label(model),
                 "switches": _switches(), "permission_mode": permission_mode,
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        "run": {"changed_paths": [], "diff": "", "events": [], "error": None},
        "tests": {},
    }

    try:
        # 1. 基线：测试必须先失败，否则这个任务没有可验证的起点
        timeout = int(config.get("test_timeout_seconds", 300))
        baseline = run_tests(resolve_command(config["test_command"], workspace), workspace, timeout)
        record["task"]["baseline_collected"] = baseline.get("collected")
        record["task"]["baseline_failed"] = bool(baseline.get("failed")) or baseline.get("returncode") not in (0, None)
        if not record["task"]["baseline_failed"]:
            logger.warning("基线测试没有失败，任务 %s 的夹具可能无效", config["id"])

        before = snapshot(workspace)
        original_cwd = Path.cwd()

        # 2. 跑 agent
        os.chdir(workspace)
        agent_result = asyncio.run(drive_agent(config["description"], workspace, model=model))
        record["run"]["events"] = agent_result["events"]
        record["run"]["usage"] = agent_result["usage"]
        record["run"]["interventions"] = agent_result["interventions"]

        # 3. 最终测试
        final_tests = run_tests(resolve_command(config["test_command"], workspace), workspace, timeout)

        # 4. 隐藏用例：拷进工作区跑完立刻删掉。
        #    必须先跑完再算最终快照——否则隐藏用例文件会出现在 changed_paths 里，
        #    被判定器当成「改了白名单之外的路径」而误判（实现时踩到过）
        hidden_result = None
        if hidden_dir is not None and config.get("hidden_test_command"):
            for path in hidden_dir.rglob("*"):
                if path.is_file():
                    target = workspace / path.relative_to(hidden_dir)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            hidden_result = run_tests(
                resolve_command(config["hidden_test_command"], workspace), workspace, timeout
            )
            for path in hidden_dir.rglob("*"):
                if path.is_file():
                    (workspace / path.relative_to(hidden_dir)).unlink(missing_ok=True)

        after = snapshot(workspace)
        changed, diff_text = diff_since(before, after)
        record["run"]["changed_paths"] = changed
        record["run"]["diff"] = diff_text
        record["tests"] = final_tests
        if hidden_result is not None:
            record["run"]["hidden_tests"] = hidden_result
    except Exception as exc:  # noqa: BLE001 - 运行器不能因为 agent 或环境问题崩掉整轮实验
        record["run"]["error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("任务 %s 运行失败", config["id"])
    finally:
        record["meta"]["duration_seconds"] = round(time.monotonic() - started, 2)
        # 恢复调用前的 cwd：pytest 的 tmp_path 断言依赖 cwd，恢复成硬编码的仓库根会把它带偏
        os.chdir(original_cwd if "original_cwd" in dir() else REPO_ROOT)
        if not keep:
            shutil.rmtree(sandbox, ignore_errors=True)

    target_dir = out_dir / config["id"] / version
    target_dir.mkdir(parents=True, exist_ok=True)
    record_path = target_dir / f"{repeat}.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_path


def _model_label(model) -> str:
    if model is None:
        from agent.model import MODEL_NAME
        return MODEL_NAME
    return type(model).__name__


def _switches() -> dict:
    """记录本次运行生效的消融开关，便于事后核对记录属于哪个版本。"""
    return {
        "skills_disabled": os.environ.get("CODING_AGENT_DISABLE_SKILLS", "") not in ("", "0", "false"),
        "subagents_disabled": os.environ.get("CODING_AGENT_DISABLE_SUBAGENTS", "") not in ("", "0", "false"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True, help="任务夹具目录（含 task.json）")
    parser.add_argument("--version", default="default", help="版本标签，只用于目录与记录")
    parser.add_argument("--repeat", type=int, default=1, help="第几次重复，编号从 1 开始")
    parser.add_argument("--out", type=Path, default=Path("runs"), help="运行记录输出目录")
    parser.add_argument("--keep", action="store_true", help="保留临时工作区以便事后排查")
    parser.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE,
                        help="agent 的权限模式；eval 无人应答审批，默认 bypass")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    record_path = run_one(
        args.task, args.version, args.repeat, args.out,
        keep=args.keep, permission_mode=args.permission_mode,
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))

    from scripts.eval.judge import judge
    verdict = judge(record)
    print(f"{'PASS' if verdict['passed'] else 'FAIL'}  {record_path}")
    for reason in verdict["reasons"]:
        print(f"  - {reason}")
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
