"""
模型实例化：从 agent/.env 读配置，构造 DeepSeek（OpenAI 协议兼容）的 model。

文本轮用 DEEPSEEK_MODEL（默认 deepseek-v4-flash），含图片块的一轮切到
DEEPSEEK_VISION_MODEL（默认 deepseek-v4-flash-vision-exp）；两者共用同一 provider，
因此端点和密钥只有一处事实。subagent、压缩和记忆后台任务都用文本模型。
"""
import os
from pathlib import Path

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from dotenv import load_dotenv

import images

# .env 跟模块同目录（agent/），用绝对路径避免 CWD 不同导致加载失败
load_dotenv(Path(__file__).parent / ".env")

# 从环境变量读取 API Key
API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not API_KEY:
    raise RuntimeError("请先在 agent/.env 中设置 DEEPSEEK_API_KEY")

DEFAULT_MODEL_NAME = "deepseek-v4-flash"
DEFAULT_VISION_MODEL_NAME = "deepseek-v4-flash-vision-exp"

MODEL_NAME = os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL_NAME
# 显式设成空串表示这个部署不提供视觉模型：图片轮给出可操作的报错，而不是静默降级
VISION_MODEL_NAME = os.getenv("DEEPSEEK_VISION_MODEL", DEFAULT_VISION_MODEL_NAME).strip()
# 从 .env 读取 API 端点，默认 DeepSeek 官方；可改成中转/镜像
API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")

# DeepSeek API 兼容 OpenAI 协议，用 OpenAIProvider 才能把 DEEPSEEK_API_BASE 真正传进去
# （DeepSeekProvider 不接受 base_url，会忽略自定义端点）
_provider = OpenAIProvider(base_url=API_BASE, api_key=API_KEY)


def _build_model(model_name: str) -> OpenAIChatModel:
    """两个模型共用同一 provider，端点与密钥只解析一次。"""
    return OpenAIChatModel(model_name, provider=_provider)


model = _build_model(MODEL_NAME)
# 没有配置视觉模型时保持 None：只有真的发图片才需要它，纯文本部署不受影响
vision_model = _build_model(VISION_MODEL_NAME) if VISION_MODEL_NAME else None


class VisionModelNotConfigured(RuntimeError):
    """本轮含图片，但部署没有配置视觉模型。"""


def select_turn_model(content):
    """
    按本轮内容选择模型：含图片块的一轮走视觉模型，其余一律走文本模型。

    read_file 可能在纯文本开场之后才把图片带进同一轮工具调用，所以整轮模型必须支持视觉输入。
    """
    if not images.contains_image(content):
        return model
    if vision_model is None:
        raise VisionModelNotConfigured(
            "本轮包含图片，但未配置视觉模型：请在 agent/.env 中设置 DEEPSEEK_VISION_MODEL"
            f"（默认 {DEFAULT_VISION_MODEL_NAME}）后重试"
        )
    return vision_model
