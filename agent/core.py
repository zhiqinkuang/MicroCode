"""
Agent 实例化：把 model / instructions / tools / hooks 拼起来。
"""
import os
import platform
from pathlib import Path
from datetime import date
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .hooks import hooks
from .tools import TOOLS
# 长期记忆：instructions 是静态约定（拼在主指令末尾），store 在 project_context 里每轮注入 MEMORY.md 索引
from memory.instructions import MEMORY_INSTRUCTIONS
from memory import store
from dotenv import load_dotenv

# .env 跟 core.py 同目录（agent/），用绝对路径避免 CWD 不同导致加载失败
load_dotenv(Path(__file__).parent / ".env")

# 从环境变量读取 API Key
API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not API_KEY:
    raise RuntimeError("请先在 agent/.env 中设置 DEEPSEEK_API_KEY")

# 从 .env 读取模型名，默认 deepseek-v4-flash
MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
# 从 .env 读取 API 端点，默认 DeepSeek 官方；可改成中转/镜像
API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")

# DeepSeek API 兼容 OpenAI 协议，用 OpenAIProvider 才能把 DEEPSEEK_API_BASE 真正传进去
# （DeepSeekProvider 不接受 base_url，会忽略自定义端点）
model = OpenAIChatModel(
    MODEL_NAME,
    provider=OpenAIProvider(base_url=API_BASE, api_key=API_KEY),
)

INSTRUCTIONS = (
    "你是一个编程助手。你可以读写文件和执行命令来帮用户完成编程任务。\n"
    "修改已有文件前必须先用 read_file 读取它。"
    "改动局部内容时优先用 edit_file（只传改动的片段，省 token），新建文件或整体重写才用 write_file。\n"
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

    return "\n".join(parts)