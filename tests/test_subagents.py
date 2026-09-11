import asyncio
import shlex
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.exceptions import ModelRetry
import pytest

import main
import permissions
import subagents
from agent.deps import AgentDeps
from agent.file_state import ReadFileState
from agent.reminders import build_job_reminder_text
from agent.tools.agents import run_agent
from background_jobs import Job, JobRegistry
from file_history import FileHistory
from UI.commands import SessionState, cmd_new, cmd_agents
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.application.current import get_app
from UI.input_ui import Repl


async def settle(registry):
    await asyncio.wait_for(asyncio.gather(*registry._watchers), timeout=3)


def test_agent_dispatch_is_immediate_and_report_notified_once():
    async def scenario():
        registry = JobRegistry("dispatch")
        release = asyncio.Event()

        async def run(job):
            await release.wait()
            job.result = "调查报告"

        first, second = await asyncio.gather(
            registry.spawn_agent("first", run), registry.spawn_agent("second", run),
        )
        assert first.id.startswith("a") and first.id != second.id
        assert len(registry.running()) == 2
        assert build_job_reminder_text(registry) is None
        release.set()
        await settle(registry)
        notice = build_job_reminder_text(registry)
        assert notice.count("<result>调查报告</result>") == 2
        assert notice.count("<status>completed</status>") == 2
        assert build_job_reminder_text(registry) is None

    asyncio.run(scenario())


def test_failed_agent_logs_error_and_notifies_failure():
    async def scenario():
        registry = JobRegistry("failure")

        async def run(job):
            raise ValueError("broken task")

        job = await registry.spawn_agent("failure", run)
        await settle(registry)
        assert job.status == "failed"
        assert "ValueError: broken task" in job.log_path.read_text()
        assert "<status>failed</status>" in build_job_reminder_text(registry)

    asyncio.run(scenario())


def test_kill_during_completion_keeps_killed_status():
    async def scenario():
        registry = JobRegistry("cancel")
        started = asyncio.Event()

        async def run(job):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                job.result = "late report"

        job = await registry.spawn_agent("cancel", run)
        await started.wait()
        registry.kill(job.id)
        await settle(registry)
        assert job.status == "killed"

    asyncio.run(scenario())


def test_cancelled_approval_does_not_reappear():
    async def scenario():
        job = Job("a1", "agent", "cancelled", Path("unused.log"))
        pending = asyncio.create_task(subagents._request_user_approval(job, "write_file", {}))
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert subagents.pop_pending_approval() is None

    asyncio.run(scenario())


def test_close_waits_for_agent_cleanup():
    async def scenario():
        registry = JobRegistry("close")
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def run(job):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        job = await registry.spawn_agent("close", run)
        await started.wait()
        await registry.aclose()
        assert cleaned.is_set()
        assert job.status == "killed"
        assert not registry._watchers

    asyncio.run(scenario())


def test_close_terminates_shell_ignoring_sigterm():
    async def scenario():
        registry = JobRegistry("stubborn-shell")
        script = (
            "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "os.write(1, str(os.getpid()).encode()); time.sleep(10)"
        )
        job = await registry.spawn_shell(f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}")
        try:
            async with asyncio.timeout(3):
                while not job.log_path.read_text():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(registry.aclose(), timeout=4)
            assert job.status == "killed"
            assert not registry._watchers
        finally:
            if registry._watchers:
                os.killpg(int(job.log_path.read_text()), signal.SIGKILL)
                await settle(registry)

    asyncio.run(scenario())


def test_approval_popup_reserves_repl(monkeypatch):
    async def scenario():
        repl = Repl.__new__(Repl)
        repl._task = None
        repl.app = object()
        repl.approval_active = False
        entered = asyncio.Event()
        release = asyncio.Event()

        async def prompt(*args, **kwargs):
            entered.set()
            assert get_app() is repl.app
            await release.wait()
            return "once"

        monkeypatch.setattr(permissions, "prompt_approval", prompt)
        job = Job("a1", "agent", "approval", Path("unused.log"))
        request = asyncio.create_task(subagents._request_user_approval(job, "write_file", {}))
        watcher = asyncio.create_task(main.watch_approvals(repl))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not repl.is_idle
            release.set()
            assert await asyncio.wait_for(request, timeout=2) == "once"
            assert repl.is_idle
        finally:
            watcher.cancel()
            request.cancel()
            await asyncio.gather(watcher, request, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("choice", ["once", "always", "deny"])
def test_subagent_permission_executes_only_after_approval(choice):
    async def scenario():
        job = Job("a1", "agent", "approval", Path("unused.log"))
        ctx = SimpleNamespace(deps=SimpleNamespace(subagent_job=job), messages=[])
        writes = []

        async def handler(args):
            writes.append(args)
            return "written"

        task = asyncio.create_task(subagents._check_sub_permission(
            ctx, call=SimpleNamespace(tool_name="write_file"), tool_def=None,
            args={"path": "example.py"}, handler=handler,
        ))
        await asyncio.sleep(0)
        assert not writes and not task.done()
        request = subagents.pop_pending_approval()
        request.future.set_result(choice)
        await task
        assert bool(writes) == (choice != "deny")
        assert ("write_file" in permissions.state.session_allowed) == (choice == "always")

    asyncio.run(scenario())


def test_run_agent_isolated_context_logs_report_and_tracks_edit(tmp_path):
    async def scenario():
        file_path = tmp_path / "example.py"
        file_path.write_text("before\n")
        registry = JobRegistry("integration")
        history = FileHistory("integration")
        history.make_checkpoint(0, "delegate")
        deps = AgentDeps(ReadFileState(), None, history, registry)
        ctx = SimpleNamespace(deps=deps)
        permissions.state.mode = permissions.BYPASS
        prompts = []

        def respond(messages, info):
            prompts.append(messages)
            returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart("read_file", {"path": str(file_path)})])
            if len(returns) == 1:
                return ModelResponse(parts=[ToolCallPart("edit_file", {
                    "path": str(file_path), "old_string": "before", "new_string": "after",
                })])
            return ModelResponse(parts=[TextPart("已修改 example.py")])

        agent_type = subagents.get_agent_type("general")
        with agent_type.agent.override(model=FunctionModel(respond)):
            notice = await run_agent(ctx, "edit", "独立修改任务")
            job = registry.list()[0]
            assert job.id in notice
            await settle(registry)
        assert job.status == "completed"
        assert file_path.read_text() == "after\n"
        assert deps.read_file_state.get(str(file_path)) is None
        assert "独立修改任务" in str(prompts[0])
        assert "已修改 example.py" in job.log_path.read_text()
        assert "read_file" in job.log_path.read_text()
        assert "<result>已修改 example.py</result>" in build_job_reminder_text(registry)
        history.rewind_files(history.checkpoints[0])
        assert file_path.read_text() == "before\n"

    asyncio.run(scenario())


@pytest.mark.parametrize("verdict", [
    {"should_block": False, "reason": "authorized"},
    {"should_block": True, "reason": "blocked"},
    {"error": True, "reason": "unavailable"},
])
def test_auto_permission_allows_blocks_or_falls_back(verdict, monkeypatch):
    async def scenario():
        permissions.state.mode = permissions.AUTO
        writes = []

        async def classify(messages, tool_name, args):
            return verdict

        async def handler(args):
            writes.append(args)
            return "written"

        monkeypatch.setattr(subagents.classifier, "classify", classify)
        job = Job("a1", "agent", "auto", Path("unused.log"))
        ctx = SimpleNamespace(deps=SimpleNamespace(subagent_job=job), messages=[])
        task = asyncio.create_task(subagents._check_sub_permission(
            ctx, call=SimpleNamespace(tool_name="write_file"), tool_def=None,
            args={"path": "example.py"}, handler=handler,
        ))
        await asyncio.sleep(0)
        if verdict.get("error"):
            assert not task.done() and not writes
            request = subagents.pop_pending_approval()
            request.future.set_result("deny")
        result = await task
        assert bool(writes) == (verdict.get("should_block") is False)
        if verdict.get("should_block"):
            assert "blocked" in result
        assert not subagents.PENDING_APPROVALS

    asyncio.run(scenario())


def test_subagent_cannot_delegate_or_access_parent_tools():
    async def scenario():
        observed_tools = []

        def respond(messages, info):
            observed_tools.extend(tool.name for tool in info.function_tools)
            return ModelResponse(parts=[TextPart("调查完成")])

        agent_type = subagents.get_agent_type("explore")
        registry = JobRegistry("tools")
        deps = AgentDeps(ReadFileState(), None, job_registry=registry)
        with agent_type.agent.override(model=FunctionModel(respond)):
            await run_agent(SimpleNamespace(deps=deps), "explore", "调查", "explore")
            await settle(registry)
        assert set(observed_tools) == {"read_file", "run_command"}

    asyncio.run(scenario())


def test_status_bar_counts_background_shells_and_agents_separately():
    registry = JobRegistry("counts")
    for job_id, kind, background in [("a1", "agent", True), ("b1", "shell", True), ("b2", "shell", False)]:
        registry._jobs[job_id] = Job(job_id, kind, "count", Path("unused"), background=background)
    repl = Repl.__new__(Repl)
    repl.state = SimpleNamespace(job_registry=registry)
    text = "".join(fragment[1] for fragment in to_formatted_text(repl._mode_line()))
    assert "1 agent" in text and "1 shell" in text
    assert "2 shell" not in text


def test_agents_command_lists_custom_type(caplog, tmp_path):
    agents_dir = tmp_path / ".my-claude-code/agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Review code\ntools: read_file\n---\nReview carefully.\n",
    )
    subagents.load_custom_agents()
    with caplog.at_level("INFO"):
        cmd_agents(None)
    assert "reviewer" in caplog.text and "Review code" in caplog.text
    assert "explore" in caplog.text and "general" in caplog.text


def test_invalid_agent_type_returns_recoverable_error():
    with pytest.raises(ModelRetry, match="不存在"):
        asyncio.run(run_agent(SimpleNamespace(deps=None), "unknown", "task", "unknown"))


def test_custom_agent_rejects_unknown_tools(tmp_path):
    agents_dir = tmp_path / ".my-claude-code/agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Review code\ntools: read_file\n---\nReview carefully.\n",
    )
    (agents_dir / "broken.md").write_text(
        "---\nname: broken\ndescription: Broken\ntools: typo_tool\n---\nRead files.\n",
    )
    assert subagents.load_custom_agents() == 1
    assert subagents.get_agent_type("reviewer").tool_names == ["read_file"]
    assert subagents.get_agent_type("broken") is None


def test_new_session_cleans_approval_and_session_permission():
    async def scenario():
        state = SessionState(session_id="old")
        permissions.state.session_allowed.add("write_file")

        async def run(job):
            await subagents._request_user_approval(job, "run_command", {})

        job = await state.job_registry.spawn_agent("old approval", run)
        await asyncio.sleep(0)
        result = cmd_new(state)
        if asyncio.iscoroutine(result):
            await result
        assert state.session_id != "old"
        assert job.status == "killed"
        assert not permissions.state.session_allowed
        assert subagents.pop_pending_approval() is None

    asyncio.run(scenario())
