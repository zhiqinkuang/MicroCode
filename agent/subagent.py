"""
subagent：把一个子任务交给独立的子 agent 跑，以后台 job 的形态融入现有机制。

子 agent 有自己的 instructions 和精简工具集，不继承主对话历史（防爆上下文），
只收到主 agent 拼好的任务描述；过程实时写进 job 日志文件，read_file 随时可看。
权限上和主 agent 完全同一条链：同一套 hooks，工具调用照样过审批。
"""
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, Tool
from pydantic_graph import End

from .hooks import hooks
from .model import model
# 直接从具体工具模块取函数而不走 tools/__init__，避免循环 import
from .tools.file import edit_file, read_file, write_file
from .tools.shell import run_command


@dataclass(frozen=True)
class SubagentSpec:
    # 给主 agent 看的类型说明（拼进 run_subagent 的工具描述）
    name: str
    description: str
    # 子 agent 自己的指令
    instructions: str
    # 子 agent 可用的工具
    tools: list


# 编辑类工具在子 agent 里同样标记 sequential=True，理由和主 TOOLS 一致：
# 同一轮多个改文件调用并发写盘会互相覆盖
_EXPLORE_TOOLS = [read_file, run_command]
_IMPLEMENT_TOOLS = [
    read_file,
    Tool(edit_file, sequential=True),
    Tool(write_file, sequential=True),
    run_command,
]

# 三个内置类型：覆盖「调查 / 实现 / 审查」三类边界清晰的委托场景。
# 子 agent 一律没有 ask_user_question（不能阻塞等用户），也不能再派 subagent（不递归）
SUBAGENTS = {
    "explore": SubagentSpec(
        name="explore",
        description="只读调查：搞清楚代码怎么工作的、定位实现、回答调查类问题，返回自包含的结论",
        instructions=(
            "你是一个代码调查 agent，负责理解代码库、定位代码、回答调查类问题。\n"
            "只做调查，不要修改任何文件。用 read_file 读代码，用 run_command 跑 grep / find 这类只读命令。\n"
            "你的最终回复会被主 agent 直接采用，必须自包含：给出关键文件路径和行号，"
            "直接回答被问到的问题，不要说「见上文」这类依赖上下文的话。\n"
            "信息不足时基于现有材料给出最优答案，并注明不确定的部分。你不能向用户提问。\n"
        ),
        tools=_EXPLORE_TOOLS,
    ),
    "implement": SubagentSpec(
        name="implement",
        description="独立实现：完成边界清晰的编码子任务（写新模块、独立小功能），返回改动摘要",
        instructions=(
            "你是一个实现 agent，负责完成主 agent 委托的、边界清晰的编码子任务。\n"
            "修改已有文件前必须先用 read_file 读取它，改动局部内容优先用 edit_file。\n"
            "写完代码要运行验证（跑测试 / 编译 / 执行），有错误就修复重跑，直到确认正确。\n"
            "你的最终回复是给主 agent 的改动摘要：改了哪些文件、各做了什么、验证结果如何。\n"
            "遇到超出任务边界的需求不要扩张范围，在摘要里说明即可。你不能向用户提问。\n"
        ),
        tools=_IMPLEMENT_TOOLS,
    ),
    "review": SubagentSpec(
        name="review",
        description="代码审查：读指定代码找缺陷和风险，返回带文件行号的问题清单",
        instructions=(
            "你是一个代码审查 agent，负责找出委托范围内代码的真实缺陷。\n"
            "只读不改。重点看：逻辑错误、边界条件、错误处理缺失、并发问题、安全隐患。\n"
            "每个发现都要给出文件路径和行号、问题描述、失败场景（什么输入会触发什么错误）；"
            "拿不准的问题注明存疑，不要为了凑数报风格问题。\n"
            "你的最终回复是给主 agent 的问题清单，按严重程度排序，自包含不引用上下文。\n"
        ),
        tools=_EXPLORE_TOOLS,
    ),
}


def _short(value, limit: int = 120) -> str:
    # 日志里工具调用的单行摘要
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


async def run_subagent_coro(spec: SubagentSpec, task: str, log_path: Path, deps) -> None:
    """
    跑一个子 agent：独立上下文，过程和最终结论实时写进 job 日志文件。

    共享主 agent 的 hooks（权限审批、API 元数据记录）和 deps
    （read_file_state / file_history / job_registry——子 agent 读过的文件、改动能被
    主 agent 和 /rewind 看到，也能起自己的后台命令）。
    """
    sub_agent = Agent(model, instructions=spec.instructions, tools=spec.tools, capabilities=[hooks])

    with open(log_path, "w", encoding="utf-8") as log:
        def write(line: str = ""):
            log.write(line + "\n")
            log.flush()

        write(f"# subagent[{spec.name}]")
        write(f"## 任务\n{task}\n")
        write("## 过程")

        async with sub_agent.iter(task, deps=deps) as run:
            node = run.next_node
            while not isinstance(node, End):
                node = await run.next(node)

                if Agent.is_call_tools_node(node):
                    # 模型刚回复完：记下 thinking / 工具调用
                    for part in node.model_response.parts:
                        if part.part_kind == "thinking" and part.content.strip():
                            write(f"\n[思考] {_short(part.content, 300)}")
                        elif part.part_kind == "tool-call":
                            write(f"- 调用 {part.tool_name}({_short(part.args)})")
                elif Agent.is_model_request_node(node):
                    # 工具刚执行完：记下返回摘要
                    for part in node.request.parts:
                        if part.part_kind == "tool-return":
                            write(f"  → {part.tool_name}: {_short(part.content)}")

        write(f"\n## 最终结论\n{run.result.output}")
