"""
模型实例化：从 agent/.env 读配置，构造 DeepSeek（OpenAI 协议兼容）的 model。

主 agent 和 subagent 共用同一个 model 实例。从 core.py 拆出来单独一个模块，
避免 subagent 工具链反向 import core 时形成循环依赖。
"""
import os
from pathlib import Path

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from dotenv import load_dotenv

# .env 跟模块同目录（agent/），用绝对路径避免 CWD 不同导致加载失败
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
