"""Skill 发现与渐进式加载。

发现阶段只读取 SKILL.md 的 frontmatter；正文只在 load_skill 命中后读取。
"""
from dataclasses import dataclass
import json
from pathlib import Path

from pydantic_ai.exceptions import ModelRetry


FRONTMATTER_MAX_LINES = 100
PROJECT_SKILLS_RELATIVE_DIR = Path(".my-claude-code") / "skills"


@dataclass(frozen=True)
class SkillInfo:
    """发现阶段保留的轻量 Skill 元数据。"""

    name: str
    description: str
    path: Path
    source: str


def _decode_scalar(value: str) -> str:
    """解析 frontmatter 中常见的裸字符串、双引号或单引号字符串。"""
    value = value.strip()
    if len(value) < 2 or value[0] != value[-1] or value[0] not in {'"', "'"}:
        return value
    if value[0] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else str(decoded)
    return value[1:-1].replace("''", "'")


def _read_frontmatter(path: Path) -> dict[str, str]:
    """仅读取文件头的扁平 frontmatter，不触碰正文。"""
    try:
        with open(path, encoding="utf-8") as skill_file:
            if skill_file.readline().strip() != "---":
                return {}
            fields: dict[str, str] = {}
            for _ in range(FRONTMATTER_MAX_LINES):
                line = skill_file.readline()
                if not line:
                    return {}
                if line.strip() == "---":
                    return fields
                key, separator, value = line.partition(":")
                if separator:
                    fields[key.strip()] = _decode_scalar(value)
    except (OSError, UnicodeError):
        return {}
    return {}


def _read_body(path: Path) -> str:
    """读取并返回去掉 frontmatter 的 SKILL.md 正文。"""
    try:
        with open(path, encoding="utf-8") as skill_file:
            if skill_file.readline().strip() != "---":
                return ""
            for _ in range(FRONTMATTER_MAX_LINES):
                line = skill_file.readline()
                if not line:
                    return ""
                if line.strip() == "---":
                    return skill_file.read().strip()
    except (OSError, UnicodeError):
        return ""
    return ""


def discover_skills(
    cwd: Path | str | None = None,
    user_skills_dir: Path | str | None = None,
) -> list[SkillInfo]:
    """发现个人和项目 Skill；同名时项目定义覆盖个人定义。"""
    project_dir = Path.cwd() if cwd is None else Path(cwd)
    personal_root = (
        Path.home() / ".my-claude-code" / "skills"
        if user_skills_dir is None
        else Path(user_skills_dir)
    )
    project_root = project_dir / PROJECT_SKILLS_RELATIVE_DIR
    effective_skills: dict[str, SkillInfo] = {}

    for skills_root, source in ((personal_root, "personal"), (project_root, "project")):
        if not skills_root.is_dir():
            continue
        for skill_path in sorted(skills_root.glob("*/SKILL.md")):
            fields = _read_frontmatter(skill_path)
            name = fields.get("name", "").strip()
            description = fields.get("description", "").strip()
            if not name or not description:
                continue
            effective_skills[name] = SkillInfo(
                name=name,
                description=description,
                path=skill_path.resolve(),
                source=source,
            )

    return sorted(effective_skills.values(), key=lambda skill_info: skill_info.name)


def format_skill_listing(skill_infos: list[SkillInfo]) -> str:
    """把 Skill 元数据格式化成注入模型的轻量能力目录。"""
    if not skill_infos:
        return ""
    lines = ["可用 skills（这里只包含名称和描述）："]
    lines.extend(f"- {skill_info.name}: {skill_info.description}" for skill_info in skill_infos)
    lines.extend([
        "",
        "当某个 skill 匹配用户任务时，继续处理前先调用 load_skill 读取它；不要加载无关 skill。",
    ])
    return "\n".join(lines)


def format_startup_summary(skill_infos: list[SkillInfo]) -> str:
    """构造启动时展示的 Skill 发现摘要。"""
    if not skill_infos:
        return ""
    names = "、".join(skill_info.name for skill_info in skill_infos)
    return f"发现 {len(skill_infos)} 个 skills：{names}"


def read_skill(
    name: str,
    cwd: Path | str | None = None,
    user_skills_dir: Path | str | None = None,
) -> str:
    """按当前目录重新发现并读取一个 Skill 的正文。"""
    skill_by_name = {
        skill_info.name: skill_info
        for skill_info in discover_skills(cwd, user_skills_dir)
    }
    normalized_name = name.strip()
    skill_info = skill_by_name.get(normalized_name)
    if skill_info is None:
        available = ", ".join(skill_by_name) or "(none)"
        raise ModelRetry(f"未知 skill：{normalized_name}。可用 skills：{available}")

    body = _read_body(skill_info.path)
    if not body:
        raise ModelRetry(f"skill {skill_info.name} 的 SKILL.md 正文为空或无法读取")
    return f"skill {skill_info.name} 的根目录：{skill_info.path.parent}\n\n{body}"


def load_skill(name: str) -> str:
    """
    当可用 skill 的描述匹配当前任务后，读取它的完整指令。

    Args:
        name: 可用 skills 清单里的准确名称
    """
    return read_skill(name)
