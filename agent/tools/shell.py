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
from ..iteration import IterationState

# 前台命令的等待上限，超过就终止并建议模型改用后台模式
FOREGROUND_TIMEOUT = 120


async def run_command(
    ctx: RunContext[AgentDeps],
    command: str,
    run_in_background: bool = False,
    verify: bool = False,
) -> str:
    """
    执行一条 shell 命令并返回输出。
    长驻或耗时命令（dev server、长测试）用 run_in_background=True 放到后台执行——只在不需要立刻拿到结果时使用，命令末尾不需要加 &。
    后台 job 结束后你会收到 <task-notification> 通知，所以不要主动轮询等待；
    期间可以用 read_file 读它的日志文件查看已有输出，也可以用 job_kill 提前终止。
    跑的如果是项目的测试 / 构建 / 类型检查这类验证命令，把 verify 设为 True：
    退出码会被记进闭环状态，通过之后这一轮才算验证完成；改了文件却从未通过验证，
    系统会要求你补跑一次。

    Args:
        command: 要执行的 shell 命令
        run_in_background: 是否放到后台执行
        verify: 本次执行是否是「验证」这一步（测试 / 构建 / 检查）
    """
    registry = ctx.deps.job_registry
    # 所有 shell 命令都放进 job 注册表：用户随时能按 ctrl+b 把前台任务转入后台
    job = await registry.spawn_shell(command, background=run_in_background)
    if run_in_background:
        # 后台任务直接返回，不阻塞。verify 的后台 job 也记账，但退出码要等它自己跑完，
        # 所以此刻仍算「未通过验证」——这正是提醒里引导模型去读日志的原因。
        result = _background_notice(
            job,
            f"已放入后台运行，job id: {job.id}",
            hint="这是你声明的验证命令：它跑完后用 read_file 读该日志，确认通过再收尾。" if verify else "",
        )
        if verify:
            run_verification(ctx, command, None, str(job.log_path))
        return result

    output, timed_out = await _wait_foreground(registry, job)
    if verify:
        # None 表示没拿到结论（超时被杀 / 用户转后台）：拿不到证据就不算验证通过
        run_verification(ctx, command, None if timed_out else job.returncode, output)
    if timed_out:
        return f"[错误] 命令执行超时（{FOREGROUND_TIMEOUT}秒）；长驻或耗时命令请改用 run_in_background=True 重新执行"

    if job.background:
        # 用户用 ctrl+b 把前台任务转入了后台
        return _background_notice(job, f"用户把这条命令转入了后台，它作为 job {job.id} 继续运行")
    return output or "(无输出)"


async def _wait_foreground(registry, job) -> tuple[str, bool]:
    """
    等一个前台命令结束。返回 (日志全文, 是否超时)。
    循环退出有三种可能：进程结束、超时被杀、job.background 被置 True（ctrl+b 转后台）。
    """
    start = time.monotonic()
    # 每隔 50ms 轮询一次：循环退出除了进程结束，还有 job.background 被置 True（ctrl+b 转后台）
    while job.status == "running" and not job.background:
        if time.monotonic() - start > FOREGROUND_TIMEOUT:
            registry.kill(job.id)
            return "", True
        await asyncio.sleep(0.05)
    output = job.log_path.read_text(errors="replace")
    if job.returncode:
        output += f"\n[错误] exit code {job.returncode}"
    return output, False


def run_verification(ctx: RunContext[AgentDeps], command: str, exit_code: int | None, output: str) -> None:
    """
    把一次验证结果记进闭环状态。系统记账，不依赖模型自己上报结论。
    注意这里不参与权限判定——它只写状态，真正执行命令的是上面的 run_command。
    """
    state: IterationState | None = ctx.deps.iteration
    if state is not None:
        state.mark_verification(command, exit_code, output)


# 给模型看的工具返回内容：日志路径 + 后续动作引导
def _background_notice(job, lead: str, hint: str = "") -> str:
    tail = f"{hint}\n" if hint else ""
    return (
        f"{lead}\n"
        f"{tail}"
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
