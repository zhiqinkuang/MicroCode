"""
subagent 工具：run_subagent，把子任务委托给独立的子 agent。

子 agent 以后台 job 的形态运行：注册表、日志、<task-notification> 通知、
/jobs 面板、ctrl+b 转后台全部与 shell job 复用，模型和用户不需要学新概念。
"""
import asyncio

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from ..deps import AgentDeps
from ..subagent import SUBAGENTS, run_subagent_coro


async def run_subagent(
    ctx: RunContext[AgentDeps],
    task: str,
    agent_type: str = "explore",
    run_in_background: bool = False,
) -> str:
    """
    派一个子 agent 独立完成一个子任务，子 agent 有自己的上下文和精简工具集，不占用主对话历史。
    适合边界清晰的委托：调查代码、实现独立子功能、代码审查这类会产生大量中间过程、会污染主上下文的任务。
    task 要写得自包含：子 agent 看不到主对话历史，需要的背景、目标、验收标准都得写进去。

    Args:
        task: 子任务描述，自包含（子 agent 看不到当前对话）
        agent_type: 子 agent 类型：explore（只读调查）/ implement（独立实现）/ review（代码审查）
        run_in_background: 预计耗时长（如全库审查）时设 true，完成后会收到 <task-notification> 通知
    """
    spec = SUBAGENTS.get(agent_type)
    if spec is None:
        raise ModelRetry(f"未知的 agent_type：{agent_type}，可选 {' / '.join(SUBAGENTS)}")

    registry = ctx.deps.job_registry
    job = registry.spawn_agent(
        description=f"[{spec.name}] {task}",
        make_run=lambda log_path: run_subagent_coro(spec, task, log_path, deps=ctx.deps),
        background=run_in_background,
    )
    if run_in_background:
        return (
            f"subagent[{spec.name}] 已放入后台运行，job id: {job.id}\n"
            f"过程日志：{job.log_path}（随时可用 read_file 查看进度）\n"
            "完成后你会收到 <task-notification> 通知，不要原地等待。"
        )

    # 前台：轮询等待。循环退出的原因除了跑完，还有 job.background 被置 True（用户 ctrl+b 转后台）
    while job.status == "running" and not job.background:
        await asyncio.sleep(0.1)

    if job.background:
        return (
            f"用户把这个 subagent 转入了后台，它作为 job {job.id} 继续运行\n"
            f"过程日志：{job.log_path}（随时可用 read_file 查看进度）\n"
            "完成后你会收到 <task-notification> 通知，不要原地等待。"
        )
    if job.status == "killed":
        return f"subagent job {job.id} 已被终止，没有产出结论"
    if job.status == "failed":
        # 失败详情（异常栈）已由注册表写进日志末尾
        tail = job.log_path.read_text(errors="replace")[-1500:]
        return f"subagent job {job.id} 运行失败，日志末尾如下：\n{tail}"

    # 前台跑完：把日志里的「最终结论」段返回给主 agent
    text = job.log_path.read_text(errors="replace")
    marker = "## 最终结论"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    # 没有结论段（异常兜底）时给日志末尾
    return text[-3000:]
