"""
run_agent 工具：把任务委托给一个 sub agent，它在后台的 agent 型 job 里独立运行。

工具本身不改动任何东西，真正动系统的是 sub agent 后续的每一次工具调用——
权限在那一层把关（权限下沉），所以 run_agent 进只读白名单免审批。
"""
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from ..deps import AgentDeps
from .shell import _background_notice


async def run_agent(
    ctx: RunContext[AgentDeps],
    description: str,
    prompt: str,
    agent_type: str = "general",
) -> str:
    """
    把任务委托给一个 sub agent，它在全新的隔离上下文里工作，只把最终报告交回给你。
    sub agent 一律在后台运行：派出后立即返回，不要轮询等待，
    完成通知会在 <result> 字段附带它的最终报告；期间可用 read_file 读它的日志文件看进度。

    Args:
        description: 一句话任务描述，/jobs 面板和通知里展示用
        prompt: 任务描述，要自包含——sub agent 对当前对话一无所知，需要的背景、目标、验收标准都得写进去
        agent_type: sub agent 类型（explore 只读调查 / general 通用，另有项目自定义类型）
    """
    # 函数内 import：subagents 顶层 import 了 agent 工具链，模块级 import 会循环依赖
    import subagents

    atype = subagents.get_agent_type(agent_type)
    if atype is None:
        available = ", ".join(t.name for t in subagents.list_agent_types())
        raise ModelRetry(f"agent 类型 {agent_type} 不存在，可用类型：{available}")

    registry = ctx.deps.job_registry
    job = await registry.spawn_agent(
        description,
        lambda job: subagents.run_subagent(atype, prompt, job, ctx.deps),
    )
    return _background_notice(
        job,
        f"sub agent 已在后台开始工作，job id: {job.id}，"
        "完成通知会在 <result> 字段附带它的最终报告",
    )
