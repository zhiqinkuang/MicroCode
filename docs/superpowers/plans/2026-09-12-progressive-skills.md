# Progressive Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add progressively loaded personal and project Skills to My Claude Code, with a complete standalone test script under `test/`.

**Architecture:** A focused `skills.py` module owns discovery, precedence, catalog formatting, and body loading. The existing dynamic project instructions expose the lightweight catalog, while a new read-only `load_skill` tool loads one selected body. Startup, documentation, and tests consume those stable interfaces.

**Tech Stack:** Python 3.12, pydantic-ai `ModelRetry`, pathlib, unittest, existing Agent tool and permission registries.

**Spec:** `docs/superpowers/specs/2026-09-12-progressive-skills-design.md`

## Global Constraints

- All variable names use snake_case.
- Production code uses `logging` or existing Rich console rendering; it does not call `print`.
- Discovery never reads past a valid frontmatter terminator.
- Project Skills override personal Skills with the same declared name.
- Default tests make no network or real model requests.

---

### Task 1: Skill discovery and lazy loading

**Files:**
- Create: `skills.py`
- Create: `test/test_skills.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `SkillInfo`, `discover_skills(cwd=None, user_skills_dir=None)`, `format_skill_listing(skill_infos)`, `read_skill(name, cwd=None, user_skills_dir=None)`, and `load_skill(name)`.

- [x] **Step 1: Write failing discovery tests**

```python
def test_project_skill_overrides_personal_skill(self):
    skill_infos = skills.discover_skills(self.project_dir, self.personal_dir)
    self.assertEqual(skill_infos[0].source, "project")
```

- [x] **Step 2: Verify the tests fail because `skills.py` is absent**

Run: `.venv/bin/python -B test/test_skills.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'skills'`.

- [x] **Step 3: Implement discovery and body loading**

```python
@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    source: str

def load_skill(name: str) -> str:
    return read_skill(name)
```

- [x] **Step 4: Run deterministic tests**

Run: `.venv/bin/python -B test/test_skills.py`
Expected: discovery, precedence, lazy loading, validation, and unknown-name cases pass.

### Task 2: Agent context, tool, permissions, and startup

**Files:**
- Modify: `agent/core.py`
- Modify: `agent/tools/__init__.py`
- Modify: `permissions.py`
- Modify: `main.py`
- Test: `test/test_skills.py`

**Interfaces:**
- Consumes: all public functions from `skills.py`.
- Produces: `load_skill` in `TOOLS`, skill metadata in `project_context()`, and `format_startup_summary()` for startup reporting.

- [x] **Step 1: Add failing integration tests**

```python
def test_catalog_is_dynamic_and_body_is_not_in_context(self):
    context = project_context()
    self.assertIn("reviewing-code", context)
    self.assertNotIn("BODY_SENTINEL", context)
```

- [x] **Step 2: Verify failures identify missing integrations**

Run: `.venv/bin/python -B test/test_skills.py`
Expected: FAIL because the tool, permission entry, context listing, and startup summary are missing.

- [x] **Step 3: Wire the catalog and tool into the Agent**

```python
skill_listing = skills.format_skill_listing(skills.discover_skills())
if skill_listing:
    parts.extend(["", skill_listing])
```

- [x] **Step 4: Run the standalone test suite**

Run: `.venv/bin/python -B test/test_skills.py`
Expected: all offline tests pass with no API requests.

### Task 3: Documentation and live functional test

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `.github/workflows/ci.yml`
- Test: `test/test_skills.py`

**Interfaces:**
- Consumes: the public Skill API and existing `run_agent_loop`.
- Produces: documented user workflow and an opt-in `--live` test mode.

- [x] **Step 1: Add the live test path**

```python
async def run_live_test(project_dir: Path) -> None:
    await run_agent_loop("加载 live-review Skill 后总结步骤。", state)
    assert_tool_called(state.history, "load_skill")
```

- [x] **Step 2: Document the three loading layers and override rule**

Document metadata catalog loading, selected `SKILL.md` loading, and on-demand reference/script access with a complete `reviewing-code` example.

- [x] **Step 3: Run all verification**

Run: `.venv/bin/python -B test/test_skills.py`
Expected: standalone offline suite passes.

Run: `.venv/bin/python -B -m pytest -q -p no:cacheprovider`
Expected: existing 20 tests pass.

Run: `uvx --offline ruff check .`
Expected: no lint errors.

Run: `DEEPSEEK_API_KEY=smoke .venv/bin/python -B -c 'import main; from agent import agent'`
Expected: import succeeds.

Run when a real key is configured: `.venv/bin/python -B test/test_skills.py --live`
Expected: model calls `load_skill` and answers from the loaded body.
