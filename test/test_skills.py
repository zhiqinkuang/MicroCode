"""Skill 系统完整功能测试。

离线测试（默认，不访问模型或网络）：
    .venv/bin/python -B test/test_skills.py

真实模型测试（需要已配置 DEEPSEEK_API_KEY，会产生模型用量）：
    .venv/bin/python -B test/test_skills.py --live
"""
from __future__ import annotations

import asyncio
import builtins
import logging
import os
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, main as unittest_main
from unittest.mock import patch
import uuid

from dotenv import load_dotenv

live_requested = "--live" in sys.argv
unittest_argv = [argument for argument in sys.argv if argument != "--live"]
project_source_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_source_dir))
load_dotenv(project_source_dir / "agent" / ".env")
if not os.getenv("DEEPSEEK_API_KEY"):
    if live_requested:
        raise SystemExit("--live 需要通过 agent/.env 或环境变量配置 DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "offline-test-key"

from pydantic_ai import models  # noqa: E402
from pydantic_ai.exceptions import ModelRetry  # noqa: E402

models.ALLOW_MODEL_REQUESTS = live_requested

import permissions  # noqa: E402
import session  # noqa: E402
import skills  # noqa: E402
from agent.core import project_context  # noqa: E402
from agent.tools import TOOLS  # noqa: E402


logger = logging.getLogger(__name__)


def write_skill(
    skills_root: Path,
    directory_name: str,
    name: str | None,
    description: str | None,
    body: str,
    *,
    close_frontmatter: bool = True,
) -> Path:
    skill_dir = skills_root / directory_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if description is not None:
        lines.append(f'description: "{description}"')
    if close_frontmatter:
        lines.append("---")
    lines.extend(["", body])
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("\n".join(lines), encoding="utf-8")
    return skill_path


class HeaderOnlyReader:
    """发现阶段若越过 frontmatter 或调用 read()，测试立即失败。"""

    def __init__(self, content: str):
        self.lines = iter(content.splitlines(keepends=True))
        self.closed_frontmatter = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def readline(self):
        if self.closed_frontmatter:
            raise AssertionError("发现阶段读取了 SKILL.md 正文")
        line = next(self.lines, "")
        if line.strip() == "---" and hasattr(self, "opened_frontmatter"):
            self.closed_frontmatter = True
        elif line.strip() == "---":
            self.opened_frontmatter = True
        return line

    def read(self, *args, **kwargs):
        raise AssertionError("发现阶段不应调用 read() 读取完整文件")


class SkillSystemTests(TestCase):
    def setUp(self):
        self.temp_dir_context = tempfile.TemporaryDirectory(prefix="skills-test-")
        self.temp_dir = Path(self.temp_dir_context.name).resolve()
        self.project_dir = self.temp_dir / "project"
        self.project_dir.mkdir()
        self.personal_dir = self.temp_dir / "personal-skills"
        self.personal_dir.mkdir()

    def tearDown(self):
        self.temp_dir_context.cleanup()

    def discover(self):
        return skills.discover_skills(self.project_dir, self.personal_dir)

    def test_frontmatter_reader_stops_before_body(self):
        content = "---\nname: reviewing-code\ndescription: Review changes\n---\nBODY_SENTINEL\n"
        reader = HeaderOnlyReader(content)
        with patch.object(builtins, "open", return_value=reader):
            fields = skills._read_frontmatter(Path("unused/SKILL.md"))
        self.assertEqual(fields, {"name": "reviewing-code", "description": "Review changes"})

    def test_discovery_uses_metadata_only_and_sorts_by_name(self):
        write_skill(self.personal_dir, "z-dir", "z-skill", "Zulu workflow", "Z_BODY")
        write_skill(self.personal_dir, "a-dir", "a-skill", "Alpha workflow", "A_BODY")
        skill_infos = self.discover()
        self.assertEqual([skill_info.name for skill_info in skill_infos], ["a-skill", "z-skill"])
        self.assertEqual([skill_info.source for skill_info in skill_infos], ["personal", "personal"])
        self.assertFalse(hasattr(skill_infos[0], "body"))

    def test_project_skill_overrides_personal_skill_by_declared_name(self):
        personal_path = write_skill(self.personal_dir, "personal-dir", "reviewing-code", "Personal", "PERSONAL_BODY")
        project_root = self.project_dir / ".my-claude-code" / "skills"
        project_path = write_skill(project_root, "project-dir", "reviewing-code", "Project", "PROJECT_BODY")
        skill_info = self.discover()[0]
        self.assertEqual(skill_info.source, "project")
        self.assertEqual(skill_info.description, "Project")
        self.assertEqual(skill_info.path, project_path.resolve())
        self.assertNotEqual(skill_info.path, personal_path.resolve())

    def test_invalid_or_unreadable_skill_definitions_are_ignored(self):
        write_skill(self.personal_dir, "valid", "valid", "Valid workflow", "BODY")
        write_skill(self.personal_dir, "missing-name", None, "No name", "BODY")
        write_skill(self.personal_dir, "missing-description", "missing-description", None, "BODY")
        write_skill(self.personal_dir, "unterminated", "unterminated", "Broken", "BODY", close_frontmatter=False)
        invalid_dir = self.personal_dir / "invalid-encoding"
        invalid_dir.mkdir()
        (invalid_dir / "SKILL.md").write_bytes(b"---\nname: bad\ndescription: \xff\n---\n")
        self.assertEqual([skill_info.name for skill_info in self.discover()], ["valid"])

    def test_catalog_contains_only_metadata_and_routing_instruction(self):
        skill_path = write_skill(
            self.personal_dir,
            "reviewing-code",
            "reviewing-code",
            "Review changes",
            "BODY_SENTINEL",
        )
        references_dir = skill_path.parent / "references"
        references_dir.mkdir()
        (references_dir / "database.md").write_text("REFERENCE_SENTINEL", encoding="utf-8")
        listing = skills.format_skill_listing(self.discover())
        self.assertIn("- reviewing-code: Review changes", listing)
        self.assertIn("先调用 load_skill", listing)
        self.assertNotIn("BODY_SENTINEL", listing)
        self.assertNotIn("REFERENCE_SENTINEL", listing)

    def test_read_skill_loads_body_without_frontmatter_and_reports_root(self):
        skill_path = write_skill(
            self.personal_dir,
            "reviewing-code",
            "reviewing-code",
            "Review changes",
            "# Review\n\nRead the diff first.",
        )
        result = skills.read_skill(" reviewing-code ", self.project_dir, self.personal_dir)
        self.assertIn(f"skill reviewing-code 的根目录：{skill_path.parent.resolve()}", result)
        self.assertIn("# Review\n\nRead the diff first.", result)
        self.assertNotIn("description: Review changes", result)

    def test_unknown_skill_is_recoverable_and_lists_available_names(self):
        write_skill(self.personal_dir, "reviewing-code", "reviewing-code", "Review changes", "BODY")
        with self.assertRaisesRegex(ModelRetry, "未知 skill：missing.*reviewing-code"):
            skills.read_skill("missing", self.project_dir, self.personal_dir)

    def test_discovery_and_reading_refresh_after_files_change(self):
        self.assertEqual(self.discover(), [])
        skill_path = write_skill(self.personal_dir, "dynamic", "dynamic", "First description", "FIRST_BODY")
        self.assertEqual(self.discover()[0].description, "First description")
        skill_path.write_text(
            "---\nname: dynamic\ndescription: Second description\n---\n\nSECOND_BODY\n",
            encoding="utf-8",
        )
        self.assertEqual(self.discover()[0].description, "Second description")
        self.assertIn("SECOND_BODY", skills.read_skill("dynamic", self.project_dir, self.personal_dir))
        skill_path.unlink()
        self.assertEqual(self.discover(), [])

    def test_load_skill_uses_current_project_and_default_personal_root(self):
        project_root = self.project_dir / ".my-claude-code" / "skills"
        skill_path = write_skill(project_root, "local", "local", "Local workflow", "LOCAL_BODY")
        with patch.object(Path, "home", return_value=self.temp_dir), patch("os.getcwd", return_value=str(self.project_dir)):
            result = skills.load_skill("local")
        self.assertIn("LOCAL_BODY", result)
        self.assertIn(str(skill_path.parent.resolve()), result)

    def test_load_skill_is_registered_and_read_only(self):
        tool_names = [getattr(tool, "name", None) or getattr(tool, "__name__", None) for tool in TOOLS]
        self.assertIn("load_skill", tool_names)
        self.assertEqual(permissions.compute_decision("load_skill", {"name": "reviewing-code"}), "allow")

    def test_project_context_injects_dynamic_catalog_without_body(self):
        project_root = self.project_dir / ".my-claude-code" / "skills"
        with (
            patch("os.getcwd", return_value=str(self.project_dir)),
            patch.object(Path, "home", return_value=self.temp_dir),
            patch("agent.core.store.read_index", return_value=None),
            patch("agent.core.subagents.list_agent_types", return_value=[]),
        ):
            self.assertNotIn("可用 skills", project_context())
            write_skill(project_root, "reviewing-code", "reviewing-code", "Review changes", "BODY_SENTINEL")
            context = project_context()
        self.assertIn("- reviewing-code: Review changes", context)
        self.assertNotIn("BODY_SENTINEL", context)

    def test_startup_summary_is_empty_or_lists_discovered_names(self):
        self.assertEqual(skills.format_startup_summary([]), "")
        write_skill(self.personal_dir, "z", "z-skill", "Z", "BODY")
        write_skill(self.personal_dir, "a", "a-skill", "A", "BODY")
        self.assertEqual(
            skills.format_startup_summary(self.discover()),
            "发现 2 个 skills：a-skill、z-skill",
        )


async def run_live_test(project_dir: Path) -> None:
    import main
    from UI.commands import SessionState
    from memory import background as memory_background

    with tempfile.TemporaryDirectory(prefix="skills-live-") as directory:
        root_dir = Path(directory).resolve()
        skill_name = "live-review-" + uuid.uuid4().hex[:8]
        answer_token = "SKILL-LOADED-" + uuid.uuid4().hex[:10]
        reference_token = "REFERENCE-NOT-LOADED-" + uuid.uuid4().hex[:10]
        skills_root = root_dir / ".my-claude-code" / "skills"
        skill_path = write_skill(
            skills_root,
            skill_name,
            skill_name,
            "当用户要求执行渐进式 Skill 真实测试时使用。",
            f"回复时必须包含标记 {answer_token}。不要读取 references/database.md。",
        )
        references_dir = skill_path.parent / "references"
        references_dir.mkdir()
        (references_dir / "database.md").write_text(reference_token, encoding="utf-8")
        previous_dir = Path.cwd()
        os.chdir(root_dir)
        state = None
        try:
            with (
                patch.object(Path, "home", return_value=root_dir),
                patch.object(session, "STORAGE_ROOT", root_dir / "sessions"),
                patch.object(memory_background, "schedule"),
            ):
                state = SessionState(model_name="live", session_id="skills-live")
                async with asyncio.timeout(180):
                    await main.run_agent_loop(
                        "执行渐进式 Skill 真实测试。根据可用 skills 的描述选择匹配项，"
                        "先调用 load_skill，再严格按加载到的指令回复。不要调用其他工具。",
                        state,
                    )
                tool_calls = [
                    part
                    for message in state.history
                    for part in message.parts
                    if part.part_kind == "tool-call"
                ]
                tool_returns = [
                    part
                    for message in state.history
                    for part in message.parts
                    if part.part_kind == "tool-return"
                ]
                assistant_text = "\n".join(
                    part.content
                    for message in state.history
                    for part in message.parts
                    if part.part_kind == "text"
                )
                if [part.tool_name for part in tool_calls] != ["load_skill"]:
                    raise AssertionError(f"模型工具调用不符合预期：{tool_calls}")
                if not any(answer_token in str(part.content) for part in tool_returns):
                    raise AssertionError("load_skill 返回值未包含 Skill 正文")
                if answer_token not in assistant_text:
                    raise AssertionError("模型没有遵循加载后的 Skill 指令")
                if reference_token in str(state.history):
                    raise AssertionError("未使用的参考资料被加载进上下文")
                logger.info("真实模型测试通过：目录发现 → load_skill → 按正文回答，未加载无关 reference")
        finally:
            if state is not None:
                await state.job_registry.aclose()
            os.chdir(previous_dir)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_program = unittest_main(argv=unittest_argv, exit=False)
    if test_program.result.wasSuccessful() and live_requested:
        asyncio.run(run_live_test(Path(__file__).resolve().parents[1]))
    elif not test_program.result.wasSuccessful():
        raise SystemExit(1)
