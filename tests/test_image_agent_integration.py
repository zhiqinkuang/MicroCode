import asyncio

import pytest
from pydantic_ai.messages import BinaryContent, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_graph import End

import agent.model as agent_model
import images
import main
import permissions
from tests.image_helpers import make_png
from UI.commands import SessionState


def image_parts(messages):
    return [
        item
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, list)
        for item in part.content
        if isinstance(item, BinaryContent)
    ]


def isolated_state(session_id):
    return SessionState(session_id=session_id)


def configure_run(monkeypatch):
    monkeypatch.setattr(main.mcp_servers, "active_toolsets", lambda: [])
    monkeypatch.setattr(main.memory_background, "schedule", lambda state, messages: None)
    monkeypatch.setattr(main, "print_part", lambda part: None)
    permissions.state.mode = permissions.BYPASS


def test_direct_image_reaches_agent_in_order(tmp_path, monkeypatch):
    configure_run(monkeypatch)
    content = images.load_image(make_png(tmp_path / "direct.png"))
    observed = []

    def respond(messages, info):
        observed.extend(messages)
        return ModelResponse(parts=[TextPart("received")])

    state = isolated_state("direct-image")
    asyncio.run(main.run_agent_loop(["before", content, "after"], state, model=FunctionModel(respond)))

    prompt = next(part.content for message in observed for part in message.parts if isinstance(part, UserPromptPart))
    assert prompt == ["before", content, "after"]
    assert image_parts(observed) == [content]
    asyncio.run(state.job_registry.aclose())


def test_read_file_image_returns_binary_content_to_agent(tmp_path, monkeypatch):
    configure_run(monkeypatch)
    path = make_png(tmp_path / "tool.png")
    observed_returns = []

    def respond(messages, info):
        tool_returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not tool_returns:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": str(path)})])
        observed_returns.extend(tool_returns)
        return ModelResponse(parts=[TextPart("tool image received")])

    state = isolated_state("tool-image")
    asyncio.run(main.run_agent_loop("read the image", state, model=FunctionModel(respond)))

    assert len(observed_returns) == 1
    assert isinstance(observed_returns[0].content, BinaryContent)
    assert observed_returns[0].content.media_type == "image/png"
    asyncio.run(state.job_registry.aclose())


def test_at_image_flows_through_assembly_agent_and_session(tmp_path, monkeypatch):
    configure_run(monkeypatch)
    monkeypatch.chdir(tmp_path)
    make_png(tmp_path / "mention.png")
    observed = []

    def respond(messages, info):
        observed.extend(messages)
        return ModelResponse(parts=[TextPart("mention received")])

    state = isolated_state("mention-image")
    content = main.prepare_user_input("@mention.png describe", state)
    asyncio.run(main.run_agent_loop(content, state, model=FunctionModel(respond)))

    assert isinstance(content[0], BinaryContent)
    assert content[1] == " describe"
    assert len(image_parts(observed)) == 1
    assert any(image_parts([message]) for message in state.history)
    asyncio.run(state.job_registry.aclose())


class _FakeUsage:
    input_tokens = 0
    output_tokens = 0


class _FakeResult:
    usage = _FakeUsage()

    def new_messages(self):
        return []

    def all_messages(self):
        return []


class _FakeRun:
    """空跑一轮：next_node 直接是 End，用来隔离模型选择这一层。"""

    def __init__(self):
        self.next_node = End(None)
        self.result = _FakeResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeAgent:
    """只记录 agent.iter 收到的 model，证明路由打到了真正发起请求的那一层。"""

    def __init__(self):
        self.seen_model = None

    def iter(self, *args, **kwargs):
        self.seen_model = kwargs.get("model")
        return _FakeRun()


def test_text_turn_selects_text_model():
    assert main.select_turn_model("帮我看下这个函数") is agent_model.model


def test_image_turn_selects_vision_model(tmp_path):
    content = images.load_image(make_png(tmp_path / "route.png"))
    assert main.select_turn_model(["看看这张图", content]) is agent_model.vision_model


def test_image_turn_without_vision_model_reports_actionable_error(tmp_path, monkeypatch):
    content = images.load_image(make_png(tmp_path / "unconfigured.png"))
    monkeypatch.setattr(agent_model, "vision_model", None)

    with pytest.raises(agent_model.VisionModelNotConfigured) as excinfo:
        main.select_turn_model([content])

    assert "DEEPSEEK_VISION_MODEL" in str(excinfo.value)


def test_run_agent_loop_routes_image_turn_through_vision_model(tmp_path, monkeypatch):
    configure_run(monkeypatch)
    fake_agent = _FakeAgent()
    monkeypatch.setattr(main, "agent", fake_agent)
    content = images.load_image(make_png(tmp_path / "loop.png"))

    state = isolated_state("routing-image")
    asyncio.run(main.run_agent_loop(["看图", content], state))

    assert fake_agent.seen_model is agent_model.vision_model
    asyncio.run(state.job_registry.aclose())


def test_run_agent_loop_keeps_text_model_for_plain_turn(monkeypatch):
    configure_run(monkeypatch)
    fake_agent = _FakeAgent()
    monkeypatch.setattr(main, "agent", fake_agent)

    state = isolated_state("routing-text")
    asyncio.run(main.run_agent_loop("只是普通提问", state))

    assert fake_agent.seen_model is agent_model.model
    asyncio.run(state.job_registry.aclose())


def test_run_agent_loop_explicit_model_wins(monkeypatch):
    """测试与 subagent 显式传入的模型不被路由覆盖。"""
    configure_run(monkeypatch)
    fake_agent = _FakeAgent()
    monkeypatch.setattr(main, "agent", fake_agent)

    explicit = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
    state = isolated_state("routing-explicit")
    asyncio.run(main.run_agent_loop("普通提问", state, model=explicit))

    assert fake_agent.seen_model is explicit
    asyncio.run(state.job_registry.aclose())
