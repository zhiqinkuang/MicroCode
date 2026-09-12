"""
run_agent 工具：把任务委托给一个 sub agent，它在后台的 agent 型 job 里独立运行。

工具本身不改动任何东西，真正动系统的是 sub agent 后续的每一次工具调用——
权限在那一层把关（权限下沉），所以 run_agent 进只读白名单、常规派发免审批。
但「派发」这一步额外加了一道自检（run_agent_self_check）：带写能力的类型、
或带破坏性意图的 prompt 要用户点头。理由是子代理在后台跑，它要弹的审批得等主界面
空闲才冒泡，而派发那一刻才是用户在场、意图最清楚的时刻；错过这个时刻就只剩事后兜底。
"""
import re

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

import permissions

from ..deps import AgentDeps
from .shell import DANGEROUS_PATTERNS, _background_notice


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


# prompt 里出现这些词说明模型打算让子代理改动系统，而不是「看看代码」。
# 与 shell 的 DANGEROUS_PATTERNS 互补：那套只覆盖「删除/提权/直写磁盘」这类，
# 这里补两块它不认的意图——
# 1. 中文与英文的改写类动词（shell 那套是英文命令写法，认不出「删除这个文件」）；
# 2. 下载后执行的写法（curl/wget 拉东西回来再跑）。它比 rm 更隐蔽：shell 自检连
#    curl 都不拦（只认 rm/sudo/dd/mkfs/find ~），等于「拉外部代码来执行」这条
#    经典路径在派发时完全无感，所以在派发这一层单独兜一下。
_DESTRUCTIVE_PROMPT_WORDS = re.compile(
    r"删除|覆盖|清空|重置|回滚|去掉|清理|"
    r"\brm\b|\bdelete\b|\bremove\b|\boverwrite\b|\bclean\b|\breset\b|"
    r"\bchmod\b|\bchown\b|\btruncate\b|"
    r"\bcurl\b|\bwget\b",
    re.IGNORECASE,
)


def run_agent_self_check(args: dict):
    """
    run_agent 的权限自检：命中即要求审批（返回 "ask"），否则交给通用规则（返回 None）。

    两条触发条件，命中任一即审批：
    1. 目标类型带写能力（general 或自定义的写类型）——它后面一定会写盘；
    2. prompt 带破坏性意图（自然语言词表，或直接贴了高危命令）——即使类型声明为只读，
       也不能让删库指令静默跑进后台。
    只读类型的常规派发（「调查 X」「找出 Y 在哪」）仍然免审批：把免打扰路径也变成
    打扰路径，用户会转而常驻 bypass 模式，净安全收益为负。
    """
    # 函数内 import：subagents 顶层 import 了 agent 工具链，模块级 import 会循环依赖
    import subagents

    atype = subagents.get_agent_type(str(args.get("agent_type", "")))
    # 类型不存在时放行，让 run_agent 自己抛 ModelRetry 给出「可用类型」清单，避免两处重复报错
    if atype is not None and subagents.has_write_tools(atype.tool_names):
        return "ask"
    prompt = str(args.get("prompt", ""))
    if _DESTRUCTIVE_PROMPT_WORDS.search(prompt):
        return "ask"
    # prompt 里可能直接贴命令，shell 的高危特征也扫一遍
    if any(re.search(pattern, prompt) for pattern in DANGEROUS_PATTERNS):
        return "ask"
    return None


permissions.register_self_check("run_agent", run_agent_self_check)
