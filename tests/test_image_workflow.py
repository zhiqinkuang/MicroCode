import asyncio
from types import SimpleNamespace

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.keys import Keys
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

import images
import main
import session
from agent.file_state import ReadFileState
from agent.tools import file as file_tool
from mentions import build_mention_messages
from tests.image_helpers import make_png
from UI.commands import SessionState, _prompt_summary, cmd_new
from UI.input_ui import Repl


class FakeApp:
    def __init__(self):
        self.invalidations = 0

    def invalidate(self):
        self.invalidations += 1


class FakeConsole:
    def __init__(self):
        self.messages = []

    def print(self, message=""):
        self.messages.append(str(message))


def make_repl(attachments=None):
    repl = Repl.__new__(Repl)
    repl.state = SimpleNamespace(attachments=list(attachments or []), job_registry=None)
    repl._buffer = Buffer()
    repl._task = None
    repl.app = FakeApp()
    repl.approval_active = False
    return repl


def test_paste_image_adds_numbered_placeholder(monkeypatch, tmp_path):
    content = images.load_image(make_png(tmp_path / "image.png"))
    repl = make_repl()
    monkeypatch.setattr(images, "read_clipboard_image", lambda: content)

    repl._paste_image()

    assert repl.state.attachments == [content]
    assert repl._buffer.text == "[Image #1]"
    assert repl.app.invalidations == 1


def test_paste_image_error_keeps_state_and_shows_message(monkeypatch):
    repl = make_repl()
    console = FakeConsole()
    monkeypatch.setattr("UI.input_ui.console", console)

    def fail():
        raise images.ImageInputError("每轮最多发送 8 张图片")

    monkeypatch.setattr(images, "read_clipboard_image", fail)

    repl._paste_image()

    assert repl.state.attachments == []
    assert repl._buffer.text == ""
    assert any("最多" in message for message in console.messages)


def test_paste_image_enforces_shared_attachment_limit(monkeypatch, tmp_path):
    content = images.load_image(make_png(tmp_path / "image.png"))
    repl = make_repl([content] * images.MAX_ATTACHMENTS)
    console = FakeConsole()
    monkeypatch.setattr("UI.input_ui.console", console)
    monkeypatch.setattr(images, "read_clipboard_image", lambda: content)

    repl._paste_image()

    assert len(repl.state.attachments) == images.MAX_ATTACHMENTS
    assert repl._buffer.text == ""
    assert any("最多" in message for message in console.messages)


def test_escape_clears_buffer_and_pending_attachments():
    content = BinaryContent(data=b"image", media_type="image/png")
    repl = make_repl([content])
    repl._buffer.text = "draft [Image #1]"
    bindings = repl._build_key_bindings().bindings
    escape_binding = next(binding for binding in bindings if binding.keys == (Keys.Escape,))

    escape_binding.handler(SimpleNamespace(app=repl.app))

    assert repl._buffer.text == ""
    assert repl.state.attachments == []


def test_new_command_clears_pending_attachments():
    state = SessionState(session_id="old")
    state.attachments.append(BinaryContent(data=b"image", media_type="image/png"))

    asyncio.run(cmd_new(state))

    assert state.attachments == []
    asyncio.run(state.job_registry.aclose())


def test_mentions_share_attachment_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_png(tmp_path / "extra.png")
    content = images.load_image(tmp_path / "extra.png")
    attachments = [content] * images.MAX_ATTACHMENTS

    with pytest.raises(images.ImageInputError, match="最多"):
        build_mention_messages(["extra.png"], ReadFileState(), attachments)
    assert len(attachments) == images.MAX_ATTACHMENTS


def test_invalid_mentioned_image_is_not_silently_dropped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "broken.png").write_bytes(b"broken")

    with pytest.raises(images.ImageInputError, match="无法识别"):
        build_mention_messages(["broken.png"], ReadFileState(), [])


def test_read_file_converts_image_error_to_model_retry(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"broken")
    context = SimpleNamespace(deps=SimpleNamespace(read_file_state=ReadFileState()))

    with pytest.raises(ModelRetry, match="无法识别"):
        file_tool.read_file(context, str(path))


def test_prepare_user_input_consumes_attachments_after_success(tmp_path, monkeypatch):
    path = make_png(tmp_path / "image.png")
    content = images.load_image(path)
    state = SessionState(session_id="workflow")
    state.attachments.append(content)
    monkeypatch.setattr(main, "inject_at_mentions", lambda text, state, attachments: text)

    result = main.prepare_user_input("[Image #1] describe", state)

    assert result == [content, " describe"]
    assert state.attachments == []
    asyncio.run(state.job_registry.aclose())


def test_prepare_user_input_keeps_attachments_when_local_validation_fails(tmp_path, monkeypatch):
    content = images.load_image(make_png(tmp_path / "image.png"))
    state = SessionState(session_id="workflow-error")
    state.attachments.append(content)

    def fail(text, state, attachments):
        raise images.ImageInputError("bad mention")

    monkeypatch.setattr(main, "inject_at_mentions", fail)

    with pytest.raises(images.ImageInputError, match="bad mention"):
        main.prepare_user_input("prompt", state)
    assert state.attachments == [content]
    asyncio.run(state.job_registry.aclose())


def test_prepare_user_input_text_turn_has_no_stale_image(monkeypatch):
    state = SessionState(session_id="text-only")
    monkeypatch.setattr(main, "inject_at_mentions", lambda text, state, attachments: text)

    assert main.prepare_user_input("hello", state) == "hello"
    asyncio.run(state.job_registry.aclose())


def test_multimodal_session_round_trip_and_safe_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "STORAGE_ROOT", tmp_path / "sessions")
    content = BinaryContent(data=b"secret-image-bytes", media_type="image/png")
    message = ModelRequest(parts=[UserPromptPart(content=["look ", content])])

    session.append_messages("round-trip", [message])
    restored = session.load_history("round-trip")
    summary = session.first_prompt(session.session_file("round-trip"))

    restored_content = restored[0].parts[0].content
    assert restored_content[1].data == content.data
    assert restored_content[1].media_type == "image/png"
    assert "image/png" in summary
    assert "secret-image-bytes" not in summary
    assert "secret-image-bytes" not in _prompt_summary(restored_content)
    # 会话列表摘要和终端回放必须用同一套规则，不能各自演化
    assert summary == _prompt_summary(restored_content)
