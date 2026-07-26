"""
Agent 实例化：把 model / instructions / tools / hooks 拼起来。
"""
import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from .hooks import hooks
from .tools import TOOLS
from dotenv import load_dotenv

# .env 跟 core.py 同目录（agent/），用绝对路径避免 CWD 不同导致加载失败
load_dotenv(Path(__file__).parent / ".env")

# 从环境变量读取 API Key
API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not API_KEY:
    raise RuntimeError("请先在 agent/.env 中设置 DEEPSEEK_API_KEY")

# 从 .env 读取模型名，默认 deepseek-v4-flash
MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

model = OpenAIChatModel(
    MODEL_NAME,
    provider=DeepSeekProvider(api_key=API_KEY),
)

agent = Agent(
    model,
    instructions=(
        "你是一个编程助手。你可以读写文件和执行命令来帮用户完成编程任务。\n"
        "工作流程：先理解需求，写代码，然后运行验证。"
        "如果有错误就修复并重新运行，直到确认正确。"
    ),
    tools=TOOLS,
    capabilities=[hooks],
)