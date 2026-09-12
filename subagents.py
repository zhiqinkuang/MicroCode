"""
sub agent 模块：agent 类型定义（内置 + 自定义）、sub agent 专用 hooks，以及在 agent 型 job 里运行 sub agent 的执行器。

sub agent 就是第二个 Agent 实例：全新上下文（不传 message_history）、独立的 system prompt、
裁剪过的工具集——工具集里没有 run_agent，天然不能再套娃。
"""
import asyncio
import dataclasses
import logging
import os
import platform
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.usage import UsageLimits
from pydantic_graph import End

import classifier
import permissions
from agent.deps import AgentDeps
from agent.file_state import ReadFileState
from agent.hooks import _handle_tool_error, _retry_on_error
from agent.model import model
from agent.reminders import build_job_reminder_text
from agent.tools.file import edit_file, read_file, write_file
from agent.tools.shell import run_command
from background_jobs import Job, JobRegistry

logger = logging.getLogger(__name__)

# 单个 sub agent 最多允许的模型请求次数，防止跑飞空转
MAX_SUBAGENT_REQUESTS = 40

# 自定义 agent 的定义目录（项目级），一个 .md 文件定义一个 agent
AGENTS_DIR = Path(".my-claude-code/agents")


# ---------- sub agent 专用 hooks ----------

sub_hooks = Hooks()


@sub_hooks.on.tool_execute
async def _check_sub_permission(ctx, *, call, tool_def, args, handler):
    """
    sub agent 的权限关卡：权限下沉——审批不看「谁派的活」，只看「实际要干什么」。
    第 0 层只读强制；第一层规则放行的照常放行；auto 模式交给 classifier 自动审批；
    需要人工审批的调用不直接弹窗（sub agent 在后台运行，弹窗时机随机会打断用户），
    而是冒泡进队列，等主界面空闲时再由 watch_approvals 弹给用户，sub agent 挂起等待答复。
    """
    # 第 0 层：只读子代理的写调用直接回填可操作说明。
    # 权威强制在 file 工具里（hook 被拆掉也依然生效）；这里再拦一次是因为工具抛的
    # ModelRetry 会被通用错误处理器转成「工具执行出错」，子代理拿不到那句「请改用只读方式」，
    # 容易反复重试同一个写操作。
    # getattr 兜底：与 agent/hooks.py 里取 iteration 的写法一致，deps 没这个字段时按可写处理
    # （权威强制在 file 工具里，那里 deps 一定是真 AgentDeps，不依赖这次兜底）
    if getattr(ctx.deps, "readonly", False) and call.tool_name in WRITE_TOOL_NAMES:
        return (
            f"当前是只读子代理，禁止调用 {call.tool_name}。"
            "请改用只读方式完成任务，或在最终报告里说明这一步没有做。"
        )
    # 第一层：规则能直接放行的（只读工具、bypass 模式等）照常放行
    if permissions.compute_decision(call.tool_name, args) == "allow":
        return await handler(args)
    # 第二层：auto 模式先交给 classifier 审查，它本来就是替用户做决定的自动审批官
    if permissions.state.mode == permissions.AUTO:
        verdict = await classifier.classify(ctx.messages, call.tool_name, args)
        if not verdict.get("error"):
            if not verdict["should_block"]:
                return await handler(args)
            return (
                f"安全检查拦截了这次 {call.tool_name} 调用，没有执行。"
                f"拦截理由：{verdict['reason']}。不要尝试绕过拦截，"
                "请换别的方式完成任务，或在最终报告里说明这一步没有做。"
            )
    # 第三层：需要人工审批，排队冒泡给用户，挂起等待答复
    choice = await _request_user_approval(ctx.deps.subagent_job, call.tool_name, args)
    if choice == "always":
        permissions.state.session_allowed.add(call.tool_name)
    if choice in ("once", "always"):
        return await handler(args)
    return (
        f"用户拒绝了这次 {call.tool_name} 调用。"
        "请换不需要审批的方式完成任务，或在最终报告里说明这一步没有做。"
    )


# 复用主 agent 的错误兜底和请求重试：sub agent 的工具出错、网络抖动同样要兜住
sub_hooks.on.tool_execute_error(_handle_tool_error)
sub_hooks.on.model_request(_retry_on_error)


@sub_hooks.on.before_model_request
async def _inject_sub_job_reminder(ctx, request_context):
    """
    sub agent 自己起的后台命令，完成通知搭它下一次模型请求的车送到它面前
    （用的是它自己的 job 注册表，和主会话的通知互不干扰）。
    """
    if ctx.deps.job_registry is None:
        return request_context
    text = build_job_reminder_text(ctx.deps.job_registry)
    if not text:
        return request_context
    reminder = ModelRequest(parts=[UserPromptPart(content=text)])
    messages = list(request_context.messages) + [reminder]
    return dataclasses.replace(request_context, messages=messages)


# ---------- 审批冒泡 ----------

@dataclass
class PendingApproval:
    job: Job
    tool_name: str
    args: dict
    # 答复通过 future 回填，挂起等待的 sub agent 由此恢复
    future: asyncio.Future


# 排队等用户答复的审批请求
PENDING_APPROVALS: list[PendingApproval] = []


async def _request_user_approval(job: Job, tool_name: str, args: dict) -> str:
    """
    把审批请求排进队列，await 一个没人 set 的 future——sub agent 的这次工具调用就此挂起，
    不烧 token、不占资源，安静等 watch_approals 弹窗拿回用户答复。
    """
    future = asyncio.get_running_loop().create_future()
    request = PendingApproval(job, tool_name, args, future)
    PENDING_APPROVALS.append(request)
    try:
        return await future
    finally:
        # job_kill、切换会话和退出都可能取消等待，不能再弹旧任务的审批。
        if request in PENDING_APPROVALS:
            PENDING_APPROVALS.remove(request)


def pop_pending_approval() -> PendingApproval | None:
    # watch_approvals 空闲时来取：先进先出
    while PENDING_APPROVALS:
        request = PENDING_APPROVALS.pop(0)
        if not request.future.done() and request.job.status == "running":
            return request
    return None


# ---------- agent 类型定义 ----------

@dataclass
class AgentType:
    name: str
    # 给主 agent 看的「什么时候用这个类型」
    description: str
    tool_names: list[str]
    # sub agent 的 system prompt，完全替换主 agent 的那份，不是追加
    instructions: str
    # "built-in" 或自定义 agent 的定义文件路径
    source: str
    # 只读类型：工具表里不挂写文件工具，且运行时由 file 工具硬拒（deps.readonly）。
    # 内置 explore 显式声明；自定义类型按工具表推导，见 load_custom_agents
    readonly: bool = False
    # 对应的 Agent 实例
    agent: Agent = field(init=False, repr=False)


_TYPES: dict[str, AgentType] = {}


# sub agent 可用的全部工具；run_agent、ask_user_question、task_*、job_kill 都被排除在外：
# run_agent 不给 = 委派只有一层，防止套娃烧 token；ask_user_question 不给 = 独立干活不回头找用户；
# task_* 是主会话的 UI 概念；job_kill 不需要——它起的后台命令会在任务结束时统一清理
_TOOL_FUNCS = {
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "run_command": run_command,
}

# 哪些工具属于「写」：派发自检、只读推导、只读类型的运行时强制都以它为准，口径只留这一处。
# run_command 刻意不算——它可能只是 cat/grep，算成「写能力」会把 explore 的常规派发也变成要审批
WRITE_TOOL_NAMES = frozenset({"edit_file", "write_file"})


def has_write_tools(tool_names) -> bool:
    """这组工具里是否包含写文件能力。派发自检与只读推导共用它，避免两处各写一遍判定。"""
    return bool(set(tool_names) & WRITE_TOOL_NAMES)


def _build_tools(tool_names: list[str]) -> list:
    # edit_file / write_file 标记 sequential=True，理由和主 TOOLS 一致：并发写盘会互相覆盖
    tools = []
    for name in tool_names:
        fn = _TOOL_FUNCS.get(name)
        if fn is None:
            continue
        tools.append(Tool(fn, sequential=True) if name in ("edit_file", "write_file") else fn)
    return tools


EXPLORE_INSTRUCTIONS = (
    "你是一个只读的代码调查 agent，负责在项目里搜索、阅读代码，回答交给你的问题。\n"
    "严禁做任何修改：不要创建或改动文件，run_command 只用于查看和搜索"
    "（ls、grep、find、cat 这类），不要执行有副作用的命令。\n"
    "调查完成后输出一份简洁的报告：结论放前面，附上关键文件路径作为依据。"
    "调用方只能看到你最后输出的报告，看不到中间过程，所以报告要自包含。"
)

GENERAL_INSTRUCTIONS = (
    "你是主 agent 派出的 sub agent，独立完成交给你的任务，可以读写文件和执行命令。\n"
    "修改已有文件前必须先用 read_file 读取它。改动局部内容时优先用 edit_file，"
    "新建文件或整体重写才用 write_file。\n"
    "没有人会回答你的提问，拿不准时按最合理的方式处理，并在报告里说明你的取舍。\n"
    "完成后输出一份简洁的最终报告：做了什么、改了哪些文件、关键发现。"
    "调用方只能看到这份报告，看不到中间过程。"
)


def _find_agents_md(start_dir: str) -> str | None:
    # 和 agent/core.py 的同名实现一致；subagents 不便反向 import core（会循环依赖），复制一份
    p = Path(start_dir).resolve()
    while True:
        candidate = p / "AGENTS.md"
        if candidate.is_file():
            return str(candidate)
        if p.parent == p:
            return None
        p = p.parent


def _sub_env_context() -> str:
    # sub agent 的动态环境信息：工作目录 / 日期 / 项目 AGENTS.md。
    # 不带主 agent 的记忆索引和 task 引导——sub agent 用不上那些
    cwd = os.getcwd()
    parts = [
        "下面是一些环境信息：",
        f"- 工作目录：{cwd}",
        f"- 操作系统：{platform.system()}",
        f"- 今天的日期：{date.today().isoformat()}",
    ]
    agents_md = _find_agents_md(cwd)
    if agents_md:
        try:
            content = open(agents_md, encoding="utf-8").read()
        except OSError:
            content = ""
        if content.strip():
            parts.append("")
            parts.append(f"以下是项目的 AGENTS.md（{agents_md}），请遵循其中的约定：")
            parts.append(content)
    return "\n".join(parts)


def _register(atype: AgentType) -> None:
    # 拿着 AgentType 配置构造真正的 Agent 实例并注册进类型表
    atype.agent = Agent(
        model,
        instructions=[atype.instructions, _sub_env_context],
        tools=_build_tools(atype.tool_names),
        deps_type=AgentDeps,
        capabilities=[sub_hooks],
    )
    _TYPES[atype.name] = atype


_register(AgentType(
    name="explore",
    description="只读的代码调查 agent：搜索、阅读代码来回答「X 在哪」「Y 是怎么实现的」这类问题，不做任何修改",
    tool_names=["read_file", "run_command"],
    instructions=EXPLORE_INSTRUCTIONS,
    source="built-in",
    # 只读是代码事实，不再只靠 EXPLORE_INSTRUCTIONS 那句「严禁做任何修改」
    readonly=True,
))
_register(AgentType(
    name="general",
    description="可读写文件、执行命令的通用 agent，适合写测试、修独立 bug 这类多步骤的独立子任务",
    tool_names=["read_file", "edit_file", "write_file", "run_command"],
    instructions=GENERAL_INSTRUCTIONS,
    source="built-in",
    readonly=False,
))


def get_agent_type(name: str) -> AgentType | None:
    return _TYPES.get(name)


def list_agent_types() -> list[AgentType]:
    return list(_TYPES.values())


# ---------- 自定义 agent 类型 ----------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    解析 markdown 头部的 frontmatter（只支持扁平的 key: value），返回 (meta, 正文)。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    # 没等到闭合的 ---：不算合法 frontmatter，整段当正文
    return {}, text


def load_custom_agents() -> int:
    """
    扫描 .my-claude-code/agents/ 下的 *.md 注册自定义 agent：
    frontmatter 里 name / description 必填，tools 是逗号分隔的白名单（不写默认全集），
    正文就是这个 agent 的 system prompt。返回注册数。
    """
    if not AGENTS_DIR.is_dir():
        return 0
    count = 0
    for path in sorted(AGENTS_DIR.glob("*.md")):
        try:
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()
        if not name or not description or not body.strip():
            continue
        tool_names = [t.strip() for t in str(meta.get("tools", "")).split(",") if t.strip()]
        if not tool_names:
            tool_names = list(_TOOL_FUNCS)
        unknown_tools = set(tool_names) - _TOOL_FUNCS.keys()
        if unknown_tools:
            logger.warning("跳过自定义 agent %s：未知工具 %s", path, ", ".join(sorted(unknown_tools)))
            continue
        # readonly 的来源：frontmatter 里显式写了就以它为准（readonly: true 能在配了
        # 写工具的情况下把类型强制成只读）；没写就按工具表推导——配了写工具=可写、
        # 只配只读工具=只读。推导而不是「一律默认可写」，是为了让既有自定义 agent
        # 的行为逐字节不变，同时拿到「只读类型真的只读」这个属性。
        declared = str(meta.get("readonly", "")).strip().lower()
        readonly = declared == "true" if declared else not has_write_tools(tool_names)
        _register(AgentType(
            name=name,
            description=description,
            tool_names=tool_names,
            instructions=body.strip(),
            source=str(path),
            readonly=readonly,
        ))
        count += 1
    return count


# ---------- sub agent 执行器 ----------

def _short(value, limit: int = 120) -> str:
    # 日志里工具调用的单行摘要
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


async def run_subagent(atype: AgentType, prompt: str, job: Job, parent_deps: AgentDeps) -> None:
    """
    在 agent 型 job 里跑一个 sub agent：全新上下文，每一步实时写进 job 日志文件，
    最终报告存进 job.result，等通知机制随 <result> 字段送回主 agent。
    """
    # sub agent 自带一份 job 注册表：它自己起的后台命令随它的生命周期清理
    sub_registry = JobRegistry(session_id=f"{parent_deps.job_registry.session_id}/{job.id}")
    # 从主会话 deps 派生：读文件状态隔离、job 注册表换成自己的，其余字段原样继承
    # （file_history 继承——sub agent 改文件也被追踪，/rewind 能撤销它的修改）
    deps = dataclasses.replace(
        parent_deps,
        read_file_state=ReadFileState(),
        tasks_store=None,
        job_registry=sub_registry,
        subagent_job=job,
        # 只读事实随 deps 下发：file 工具据此硬拒写盘（权威强制在工具里，不依赖 hook）
        readonly=atype.readonly,
    )
    log = open(job.log_path, "a", encoding="utf-8")
    log.write(f"=== sub agent {job.id}（{atype.name}）===\nprompt: {prompt}\n\n")
    try:
        # 只传任务 prompt，不传对话历史，全新上下文
        async with atype.agent.iter(
            prompt, deps=deps,
            usage_limits=UsageLimits(request_limit=MAX_SUBAGENT_REQUESTS),
        ) as run:
            node = run.next_node
            while not isinstance(node, End):
                node = await run.next(node)
                # 把每一步（思考/工具调用/工具结果）实时写进日志文件
                if Agent.is_call_tools_node(node):
                    for part in node.model_response.parts:
                        if part.part_kind == "thinking" and part.content.strip():
                            log.write(f"[思考] {_short(part.content, 300)}\n")
                        elif part.part_kind == "tool-call":
                            log.write(f"- 调用 {part.tool_name}({_short(part.args)})\n")
                elif Agent.is_model_request_node(node):
                    for part in node.request.parts:
                        if part.part_kind == "tool-return":
                            log.write(f"  → {part.tool_name}: {_short(part.content)}\n")
                log.flush()

        # 记录任务结果——报告是 sub agent 唯一的输出通道
        job.result = run.result.output or "(sub agent 没有输出报告)"
        # 记下子代理自己的用量：它不在主 agent 的 result.usage 里，
        # 不单独落一份就会从会话累计里整块丢掉（token 类指标会因此系统性偏低）
        job.usage = run.result.usage
        log.write(f"=== 最终报告 ===\n{job.result}\n")
        log.write(f"=== 用量 === 输入 {job.usage.input_tokens} / 输出 {job.usage.output_tokens}\n")
        logger.info("● sub agent「%s」执行成功", job.description)
    finally:
        log.close()
        await sub_registry.aclose()
