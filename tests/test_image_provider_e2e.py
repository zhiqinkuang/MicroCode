import asyncio

from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

import images
import main
from tests.fake_openai_server import FakeOpenAIServer
from tests.image_helpers import make_png
from UI.commands import SessionState


def test_image_reaches_openai_compatible_provider_as_data_uri(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(main.mcp_servers, "active_toolsets", lambda: [])
    monkeypatch.setattr(main.memory_background, "schedule", lambda state, messages: None)
    monkeypatch.setattr(main, "print_part", lambda part: None)
    image = images.load_image(make_png(tmp_path / "wire.png"))
    state = SessionState(session_id="provider-wire")

    with FakeOpenAIServer() as server:
        provider = OpenAIProvider(base_url=server.base_url, api_key="local-test-key")
        model = OpenAIChatModel("test-vision", provider=provider)
        asyncio.run(main.run_agent_loop(["before", image, "after"], state, model=model))

    request = server.requests[0]
    assert request["path"] == "/v1/chat/completions"
    user_content = next(message["content"] for message in request["payload"]["messages"] if message["role"] == "user")
    assert user_content[0] == {"type": "text", "text": "before"}
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert user_content[2] == {"type": "text", "text": "after"}
    assert state.input_tokens == 11
    assert state.output_tokens == 3
    asyncio.run(state.job_registry.aclose())
