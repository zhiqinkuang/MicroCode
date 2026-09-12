# Progressive Skills Design

## Objective

Add project and personal Skills to My Claude Code without placing every `SKILL.md` body in every model request. The model always receives a small catalog containing each Skill's name and description. It loads the selected Skill body through `load_skill`, then reads referenced files or runs scripts only when the loaded instructions require them.

## Discovery and precedence

Skills live in either `~/.my-claude-code/skills/<directory>/SKILL.md` or `<cwd>/.my-claude-code/skills/<directory>/SKILL.md`. Discovery reads only the frontmatter header and retains `name`, `description`, the resolved `SKILL.md` path, and a `personal` or `project` source label. A valid Skill requires a closed frontmatter block plus non-empty `name` and `description` fields. Invalid, unreadable, or malformed files are ignored.

Personal Skills are discovered first, then project Skills replace entries with the same declared name. Results are sorted by name so prompt contents and startup output stay deterministic. Discovery runs again for each dynamic instruction request and each `load_skill` call, so adding or removing a Skill while the program runs is reflected without restarting.

## Progressive loading

`format_skill_listing` emits only names, descriptions, and routing guidance. It never includes the Skill body or files under `references/` and `scripts/`. `agent.core.project_context` appends this listing to the dynamic instructions.

`load_skill(name)` is a read-only Agent tool. It resolves the current effective Skill by exact name, reads the full `SKILL.md`, strips its frontmatter, and returns the body together with the resolved Skill root directory. The root lets the model resolve relative references and scripts with existing `read_file` and `run_command` tools. An unknown name raises `ModelRetry` and lists the currently available names.

## Integration and observability

The startup path reports the number and names of discovered Skills. `load_skill` is registered with the main Agent and in the read-only permission set. Subagents keep their existing curated tool lists and do not automatically receive main-Agent Skills.

The README documents directory layout, precedence, frontmatter, and the three loading layers. The changelog records the feature.

## Testing

The committed standalone script `test/test_skills.py` uses `unittest`. Its default mode verifies parsing, invalid definitions, precedence, deterministic catalogs, lazy body loading, dynamic refresh, unknown-name recovery, tool registration, read-only permissions, dynamic instruction injection, and startup output without model or network access.

An explicit `--live` mode uses the configured DeepSeek model in a temporary project. It verifies that the main Agent sees the catalog, calls `load_skill`, receives the body, and answers from the loaded workflow while unrelated reference content remains unloaded. Live mode is never run by default or by CI.
