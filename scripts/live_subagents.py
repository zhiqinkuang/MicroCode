"""Opt-in functional smoke test using the configured model and temporary files.

Run: uv run python scripts/live_subagents.py
This makes real model requests; ordinary pytest/CI never runs this script.
"""
import argparse
import ast
import asyncio
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

logger = logging.getLogger(__name__)


async def exercise(project_dir: Path) -> None:
    sys.path.insert(0, str(project_dir))
    import main
    import permissions
    import session
    import subagents
    from agent import MODEL_NAME
    from agent.reminders import build_job_reminder_text
    from memory import background as memory_background
    from UI.commands import SessionState

    with tempfile.TemporaryDirectory(prefix="subagent-live-") as directory:
        root_dir = Path(directory).resolve()
        previous_dir = Path.cwd()
        os.chdir(root_dir)
        marker = "SUBAGENT-LIVE-" + uuid.uuid4().hex[:12]
        (root_dir / "README.md").write_text(f"# {marker}\n", encoding="utf-8")
        target_path = root_dir / "result.py"
        approvals = []
        states = []

        async def approve_expected_write(tool_name, args, **kwargs):
            # Only the fixture file is authorized. Unexpected shell commands or
            # writes are denied rather than blindly approving model output.
            if tool_name in {"write_file", "edit_file"} and Path(args.get("path", "")).resolve() == target_path:
                approvals.append(tool_name)
                return "once"
            return "deny"

        async def answer_queued_approvals():
            while True:
                request = subagents.pop_pending_approval()
                if request is not None and not request.future.done():
                    choice = await approve_expected_write(request.tool_name, request.args)
                    request.future.set_result(choice)
                await asyncio.sleep(0.05)

        async def wait_for_agents(state):
            while state.job_registry.running():
                await asyncio.sleep(0.05)
            jobs = [job for job in state.job_registry.list() if job.kind == "agent"]
            assert len(jobs) == 1, "Expected exactly one delegated agent"
            assert jobs[0].status == "completed", f"Agent failed: {jobs[0].log_path.read_text()}"
            assert jobs[0].result, "Missing final report"
            assert "=== 最终报告 ===" in jobs[0].log_path.read_text()
            return jobs[0]

        try:
            with (
                patch.object(Path, "home", return_value=root_dir),
                patch.object(session, "STORAGE_ROOT", root_dir / "sessions"),
                patch.object(memory_background, "schedule"),
                patch.object(permissions, "state", permissions.PermissionState()),
                patch.object(permissions, "prompt_approval", approve_expected_write),
                patch.object(subagents, "PENDING_APPROVALS", []),
                (root_dir / "conversation.log").open("w", encoding="utf-8") as output_file,
                patch.object(main.console, "_file", output_file),
            ):
                approval_task = asyncio.create_task(answer_queued_approvals())
                try:
                    async with asyncio.timeout(240):
                        state = SessionState(model_name=MODEL_NAME, session_id="read")
                        states.append(state)
                        await main.run_agent_loop(
                            "这是一个单步骤委派测试。必须只调用一次 run_agent，agent_type=explore，"
                            "让它使用 read_file 读取当前目录 README.md 并在报告中原样给出第一行标题。"
                            "你自己不要读取文件，不要创建 task，不要调用 shell。派发后立即结束本轮，不等待。",
                            state,
                        )
                        job = await wait_for_agents(state)
                        assert marker in job.result, "Subagent did not report the fixture title"

                        # Notifications can arrive via the next request or the
                        # idle watcher. Exercise the idle path for pending jobs.
                        delivered = []
                        repl = SimpleNamespace(is_idle=True, submit_system=delivered.append)
                        watcher = asyncio.create_task(main.watch_jobs(repl, state))
                        try:
                            while not job.notified:
                                await asyncio.sleep(0.05)
                        finally:
                            watcher.cancel()
                            await asyncio.gather(watcher, return_exceptions=True)
                        notification = "\n".join(delivered)
                        if not notification:
                            notification = "子代理完成通知已在历史中。请原样转述报告中的 README 标题，不调用工具。"
                        await main.run_agent_loop(notification, state)
                        final_text = "\n".join(
                            part.content for part in state.history[-1].parts if part.part_kind == "text"
                        )
                        assert marker in final_text, "Parent did not relay the returned report"
                        logger.info("PASS: live parent delegation, explore file read, report notification and relay")

                    async with asyncio.timeout(240):
                        state = SessionState(model_name=MODEL_NAME, session_id="write")
                        states.append(state)
                        state.file_history.make_checkpoint(0, "delegated write")
                        await main.run_agent_loop(
                            "这是一个单步骤委派测试。必须只调用一次 run_agent，agent_type=general。"
                            "给子代理的任务：仅使用 write_file 在当前目录创建 result.py，内容严格为\n"
                            f"def smoke_result():\n    return '{marker}'\n"
                            "子代理不要运行 shell、不要创建其他文件。你自己不要写文件，不要创建 task。"
                            "派发后立即结束本轮，不要等待子代理完成。",
                            state,
                        )
                        job = await wait_for_agents(state)
                        assert approvals, f"Expected a queued file-write approval; report: {job.result}"
                        tree = ast.parse(target_path.read_text(encoding="utf-8"))
                        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
                        assert function.name == "smoke_result"
                        assert ast.literal_eval(function.body[0].value) == marker
                        assert str(target_path) in state.file_history.versions
                        notification = build_job_reminder_text(state.job_registry)
                        assert job.notified and (notification is None or "<result>" in notification)
                        state.file_history.rewind_files(state.file_history.checkpoints[0])
                        assert not target_path.exists(), "Rewind did not remove the delegated edit"
                        logger.info("PASS: live general write, queued approval, final report and file rewind")
                finally:
                    approval_task.cancel()
                    await asyncio.gather(approval_task, return_exceptions=True)
                    for state in states:
                        await state.job_registry.aclose()
        finally:
            os.chdir(previous_dir)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logger.setLevel(logging.INFO)
    asyncio.run(exercise(args.project.resolve()))


if __name__ == "__main__":
    cli()
