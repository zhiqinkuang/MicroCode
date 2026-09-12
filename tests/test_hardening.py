"""
沙箱加固第一阶段的离线回归：旁路模块的配置一致性。

背景：`classifier` 与 `memory/recall` 曾经各自写死 base_url 与模型名，
导致 auto 模式的安全审查和每轮记忆召回绕过 `DEEPSEEK_API_BASE` 打到官方端点，
而且审查用的模型与用户配置的主模型不是同一个。本文件守住「不许再写死」。
"""
import importlib

import pytest

import agent.model as agent_model
import classifier
import memory.recall as memory_recall

CUSTOM_BASE = "https://gateway.internal/v1"
CUSTOM_MODEL = "custom-model-name"


def _load_bypass_modules_under(monkeypatch, base: str, model: str):
    """
    把两个旁路模块放到指定的端点和模型名下重新加载，返回 (classifier, recall, agent.model)。

    必须重新加载而不是只改环境变量：这两个模块在 import 期就把配置固化进模块级对象，
    只改环境变量看不出它到底读没读配置——而这正是本测试要证伪的「写死」行为。
    agent.model 用 load_dotenv 的默认 override=False，不会覆盖已有的环境变量，
    所以 monkeypatch.setenv 的值能生效。
    """
    monkeypatch.setenv("DEEPSEEK_API_BASE", base)
    monkeypatch.setenv("DEEPSEEK_MODEL", model)
    importlib.reload(agent_model)
    importlib.reload(classifier)
    importlib.reload(memory_recall)
    return classifier, memory_recall, agent_model


@pytest.fixture
def bypass_modules(monkeypatch):
    """
    在自定义端点/模型名下加载两个旁路模块；用例结束后恢复原始配置并重新加载，
    避免把自定义配置泄漏给同进程里的其他测试。
    """
    yield _load_bypass_modules_under(monkeypatch, CUSTOM_BASE, CUSTOM_MODEL)
    monkeypatch.undo()
    importlib.reload(agent_model)
    importlib.reload(classifier)
    importlib.reload(memory_recall)


def test_bypass_modules_follow_custom_endpoint(bypass_modules):
    """
    换个自定义端点后，两个旁路模块必须跟着走。
    这是「没有写死」的直接证据——写死的话它们会停在官方地址不动。
    """
    classifier_module, recall_module, model_module = bypass_modules
    assert model_module.API_BASE == CUSTOM_BASE
    assert str(classifier_module._client.base_url).rstrip("/") == CUSTOM_BASE
    assert str(recall_module._client.base_url).rstrip("/") == CUSTOM_BASE


def test_bypass_modules_follow_custom_model_name(bypass_modules):
    """模型名同理：必须跟随 DEEPSEEK_MODEL，不能各自硬编码一个常量。"""
    classifier_module, recall_module, model_module = bypass_modules
    assert model_module.MODEL_NAME == CUSTOM_MODEL
    assert classifier_module.CLASSIFIER_MODEL == CUSTOM_MODEL
    assert recall_module.RECALL_MODEL == CUSTOM_MODEL


def test_bypass_modules_share_main_model_credentials():
    """
    默认配置下三者保持一致：端点和密钥同一份事实（密钥不打印，只比相等）。
    """
    assert str(classifier._client.base_url).rstrip("/") == agent_model.API_BASE.rstrip("/")
    assert str(memory_recall._client.base_url).rstrip("/") == agent_model.API_BASE.rstrip("/")
    assert classifier.CLASSIFIER_MODEL == agent_model.MODEL_NAME
    assert memory_recall.RECALL_MODEL == agent_model.MODEL_NAME
