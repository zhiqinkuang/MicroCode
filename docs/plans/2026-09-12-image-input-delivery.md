# Image Input Delivery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship `feat/image-input` with validated image inputs, predictable attachment lifecycle, deterministic Agent/CLI E2E coverage, CI gates, and an opt-in real DeepSeek vision smoke test.

**Architecture:** Keep the three entry points—clipboard paste, `@path`, and `read_file`—but route all of them through one validated `images.load_image` boundary and one ordered `build_user_content` assembler. Keep network-dependent model semantics out of required CI: CI will use a `FunctionModel` and a local OpenAI-compatible HTTP server to prove the complete transport chain, while `scripts/live_images.py` remains an explicit release check. Clipboard tests inject subprocess behavior or fake platform executables and never read or overwrite the user's real clipboard.

**Tech Stack:** Python 3.12, pydantic-ai `BinaryContent`, prompt-toolkit, pytest, pytest-cov, standard-library PTY and HTTP server, GitHub Actions.

---

## Delivery decisions

- Required CI is fully offline and deterministic.
- `images.py` must reach at least 90% line coverage and 85% branch coverage.
- The full legacy repository coverage is reported but is not used as a new global gate.
- Per-image limit: 10 MiB. Per-prompt limit: 8 images. Both values are constants documented in the README.
- Extension and file signature must agree for PNG, JPEG, GIF, and WebP.
- Clipboard extraction uses a temporary file only where the operating system requires it and deletes the file in `finally`.
- `None` means the clipboard contains no image. Unsupported platform, missing command, timeout, save failure, invalid image, and limit violations raise `ImageInputError` with a user-readable message.
- Existing text-model behavior remains the default. Image prompts use `DEEPSEEK_VISION_MODEL`; the default vision model remains `deepseek-v4-flash-vision-exp`.
- A real-model test is evidence for release readiness, but does not block normal CI because provider responses can vary.

## Test layout after the change

```text
tests/
├── conftest.py
├── image_helpers.py
├── test_images.py
├── test_image_workflow.py
├── test_image_agent_integration.py
├── test_image_provider_e2e.py
└── test_image_cli_e2e.py
scripts/
└── live_images.py
```

The ignored `test/` directory remains for private scratch scripts. Production tests move into the already tracked `tests/` directory, so `.gitignore` does not need an exception.

---

### Task 1: Establish tracked image test fixtures and coverage tooling

**Files:**
- Create: `tests/image_helpers.py`
- Create: `tests/test_images.py`
- Modify: `pyproject.toml:16-30`
- Modify: `uv.lock`

**Step 1: Add the failing baseline test**

Move the dependency-free PNG generator from `test/smoke_images.py` into `tests/image_helpers.py`. Add `tests/test_images.py` with tests for `is_image`, `load_image`, and ordered placeholder replacement. Use `tmp_path`; do not use `tempfile.mkdtemp`, change the process working directory without `monkeypatch.chdir`, or call a real model.

**Step 2: Run the focused tests**

Run:

```bash
uv run pytest tests/test_images.py -q
```

Expected: existing happy-path assertions pass; the validation assertions introduced in Task 2 fail.

**Step 3: Add coverage dependencies and configuration**

Add `pytest-cov` to the dev dependency group. Configure branch coverage and omit test files. Do not apply a global fail-under to the legacy modules.

```toml
[dependency-groups]
dev = ["pytest>=8", "pytest-cov>=6"]

[tool.coverage.run]
branch = true
source = ["images"]

[tool.coverage.report]
show_missing = true
```

Refresh `uv.lock` with `uv lock`.

**Step 4: Verify collection and baseline coverage**

Run:

```bash
uv run pytest --collect-only -q
uv run pytest tests/test_images.py --cov=images --cov-branch --cov-report=term-missing -q
```

Expected: image tests are collected from `tests/`; coverage is reported and remains below the final gate until Tasks 2 and 3 are complete.

**Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/image_helpers.py tests/test_images.py
git commit -m "test: add tracked image input coverage"
```

---

### Task 2: Validate image type, size, and content at one boundary

**Files:**
- Modify: `images.py:17-45`
- Modify: `agent/tools/file.py:56-75`
- Modify: `mentions.py:41-68`
- Test: `tests/test_images.py`
- Test: `tests/test_image_workflow.py`

**Step 1: Write failing validation tests**

Add parametrized tests for:

- case-insensitive PNG/JPEG/GIF/WebP extensions;
- unsupported extension;
- missing file;
- empty file;
- file larger than `MAX_IMAGE_BYTES`;
- PNG, JPEG, GIF, and WebP signatures;
- extension/signature mismatch;
- corrupt data;
- `read_file` converting `ImageInputError` into `ModelRetry`;
- `@path` surfacing invalid image errors instead of silently dropping the image.

**Step 2: Run tests and confirm the red state**

Run:

```bash
uv run pytest tests/test_images.py tests/test_image_workflow.py -q
```

Expected: failures show that current `load_image` trusts extensions and that mention failures are swallowed.

**Step 3: Implement the validation API**

In `images.py`, add:

```python
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 8


class ImageInputError(ValueError):
    pass


def detect_media_type(data: bytes) -> str | None:
    ...


def load_image(path: str | os.PathLike[str]) -> BinaryContent:
    ...
```

Read at most `MAX_IMAGE_BYTES + 1` bytes, reject an empty or oversized payload, detect the real media type from magic bytes, and require it to match the normalized extension mapping. Error messages must include the path and the corrective action.

Update `read_file` to catch `ImageInputError` and raise `ModelRetry(str(exc))`. Stop suppressing `ImageInputError` in `build_mention_messages`; let `main.on_submit` render it through the existing exception path.

**Step 4: Run focused and regression tests**

Run:

```bash
uv run pytest tests/test_images.py tests/test_image_workflow.py -q
uv run pytest -q
```

Expected: all pass.

**Step 5: Commit**

```bash
git add images.py agent/tools/file.py mentions.py tests/test_images.py tests/test_image_workflow.py
git commit -m "feat: validate image attachments"
```

---

### Task 3: Make clipboard capture observable, testable, and self-cleaning

**Files:**
- Modify: `images.py:77-144`
- Modify: `UI/input_ui.py:210-227`
- Test: `tests/test_images.py`

**Step 1: Write failing clipboard tests**

Patch `platform.system`, the subprocess runner, and the temporary directory. Cover:

- unsupported operating system;
- macOS probe says no image;
- Linux returns zero bytes;
- Windows returns false;
- missing `osascript`, `xclip`, or PowerShell;
- probe timeout;
- save command failure;
- save produced no file or an empty file;
- saved file is corrupt;
- successful capture returns `BinaryContent`;
- temporary file is removed on success and every failure path.

**Step 2: Run the focused tests**

Run:

```bash
uv run pytest tests/test_images.py -k clipboard -q
```

Expected: failures expose the current `None`-for-every-error behavior and permanent clipboard files.

**Step 3: Refactor clipboard capture**

Give `read_clipboard_image` injectable keyword-only dependencies while retaining production defaults:

```python
def read_clipboard_image(
    *,
    system: str | None = None,
    run_command=subprocess.run,
    clipboard_dir: Path | None = None,
) -> BinaryContent | None:
    ...
```

Use `None` only for a valid probe that finds no image. Raise `ImageInputError` for operational failures. Create a unique temporary file, load it through the validated `load_image`, and unlink it in `finally`. Log internal command diagnostics through `logging`; never log image bytes or secrets.

Update `Repl._paste_image` to catch `ImageInputError` and show the message with `console.print` without modifying the buffer or attachment list.

**Step 4: Verify behavior and coverage**

Run:

```bash
uv run pytest tests/test_images.py -q
uv run pytest tests/test_images.py --cov=images --cov-branch --cov-report=term-missing -q
```

Expected: all clipboard branches pass; `images.py` reaches at least 90% line and 85% branch coverage.

**Step 5: Commit**

```bash
git add images.py UI/input_ui.py tests/test_images.py
git commit -m "fix: harden clipboard image capture"
```

---

### Task 4: Enforce attachment limits and a predictable lifecycle

**Files:**
- Modify: `images.py:48-74`
- Modify: `UI/commands.py:64-69,217-243`
- Modify: `UI/input_ui.py:97-113,180-187,218-227`
- Modify: `main.py:102-124,210-232`
- Modify: `mentions.py:41-68`
- Test: `tests/test_image_workflow.py`

**Step 1: Write failing workflow tests**

Cover:

- one and multiple clipboard attachments;
- clipboard images followed by `@image` keep stable numbering;
- repeated placeholders preserve intentional repeated image placement;
- invalid placeholders remain literal text;
- unreferenced attachments append once at the end;
- the ninth image is rejected without changing state;
- ESC clears the buffer and attachments;
- `/new` clears attachments;
- successful submit consumes attachments exactly once;
- mention parsing or validation failure leaves pending clipboard attachments available for correction;
- a later text-only turn contains no stale image.

Build `Repl` test instances with controlled buffers and apps; do not start a terminal for these state tests.

**Step 2: Run and confirm failures**

Run:

```bash
uv run pytest tests/test_image_workflow.py -q
```

Expected: limit and failure-retention cases fail.

**Step 3: Centralize capacity checks and delay consumption**

Add an `append_attachment(attachments, content) -> int` helper in `images.py`; it enforces `MAX_ATTACHMENTS` and returns the one-based number. Use it from clipboard paste and `@image` handling.

In `main.on_submit`, copy pending attachments, parse mentions, and build user content first. Clear `state.attachments` only after assembly succeeds and immediately before starting the Agent request. This keeps the user's pasted images when local validation fails while preventing reuse after a request begins.

**Step 4: Run workflow and full regression tests**

Run:

```bash
uv run pytest tests/test_image_workflow.py -q
uv run pytest -q
```

Expected: all pass.

**Step 5: Commit**

```bash
git add images.py UI/commands.py UI/input_ui.py main.py mentions.py tests/test_image_workflow.py
git commit -m "feat: manage image attachment lifecycle"
```

---

### Task 5: Preserve readable, resumable multimodal sessions

**Files:**
- Modify: `UI/commands.py:113-190`
- Modify: `session.py:32-76`
- Test: `tests/test_image_workflow.py`

**Step 1: Write failing persistence and rendering tests**

Create a `ModelRequest` containing text and `BinaryContent`, append it to an isolated session, reload it, and assert byte-for-byte image and media-type recovery. Verify `first_prompt`, history rendering, and tool-return rendering show only media type and human-readable size, with no base64 prefix or raw bytes.

Also cover malformed historical list items so `/resume` and session listing do not crash on older or partially written records.

**Step 2: Run the focused tests**

Run:

```bash
uv run pytest tests/test_image_workflow.py -k "session or summary or render" -q
```

Expected: malformed-record handling fails before implementation.

**Step 3: Implement safe summaries**

Keep pydantic-ai serialization for the initial release. Consolidate image summary formatting so `UI.commands` and `session.first_prompt` use the same rules. Treat unknown dictionaries as text summaries without exposing their `data` field. Do not introduce a separate blob store in this PR; the 10 MiB/8-image limits bound the storage cost.

**Step 4: Run focused and regression tests**

Run:

```bash
uv run pytest tests/test_image_workflow.py -q
uv run pytest -q
```

Expected: all pass and captured output contains no encoded image data.

**Step 5: Commit**

```bash
git add UI/commands.py session.py tests/test_image_workflow.py
git commit -m "fix: make multimodal sessions safe to resume"
```

---

### Task 6: Route image turns through an explicit vision model

**Files:**
- Modify: `agent/model.py:14-34`
- Modify: `agent/__init__.py`
- Modify: `agent/.env.example`
- Modify: `main.py:49-100,167-232`
- Test: `tests/test_image_agent_integration.py`

**Step 1: Write failing routing tests**

Test that a text-only prompt uses the normal model, while any content list containing `BinaryContent` passes the vision model through the `model=` argument of `agent.iter`. Test the configuration error shown when image input is used without a configured vision model. Verify subagent and text flows keep their current model.

**Step 2: Run the routing tests**

Run:

```bash
uv run pytest tests/test_image_agent_integration.py -k routing -q
```

Expected: failures show that the current branch globally replaces the default model.

**Step 3: Implement separate model configuration**

Restore `DEEPSEEK_MODEL=deepseek-v4-flash` as the normal default. Add:

```dotenv
DEEPSEEK_VISION_MODEL="deepseek-v4-flash-vision-exp"
```

Construct `vision_model` with the existing provider. Add `contains_image(content)` and select `vision_model` only for multimodal turns. Pass it to `agent.iter(..., model=selected_model)`. Keep `SessionState.model_name` useful by displaying both configured names in `/status` or the image model for the active turn.

**Step 4: Run routing and regression tests**

Run:

```bash
uv run pytest tests/test_image_agent_integration.py -q
uv run pytest -q
```

Expected: image turns use vision, ordinary turns and subagents retain existing behavior.

**Step 5: Commit**

```bash
git add agent/model.py agent/__init__.py agent/.env.example main.py tests/test_image_agent_integration.py
git commit -m "feat: route image prompts to the vision model"
```

---

### Task 7: Add deterministic Agent-chain integration tests

**Files:**
- Create: `tests/test_image_agent_integration.py`
- Test: `main.py`, `mentions.py`, `agent/tools/file.py`, `session.py`

**Step 1: Write three failing Agent scenarios**

Use pydantic-ai `FunctionModel` and `agent.override`:

1. direct `BinaryContent` prompt reaches the model in the expected text/image order;
2. the model calls `read_file` for an image and receives `BinaryContent` as the tool result;
3. `@image` becomes an attachment, enters the Agent request, and persists in session history.

Patch `memory_background.schedule`, session storage, MCP toolsets, and console rendering. Set `models.ALLOW_MODEL_REQUESTS = False` so an accidental network call fails the test.

**Step 2: Run the tests and inspect the red state**

Run:

```bash
uv run pytest tests/test_image_agent_integration.py -q
```

Expected: missing routing or lifecycle assertions fail until Tasks 4 and 6 are complete.

**Step 3: Make only the minimal integration fixes**

Fix the production seam exposed by the tests. Do not add test-only branches to production code; dependency injection belongs at model, subprocess, storage, and MCP boundaries.

**Step 4: Run integration and full regression tests**

Run:

```bash
uv run pytest tests/test_image_agent_integration.py -q
uv run pytest -q
```

Expected: all pass with model requests disabled globally.

**Step 5: Commit**

```bash
git add tests/test_image_agent_integration.py main.py mentions.py agent/tools/file.py session.py
git commit -m "test: cover image agent chains offline"
```

---

### Task 8: Add an offline provider-protocol E2E test

**Files:**
- Create: `tests/fake_openai_server.py`
- Create: `tests/test_image_provider_e2e.py`

**Step 1: Write the failing local-provider test**

Start a standard-library `ThreadingHTTPServer` on `127.0.0.1` and construct an `OpenAIChatModel` whose provider points to it. Capture the request JSON, return a minimal OpenAI-compatible chat-completion response, and assert the wire request contains the expected image data URI/media type and surrounding text in order.

**Step 2: Run the test**

Run:

```bash
uv run pytest tests/test_image_provider_e2e.py -q
```

Expected: fail until the fake response shape and model routing are correct.

**Step 3: Complete the fake provider fixture**

The server must bind only to loopback, select an ephemeral port, stop in fixture teardown, retain no image files, and redact authorization headers from failure output. Return deterministic token usage so state accounting can also be asserted.

**Step 4: Verify repeatedly**

Run:

```bash
uv run pytest tests/test_image_provider_e2e.py -q --count=10
```

If `pytest-repeat` is not added, use a ten-iteration shell loop locally and keep CI to one pytest invocation. Expected: 10/10 passes without internet access.

**Step 5: Commit**

```bash
git add tests/fake_openai_server.py tests/test_image_provider_e2e.py
git commit -m "test: verify image provider payload offline"
```

---

### Task 9: Replace the clipboard-mutating PTY script with a hermetic CLI E2E

**Files:**
- Create: `tests/test_image_cli_e2e.py`
- Reuse: `tests/fake_openai_server.py`
- Reuse: `tests/image_helpers.py`

**Step 1: Write the failing PTY test**

Launch the real `main.py` in a child PTY with:

- temporary `HOME` and working directory;
- repository root in `PYTHONPATH`;
- `DEEPSEEK_API_BASE` pointing to the fake provider;
- a fake `xclip` on Linux or fake `osascript` on macOS placed first in `PATH`;
- no user `.mcp.json` and no real API key.

Drive this flow: wait for prompt, send paste shortcut, assert `[Image #1]` and `1 张图片待发送`, submit a question, assert deterministic fake-provider reply, paste again, press ESC, assert the count disappears, submit text, assert the captured second request has no image, run `/new`, and exit with `/exit`.

**Step 2: Run and confirm the red state**

Run:

```bash
uv run pytest tests/test_image_cli_e2e.py -q
```

Expected: fail until fake clipboard executables and local provider capture are wired correctly.

**Step 3: Remove timing flakiness**

Use `select` plus monotonic deadlines and observable prompt/output markers. Remove fixed `sleep(1)` calls. Always terminate and reap the child in `finally`; on failure, include only the final bounded output slice. Skip only when PTY APIs are unavailable, with an explicit reason.

**Step 4: Verify repeatability**

Run the CLI E2E ten times on the development machine and once under Ubuntu CI. Expected: 10/10 local passes, no clipboard change, no files outside temporary directories, and no network call.

**Step 5: Commit**

```bash
git add tests/test_image_cli_e2e.py
git commit -m "test: add hermetic image input CLI e2e"
```

---

### Task 10: Add an opt-in real DeepSeek release smoke test

**Files:**
- Create: `scripts/live_images.py`

**Step 1: Port the useful live scenarios**

Port B1-B3 from `test/smoke_images.py`: direct image, model-initiated `read_file`, and `@image`. Use `argparse`, `logging`, `TemporaryDirectory`, and exit codes. Do not use `print`.

**Step 2: Add useful diagnostics**

Support `--repeat N` and record for each attempt: scenario name, configured vision model, elapsed time, whether an image block was assembled, and a bounded response excerpt. Never log API keys, authorization headers, base64, or full image bytes.

**Step 3: Bound retries without hiding failures**

Each requested repetition is an independent recorded attempt. Do not silently retry inside an attempt. Any failed scenario makes the process exit nonzero and reports the exact failed repetition.

**Step 4: Run the release smoke**

Run:

```bash
PYTHONPATH=. no_proxy=api.deepseek.com uv run python scripts/live_images.py --repeat 3
```

Expected: 3/3 passes for all three scenarios. If the provider is unavailable, report the release check as blocked rather than converting it into a CI failure.

**Step 5: Commit**

```bash
git add scripts/live_images.py
git commit -m "test: add live vision release smoke"
```

---

### Task 11: Document usage, limits, and troubleshooting

**Files:**
- Modify: `README.md:5-23,36-59,61-93,126-144`
- Modify: `agent/.env.example`

**Step 1: Add documentation assertions to the review checklist**

Confirm the README must describe all three image paths, platform shortcuts, supported formats, size/count limits, model variables, no-image behavior, and the offline/live test commands.

**Step 2: Update the README**

Add an “图片输入” section with examples:

```text
Ctrl+V（macOS/Linux）或 Alt+V（Windows）粘贴剪贴板图片
@screenshots/error.png 分析这个错误
请用 read_file 查看 /absolute/path/to/error.webp
```

State that GIF is sent as the original GIF payload and frame interpretation depends on the configured provider. Correct the existing model-default mismatch in the configuration table. Explain the 10 MiB and 8-image limits and actionable errors.

**Step 3: Document test tiers**

List required offline pytest/coverage/PTY checks separately from `scripts/live_images.py`, which consumes real API usage.

**Step 4: Verify docs and examples**

Run Ruff and the import check after copying the documented environment variable names exactly.

**Step 5: Commit**

```bash
git add README.md agent/.env.example
git commit -m "docs: document image input workflow"
```

---

### Task 12: Enforce delivery gates in CI and perform final verification

**Files:**
- Modify: `.github/workflows/ci.yml:16-27`
- Modify: `pyproject.toml`

**Step 1: Add the image coverage gate**

Add a dedicated CI step:

```bash
uv run pytest tests/test_images.py \
  --cov=images --cov-branch --cov-report=term-missing --cov-fail-under=90
```

Because `coverage.py` fail-under combines line and branch opportunities, also inspect the branch percentage in the report and keep it at or above 85%. If exact branch enforcement is required, add a small coverage verification script rather than applying an unrealistic project-wide threshold.

**Step 2: Add deterministic E2E to CI**

Run the full offline suite, including provider and PTY tests, on Ubuntu. Add `images.py` and `tests` to `compileall`. Keep the real DeepSeek script outside CI.

**Step 3: Run the complete local delivery gate from a clean environment**

```bash
uv sync --group dev
uvx ruff check .
uv run pytest -q
uv run pytest tests/test_images.py --cov=images --cov-branch --cov-report=term-missing --cov-fail-under=90
uv run python -m compileall -q main.py images.py agent UI memory scripts tests
DEEPSEEK_API_KEY=ci-smoke uv run python -c "import main; from agent import agent; import images"
git diff --check
```

Expected: every command exits 0; full pytest includes unit, workflow, Agent integration, provider E2E, and CLI PTY E2E.

**Step 4: Run release-only verification**

Run `scripts/live_images.py --repeat 3`. Record the command and 3/3 result in the PR description. Confirm `git status --short` contains only intended files and no `.env`, clipboard file, generated image, log, or coverage artifact.

**Step 5: Commit CI changes**

```bash
git add .github/workflows/ci.yml pyproject.toml uv.lock
git commit -m "ci: gate image input delivery checks"
```

---

## Pull request acceptance checklist

- [ ] PNG, JPEG, GIF, and WebP signatures and extension matching are tested.
- [ ] Empty, corrupt, oversized, missing, and unsupported images give actionable errors.
- [ ] Maximum eight attachments is enforced for clipboard and `@image` together.
- [ ] ESC, `/new`, failed local validation, successful submit, and next-turn state are tested.
- [ ] Clipboard tests do not touch the real clipboard and temporary files are removed.
- [ ] Direct image, `read_file`, and `@image` Agent chains pass with a deterministic model.
- [ ] OpenAI-compatible wire payload contains the actual image block in correct order.
- [ ] Real CLI PTY flow passes without internet access or user-state mutation.
- [ ] `images.py` is at least 90% line and 85% branch covered.
- [ ] Full regression, Ruff, compile, import, and `git diff --check` gates pass.
- [ ] Real DeepSeek vision smoke passes 3/3 or is explicitly recorded as an external release blocker.
- [ ] README and `.env.example` match actual model routing, shortcuts, formats, and limits.
- [ ] PR contains no secrets, generated images, clipboard files, logs, or unrelated changes.

## Recommended execution order

Execute Tasks 1-12 in order. Tasks 2-6 stabilize product behavior; Tasks 7-9 prove the Agent and CLI chains; Task 10 validates the real provider; Tasks 11-12 finish documentation and gates. Keep the listed small commits so failures and review comments can be isolated cleanly.
