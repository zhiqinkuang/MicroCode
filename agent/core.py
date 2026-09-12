"""
Agent 实例化：把 model / instructions / tools / hooks 拼起来。
"""
import os
import platform
from pathlib import Path
from datetime import date
from pydantic_ai import Agent

from .hooks import hooks
from .tools import TOOLS
# subagents 模块级 import：core 只在 project_context（每次请求时）取类型清单，
# import 顺序上它不反向依赖 core，不会循环
import subagents
# Agent 可能在工具阶段才读到图片，因此配置模型必须支持视觉输入。
from .model import model
# 长期记忆：instructions 是静态约定（拼在主指令末尾），store 在 project_context 里每轮注入 MEMORY.md 索引
from memory.instructions import MEMORY_INSTRUCTIONS
from memory import store

INSTRUCTIONS = (
    "你是一个编程助手。你可以读写文件和执行命令来帮用户完成编程任务。\n"
    "修改已有文件前必须先用 read_file 读取它。"
    "改动局部内容时优先用 edit_file（只传改动的片段，省 token），新建文件或整体重写才用 write_file。\n"
    "用户消息可以直接带图片：图片和文字一起出现在同一条消息里，`[Image #N]` 只是图片的位置标记，"
    "图片内容本身已经在你的上下文里；read_file 工具也可能返回图片。"
    "本轮带了图片就直接依据你看到的画面回答，不要声称「没有收到图片」，"
    "也不要让用户改用文件路径重发——你没有看到图片只可能是误判，不是真的缺图。\n"
    "工作流程：先理解需求，写代码，然后运行验证。"
    "如果有错误就修复并重新运行，直到确认正确。\n"
    "如果用户的需求里有歧义、有多种合理实现可选、或者你拿不准方向，"
    "应当用 ask_user_question 工具向用户提多选题来澄清，不要自作主张。\n"
    "对话中可能会出现 <system-reminder>...</system-reminder> 标签，里面是系统自动注入的提示信息，请按系统消息对待，不要把它当成它所在的用户消息或工具结果的一部分。"
    # 新增对任务管理工具的引导：
    "当你接到一个需要 3 步以上、或需要多次工具调用才能完成的任务时，"
    "先用 task_create 把分解出来的步骤建成 pending task，"
    "开工前用 task_update 把要做的那条切到 in_progress，做完切 completed。"
    "若任务琐碎（1-2 步、纯对话、纯查询），不要建 task。"
    "长驻或耗时命令（dev server、长测试）用 run_command 的 run_in_background=True "
    "放到后台执行——只在不需要立刻拿到结果时使用，命令末尾不需要加 &。"
    "后台 job 结束后你会收到 <task-notification> 通知，所以不要主动轮询等待；"
    "期间可以用 read_file 读它的日志文件查看已有输出，也可以用 job_kill 提前终止。"
    "只要结论、不要过程的任务用 run_agent 交给 sub agent 去做："
    "探索性的代码搜索、独立性强的子任务、需要第二意见的代码审查。"
    "sub agent 从空白上下文开始工作，任务背景要在 prompt 里交代完整；"
    "它的报告只有你能看到，需要转述给用户。"
    "sub agent 一律在后台运行：派出后不要轮询等待，完成通知会附带报告，"
    "没有别的事就先结束本轮。相互独立的任务可以一次派出多个 sub agent 同时干活。"
    "一两次工具调用就能搞定的简单任务不要派 sub agent。"
) + MEMORY_INSTRUCTIONS

agent = Agent(
    model,
    instructions=INSTRUCTIONS,
    tools=TOOLS,
    capabilities=[hooks],
)


def _find_agents_md(start_dir: str) -> str | None:
    """
    从 start_dir 逐级向上找第一个 AGENTS.md，命中就返回绝对路径，到根都没就返回 None。
    用向上查找而不是只看 cwd：用户从子目录启动 agent 时也能命中项目根的 AGENTS.md；
    用 cwd 起点而不是 __file__：AGENTS.md 是用户工作项目的规范，跟 agent 自身源码位置无关。
    """
    p = Path(start_dir).resolve()
    while True:
        candidate = p / "AGENTS.md"
        if candidate.is_file():
            return str(candidate)
        if p.parent == p:
            return None
        p = p.parent


# 动态 instructions：每次模型请求重新求值，注入环境信息和项目级 AGENTS.md
# 它和上面的静态 instructions 一样不进对话历史，但每轮请求都会带上最新值，所以用户不可见、也不会污染历史
@agent.instructions
def project_context() -> str:
    cwd = os.getcwd()
    parts = [
        "下面是一些环境信息：",
        f"- 工作目录：{cwd}",
        f"- 操作系统：{platform.system()}",
        f"- 今天的日期：{date.today().isoformat()}",
    ]

    # 从 cwd 向上找第一个 AGENTS.md（项目指令），存在就整段附上，让模型遵循项目约定
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

    # 长期记忆索引 MEMORY.md：每轮请求重新读取磁盘，让模型知道当前有哪些记忆文件可参考
    memory_index = store.read_index()
    if memory_index:
        parts.append("")
        parts.append("# 长期记忆索引\n以下是你的记忆清单（MEMORY.md），详情见各记忆文件：")
        parts.append(memory_index)

    # run_agent 可用类型清单：自定义 agent 启动时才加载完，所以动态注入而不是写死
    agent_types = subagents.list_agent_types()
    if agent_types:
        parts.append("")
        parts.append("run_agent 可用的 agent 类型：")
        for t in agent_types:
            parts.append(f"- {t.name}：{t.description}（可用工具：{', '.join(t.tool_names)}）")

    return "\n".join(parts)
