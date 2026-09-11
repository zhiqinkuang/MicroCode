"""
shell 工具：run_command（支持前台/后台）和 job_kill。

所有命令统一经 JobRegistry 起进程：后台任务不阻塞工具调用，
前台任务用户也能随时按 ctrl+b 转后台。
"""
import asyncio
import re
import time

import permissions
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from ..deps import AgentDeps

# 前台命令的等待上限，超过就终止并建议模型改用后台模式
FOREGROUND_TIMEOUT = 120


async def run_command(ctx: RunContext[AgentDeps], command: str, run_in_background: bool = False) -> str:
    """
    执行一条 shell 命令并返回输出。
    长驻或耗时命令（dev server、长测试）用 run_in_background=True 放到后台执行——只在不需要立刻拿到结果时使用，命令末尾不需要加 &。
    后台 job 结束后你会收到 <task-notification> 通知，所以不要主动轮询等待；
    期间可以用 read_file 读它的日志文件查看已有输出，也可以用 job_kill 提前终止。
    """
    registry = ctx.deps.job_registry
    # 所有 shell 命令都放进 job 注册表：用户随时能按 ctrl+b 把前台任务转入后台
    job = await registry.spawn_shell(command, background=run_in_background)
    if run_in_background:
        # 后台任务直接返回，不阻塞
        return _background_notice(job, f"已放入后台运行，job id: {job.id}")

    start = time.monotonic()
    # 每隔 50ms 轮询前台任务状态：循环退出除了进程结束，还有 job.background 被置 True（ctrl+b 转后台）
    while job.status == "running" and not job.background:
        if time.monotonic() - start > FOREGROUND_TIMEOUT:
            registry.kill(job.id)
            return f"[错误] 命令执行超时（{FOREGROUND_TIMEOUT}秒）；长驻或耗时命令请改用 run_in_background=True 重新执行"
        await asyncio.sleep(0.05)

    if job.background:
        # 用户用 ctrl+b 把前台任务转入了后台
        return _background_notice(job, f"用户把这条命令转入了后台，它作为 job {job.id} 继续运行")

    output = job.log_path.read_text(errors="replace")
    if job.returncode != 0:
        output += f"\n[错误] exit code {job.returncode}"
    return output or "(无输出)"


# 给模型看的工具返回内容：日志路径 + 后续动作引导
def _background_notice(job, lead: str) -> str:
    return (
        f"{lead}\n"
        f"输出日志：{job.log_path}（随时可用 read_file 查看）\n"
        "完成后你会收到 <task-notification> 通知，不要原地等待。"
    )


async def job_kill(ctx: RunContext[AgentDeps], job_id: str) -> str:
    """
    终止一个后台 job（shell 进程树整组终止 / subagent 协程取消）。只对还没结束的 job 有效。
    """
    registry = ctx.deps.job_registry
    job = registry.get(job_id)
    if job is None:
        raise ModelRetry(f"job {job_id} 不存在")
    if not registry.kill(job_id):
        return f"job {job_id} 已经结束（{job.status}），无需终止"
    return f"job {job_id} 已终止"


# 高危命令的特征：删除文件、提权、直写磁盘
# rm 用 lookbehind 排除 git rm / npm rm 这类包管理器子命令（它们前面会带 "git "/"npm "）
# find / 或 find ~ 是全盘/家目录搜索：在 agent 场景下几乎总是 LLM 在文件不存在时走偏，
# 命令本身慢、还会刷屏，拦下来让用户审批，用户能直接拒绝
DANGEROUS_PATTERNS = [
    r"(?<!git )(?<!npm )\brm\b",
    r"\bsudo\b",
    r"\bdd\b",
    r"\bmkfs\w*\b",
    r"\bfind\s+[~/]",
]
def run_command_self_check(args: dict):
    """
    run_command 的权限自检：扫一遍命令字符串，命中高危特征就要求审批。
    误伤的代价不过是多弹一次窗，绝不能把真正的高危命令漏过去。
    """
    command = args.get("command", "")
    if any(re.search(pattern, command) for pattern in DANGEROUS_PATTERNS):
        return "ask"
    # 没命中高危特征，交给通用规则决定
    return None


permissions.register_self_check("run_command", run_command_self_check)
