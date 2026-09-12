# 沙箱加固（第一阶段）Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 `opt_loop` 分支上完成三项应用层加固：旁路模块复用主模型配置、`run_agent` 派发审批、`explore` 只读从提示词升级为代码事实。拆成三个独立提交，每个都能单独回滚。

**Architecture:** 三处都不新增抽象层，分别复用项目已有的三种机制——`agent/model.py` 的配置单一来源、`permissions.register_self_check` 的自检挂载点、`ModelRetry` 的工具级硬拒。只读约束的判定落在 file 工具（代码层），而不是依赖提示词或 hook。

**Tech Stack:** Python 3.12、pydantic-ai 2.18、pytest（FunctionModel 替身，全离线）、ruff。

**前置状态：** 分支 `opt_loop` @ `ab95fc2`（闭环提交），工作区干净。**不使用 worktree**——本任务就在当前分支的当前目录进行（项目既有的 worktree 已清理）。

**基线门禁（开始前先跑一遍确认起点）：**

```bash
cd /Users/kuangzhiqin/PycharmProjects/coding_agent
.venv/bin/python -m pytest -q 2>&1 | tail -3
```

Expected: `1 failed, 86 passed`，唯一失败是 `tests/test_image_cli_e2e.py::test_cli_paste_submit_escape_and_new_are_hermetic`（沙箱 pty 不足，改动前即存在）。

---

## 设计澄清：一个实测发现推翻了先前假设

先前方案（`2026-09-12-sandbox-hardening-phase1.md`）断言「子代理钩子拿不到 agent 类型」，因此只读判定不能放在 `_check_sub_permission` 里。**这个断言只对了一半，需要更正：**

实测（`ctx.agent` 与真实 `Agent` 实例做身份比较）：

```
agent_is_probe_agent = True      ← RunContext.agent 就是那个 Agent 实例
capabilities         = ['hooks', 'pending_message_drain_capability', 'tool_search']
root_capability      = CombinedCapability   ← 不是 sub_hooks 本身
```

- `ctx.deps` 里确实**没有** agent 类型（这部分原判断正确）；
- 但 `ctx.agent` **是** Agent 实例，身份可比——所以按类型判断在技术上可行；
- `ctx.root_capability` 是合并后的 `CombinedCapability`，不是 `sub_hooks`，靠它反查类型不可靠。

**但结论不变：主判定仍放 file 工具，不放在 hook。** 理由是「哪个约束更硬」：

| 落点 | hook 被拆掉/替换 | 新增第 5 个写文件工具 | 走非 hook 路径调用 |
|---|---|---|---|
| `_check_sub_permission` 里判 `ctx.agent` 身份 | ❌ 失效 | ❌ 失效 | ❌ 失效 |
| file 工具里判 `deps.readonly` | ✅ 仍生效 | ✅ 仍生效（只要带 deps） | ✅ 仍生效 |

因此采取**双层**：file 工具是**权威强制**（必修项），hook 里补一层**面向子代理的可读错误信息**（因为 file 工具抛的 `ModelRetry` 在 hook 后续处理中会变成通用的「工具执行出错」，子代理拿不到那句可操作的话）。两层都要写测试。

---

## Commit 1：`fix: 旁路模块复用主模型的端点与模型名`

**性质：** 纯 bug 修复。当前 `classifier.py` 与 `memory/recall.py` 写死了 `base_url` 与模型名，无视 `agent/.env`。

**为什么必修（实测证据）：**

```
agent/.env: DEEPSEEK_MODEL="deepseek-v4-flash-vision-exp"

agent.model.MODEL_NAME      = deepseek-v4-flash-vision-exp   ← 主对话/子代理/压缩/记忆提炼
classifier.CLASSIFIER_MODEL = deepseek-v4-flash              ← 硬编码，没有跟随配置
recall.RECALL_MODEL         = deepseek-v4-flash              ← 硬编码
```

即：**auto 模式的安全审查用的模型与你配的主模型不是同一个**，且两条链路（安全审查、记忆召回）都把内容发往官方 `api.deepseek.com`，绕过用户可能配置的中转/自建网关。

### Task 1.1：端点与模型名一致性测试（先红后绿）

**Files:**
- Create: `tests/test_hardening.py`
- Modify: `classifier.py:16-26`
- Modify: `memory/recall.py:16-27`
- Reference: `agent/model.py:21-32`（`API_KEY` / `API_BASE` / `MODEL_NAME` 已就绪，无需改动）

**Step 1: 写失败测试**

```python
# tests/test_hardening.py
"""沙箱加固第一阶段的离线回归：旁路模块配置一致性 + 派发审批 + explore 只读强制。"""
from pydantic_ai.models.function import FunctionModel  # noqa: F401  (占位，后续 task 用)

import agent.model as agent_model
import classifier
import memory.recall as memory_recall


def test_bypass_clients_reuse_main_model_endpoint():
    """旁路模块必须与主模型共用同一个端点，不允许各自写死。

    断言「三者一致」而不是断言某个字面量：以后谁再偷偷写死都会被这条抓住。
    """
    assert str(classifier._client.base_url).rstrip("/") == agent_model.API_BASE.rstrip("/")
    assert str(memory_recall._client.base_url).rstrip("/") == agent_model.API_BASE.rstrip("/")


def test_bypass_models_reuse_main_model_name():
    """classifier / recall 的模型名必须跟随 DEEPSEEK_MODEL，不能各自硬编码。"""
    assert classifier.CLASSIFIER_MODEL == agent_model.MODEL_NAME
    assert memory_recall.RECALL_MODEL == agent_model.MODEL_NAME
```

**Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q
```

Expected: 2 passed 吗？**不是**——测试此时**会通过**，因为当前 `.env` 的 `DEEPSEEK_API_BASE` 恰好就是官方地址，两个断言都成立。这说明**测试写法不够有力，必须先改成能抓住回归的形式**，见 Step 3。

**Step 3: 把测试改成「能抓住写死」的形式（关键步骤，别跳过）**

仅断言「等于 API_BASE」在默认配置下抓不住写死。改为**用 monkeypatch 造一个非默认端点再断言跟随**：

```python
import importlib


def _reload_with_custom_base(monkeypatch, base):
    """在自定义 DEEPSEEK_API_BASE 下重新加载旁路模块，返回 (classifier, recall, agent_model)。"""
    monkeypatch.setenv("DEEPSEEK_API_BASE", base)
    monkeypatch.setenv("DEEPSEEK_MODEL", "custom-model-name")
    import agent.model as am
    importlib.reload(am)
    import classifier as cl
    import memory.recall as rc
    importlib.reload(cl)
    importlib.reload(rc)
    return cl, rc, am


def test_bypass_modules_follow_custom_endpoint_and_model(monkeypatch):
    """配置换成自定义端点/模型名后，两个旁路模块必须跟着走——这是「没有写死」的证据。"""
    cl, rc, am = _reload_with_custom_base(monkeypatch, "https://gateway.internal/v1")
    try:
        assert str(cl._client.base_url).rstrip("/") == "https://gateway.internal/v1"
        assert str(rc._client.base_url).rstrip("/") == "https://gateway.internal/v1"
        assert cl.CLASSIFIER_MODEL == "custom-model-name"
        assert rc.RECALL_MODEL == "custom-model-name"
    finally:
        # 还原成测试环境的正常模块状态，避免污染同进程里的其他测试
        monkeypatch.undo()
        importlib.reload(am), importlib.reload(cl), importlib.reload(rc)
```

保留 Step 1 的两条「一致性」用例（它们是廉价的守卫），再加上这条「follow 自定义配置」的用例。

**Step 4: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q
```

Expected: `test_bypass_modules_follow_custom_endpoint_and_model` FAIL，断言信息里能看到 `https://api.deepseek.com`（写死的那个）≠ `https://gateway.internal/v1`。另外两条一致性用例可能仍然 PASS（因为默认配置下端点恰好一致）。

**Step 5: 实现——两个模块改为从 `agent.model` 取配置**

```python
# classifier.py —— 删掉 os / Path / load_dotenv / AsyncOpenAI 之外的自建配置
"""auto 模式分类器：……（保留原 docstring）"""
import json

from openai import AsyncOpenAI

# 端点、密钥、模型名全部复用主模型那一份配置：它是唯一事实来源，
# 否则换网关/换模型时这条链路会静默走回官方端点（曾经的 bug）。
from agent.model import API_BASE, API_KEY, MODEL_NAME

_client = AsyncOpenAI(api_key=API_KEY, base_url=API_BASE)

# 保留模块级别名：既修掉硬编码，又不破坏任何按名字引用它的调用方
CLASSIFIER_MODEL = MODEL_NAME
```

```python
# memory/recall.py —— 同样替换头部
from openai import AsyncOpenAI

from agent.model import API_BASE, API_KEY, MODEL_NAME

_client = AsyncOpenAI(api_key=API_KEY, base_url=API_BASE)

RECALL_MODEL = MODEL_NAME
```

要点：
- 删掉两个模块各自的 `load_dotenv(...)` 与 `API_KEY = os.getenv(...)`、以及「未配置就 RuntimeError」的重复检查——`agent.model` 已用绝对路径加载了 `.env` 并在缺 key 时抛错，重复一份只会漂移。
- 依赖方向已核实无环：`classifier → agent.model → images`；`memory.recall → agent.model`；`agent.model` 不反向 import 二者。
- `memory/recall.py` 里 `os` / `Path` / `load_dotenv` 若不再使用，一并删掉 import（`ruff` 的 F401 会拦）。

**Step 6: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -3
```

Expected: 新用例全 PASS；全量仍 `1 failed, 86+3 passed`（那个 pty 失败仍是既有问题）。

**Step 7: Commit**

```bash
git add classifier.py memory/recall.py tests/test_hardening.py
git commit -m "fix: 旁路模块复用主模型的端点与模型名

classifier 与 memory/recall 各自写死了 base_url 和模型名，无视
DEEPSEEK_API_BASE / DEEPSEEK_MODEL：安全审查与记忆召回会把内容发往
官方端点，且审查用的模型与主模型不一致。改为从 agent.model 取配置，
并补一条「换自定义端点后必须跟随」的回归测试。"
```

---

## Commit 2：`feat: run_agent 派发前的自检`

**性质：** 行为变更（新增审批触发）。

**设计取舍（明确否决的方案）：** 不把 `run_agent` 从 `READONLY_TOOLS` 摘掉。那样**每次**派发都要弹窗，包括最常用的「派个 explore 查代码」；用户会转而常驻 `bypass`，**净安全收益为负**。改为复用既有的 `register_self_check`：只在「看起来要动系统」时要求审批，同时保留只读派发的免打扰路径。这仍然达成原目标——派发不再是无条件白名单，而是**有条件的**。

**自检优先级已核实**（`permissions.compute_decision` 内的顺序）：

```
bypass 判断 → TOOL_SELF_CHECKS → session_allowed → READONLY_TOOLS
```

自检在 `session_allowed` 与 `READONLY_TOOLS` **之前**，所以「本会话不再询问」盖不过它（与 `run_command` 的高危自检行为一致）。

### Task 2.1：派发自检矩阵测试

**Files:**
- Modify: `tests/test_hardening.py`（追加用例）
- Modify: `agent/tools/agents.py`
- Modify: `subagents.py`（新增 `READONLY_TOOL_NAMES` 常量，见 Task 3.1 的依赖说明）

**Step 1: 写失败测试**

```python
import permissions
from agent.tools.agents import run_agent_self_check


def test_dispatch_self_check_asks_for_write_capable_agents():
    """带写能力的子代理类型：派发要审批。"""
    assert run_agent_self_check({"agent_type": "general", "prompt": "写个测试", "description": "x"}) == "ask"


def test_dispatch_self_check_allows_plain_readonly_dispatch():
    """只读类型的常规派发仍然免审批——保住免打扰路径。"""
    assert run_agent_self_check({"agent_type": "explore", "prompt": "调查项目结构", "description": "x"}) is None


def test_dispatch_self_check_asks_for_destructive_prompt_even_when_readonly():
    """类型只读但意图破坏性：也要审批。"""
    for bad in ("rm -rf build 然后报告", "sudo 改一下权限", "把这个目录 clean 掉"):
        assert run_agent_self_check({"agent_type": "explore", "prompt": bad, "description": "x"}) == "ask", bad


def test_dispatch_self_check_registered_and_beats_session_allowlist(monkeypatch):
    """自检必须真的挂上，且优先级高于会话白名单。"""
    assert "run_agent" in permissions.TOOL_SELF_CHECKS
    permissions.state.mode = permissions.DEFAULT
    permissions.state.session_allowed.add("run_agent")
    try:
        # 即便用户点过「本会话不再询问 run_agent」，破坏性派发仍要被拦下
        assert permissions.compute_decision(
            "run_agent", {"agent_type": "general", "prompt": "x", "description": "x"}
        ) == "ask"
    finally:
        permissions.state.session_allowed.clear()
```

**Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q -k dispatch
```

Expected: `ImportError: cannot import name 'run_agent_self_check'`。

**Step 3: 实现——`agent/tools/agents.py` 追加自检并注册**

```python
# 追加到文件末尾（import 段补 permissions 与 shell 的 DANGEROUS_PATTERNS）

# prompt 里出现这些词说明模型打算让子代理改动系统，而不是「看看代码」。
# 与 shell 的 DANGEROUS_PATTERNS 互补：那套认命令，这套认自然语言意图。
_DESTRUCTIVE_PROMPT_WORDS = re.compile(
    r"删除|覆盖|清空|重置|回滚|强制|"
    r"\brm\b|\bdelete\b|\bremove\b|\boverwrite\b|\bclean\b|\breset\b|\bchmod\b|\bchown\b|\btruncate\b",
    re.IGNORECASE,
)


def run_agent_self_check(args: dict):
    """
    run_agent 的权限自检：派发本身不改动系统，但它是「用户还在场、意图最清楚」的唯一时刻，
    错过这个时刻就只能等子代理在后台跑起来后靠它自己的工具调用审批兜底。

    两条触发条件，命中任一即要求审批：
    1. 目标类型带写能力（general 或自定义的写类型）——它后面一定会写盘；
    2. prompt 出现破坏性意图词——即使类型声明为只读，也不能让它带着删库指令静默跑。
    只读类型的常规派发（「调查 X」「找出 Y 在哪」）仍然免审批。
    """
    # 函数内 import：subagents 顶层 import 了 agent 工具链，模块级 import 会循环依赖
    import subagents

    atype = subagents.get_agent_type(str(args.get("agent_type", "")))
    # 类型不存在时放行，让 run_agent 自己抛 ModelRetry 给出「可用类型」清单，避免两处重复报错
    if atype is not None and not atype.readonly:
        return "ask"
    if _DESTRUCTIVE_PROMPT_WORDS.search(str(args.get("prompt", ""))):
        return "ask"
    # shell 的高危命令特征也扫一遍：prompt 里可能直接贴命令
    from .shell import DANGEROUS_PATTERNS
    if any(re.search(pattern, str(args.get("prompt", ""))) for pattern in DANGEROUS_PATTERNS):
        return "ask"
    return None


permissions.register_self_check("run_agent", run_agent_self_check)
```

**Step 4: 同步更新模块 docstring（现在它说的是「派发免审批」）**

把 `agent/tools/agents.py` 顶部 docstring 改为：

```python
"""
run_agent 工具：把任务委托给一个 sub agent，它在后台的 agent 型 job 里独立运行。

工具本身不改动任何东西，真正动系统的是 sub agent 后续的每一次工具调用——
权限在那一层把关（权限下沉）；派发这一步额外加一道自检（run_agent_self_check）：
带写能力的类型或带破坏性意图的 prompt 要用户点头，只读类型的常规派发免审批。
"""
```

**Step 5: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -3
```

**注意可能的连锁失败：** `tests/test_subagents.py` 里有直接调 `run_agent(...)` 的用例。走 `agent.iter` 的路径会经过 tool_execute hook → `compute_decision` → 新自检 → `ask`。这些测试若没把 `permissions.state.mode` 设成 `bypass`，会挂在审批等待上（表现为超时或 hang）。**先跑一遍看结果**；若失败，在这些用例的 setup 里加 `permissions.state.mode = permissions.BYPASS`（与 `tests/test_image_agent_integration.py` 的 `configure_run` 同款做法），这是测试适配，不是实现缺陷。

**Step 6: Commit**

```bash
git add agent/tools/agents.py tests/test_hardening.py tests/test_subagents.py
git commit -m "feat: run_agent 派发前加自检

派发免审批 + 权限下沉的组合有个缺口：子代理在后台跑，它要弹的审批得等
主界面空闲才冒泡，而派发那一刻才是用户在场、意图最清楚的时刻。
新增自检（复用 shell 的 register_self_check 机制）：目标类型带写能力、
或 prompt 含破坏性意图时要求审批；只读类型的常规派发仍免审批——
不把 run_agent 踢出白名单，是为了不把免打扰路径变成打扰路径。"
```

---

## Commit 3：`feat: explore 只读由提示词升级为代码强制`

**性质：** 行为变更（explore 无法再写盘）。

**当前问题：** `EXPLORE_INSTRUCTIONS` 写着「严禁做任何修改」，但代码层零强制——实测 explore 挂载的是完整的 `run_command`，只读性 100% 依赖模型听话。

**readonly 默认值决策（不需要你额外拍板，见下方理由）：** 自定义 agent 的 `readonly` **从 `tool_names` 自动推导**，不引入会静默改变行为的默认值：

```python
readonly = bool(meta.get("readonly", "").lower() == "true")
if "readonly" not in meta:
    # 没显式声明时按工具表推导：配了写工具=可写，只配只读工具=只读。
    # 这样既有自定义 agent 的行为逐字节不变（声明了 write 的照旧能写）。
    readonly = not (set(tool_names) & _WRITE_TOOL_NAMES)
```

内置两个类型显式声明（`explore=True`、`general=False`），不靠推导，避免以后调工具表时悄悄改变只读语义。

### Task 3.1：`readonly` 状态与类型声明

**Files:**
- Modify: `agent/deps.py`（**字段必须追加在末尾**）
- Modify: `subagents.py:144-160`（`AgentType`）、`:248-261`（内置注册）、`:292-325`（自定义加载）、`:336-351`（`run_subagent` 派生 deps）
- Test: `tests/test_hardening.py`

**Step 1: 写失败测试**

```python
import dataclasses

import subagents
from agent.deps import AgentDeps


def test_agent_deps_readonly_is_appended_last():
    """readonly 必须追加在字段表末尾：中间插队会让位置参数构造方错位（已踩过一次）。"""
    names = [f.name for f in dataclasses.fields(AgentDeps)]
    assert names[-1] == "readonly"


def test_builtin_agent_types_declare_readonly():
    assert subagents.get_agent_type("explore").readonly is True
    assert subagents.get_agent_type("general").readonly is False


def test_explore_has_no_write_tools():
    """只读类型不应该挂载写文件工具（第一道防线）。"""
    assert not (set(subagents.get_agent_type("explore").tool_names) & subagents._WRITE_TOOL_NAMES)
```

**Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q -k readonly
```

Expected: `AttributeError: 'AgentDeps' object has no attribute 'readonly'` / `AttributeError: ... '_WRITE_TOOL_NAMES'`。

**Step 3: 实现 `agent/deps.py`**

```python
@dataclass
class AgentDeps:
    read_file_state: ReadFileState
    tasks_store: TasksStore | None
    file_history: FileHistory | None = None
    job_registry: JobRegistry | None = None
    subagent_job: Job | None = None
    iteration: IterationState | None = None
    # 只读子代理（explore 及自定义只读类型）为 True：写文件工具据此硬拒。
    # 刻意追加在字段表末尾——中间插队会让用位置参数构造 deps 的调用方整体错位
    readonly: bool = False
```

**Step 4: 实现 `subagents.py` 的类型声明**

```python
# 哪些工具属于「写」：只读类型不允许挂载，运行期也会被 file 工具硬拒
_WRITE_TOOL_NAMES = {"edit_file", "write_file"}


@dataclass
class AgentType:
    name: str
    description: str
    tool_names: list[str]
    instructions: str
    source: str
    # 只读类型：写文件工具不挂载，且即使挂上也会在运行时被硬拒
    readonly: bool = False
    agent: Agent = field(init=False, repr=False)
```

内置注册分别加 `readonly=True`（explore）与 `readonly=False`（general）。自定义加载改为：

```python
tool_names = [t.strip() for t in str(meta.get("tools", "")).split(",") if t.strip()]
if not tool_names:
    tool_names = list(_TOOL_FUNCS)
unknown_tools = set(tool_names) - _TOOL_FUNCS.keys()
if unknown_tools:
    logger.warning("跳过自定义 agent %s：未知工具 %s", path, ", ".join(sorted(unknown_tools)))
    continue
# 显式 readonly 优先；没写就按工具表推导，保证既有自定义 agent 行为不变
declared = str(meta.get("readonly", "")).strip().lower()
readonly = declared == "true" if declared else not (set(tool_names) & _WRITE_TOOL_NAMES)
_register(AgentType(
    name=name, description=description, tool_names=tool_names,
    instructions=body.strip(), source=str(path), readonly=readonly,
))
```

`run_subagent` 的 deps 派生加一个字段：

```python
deps = dataclasses.replace(
    parent_deps,
    read_file_state=ReadFileState(),
    tasks_store=None,
    job_registry=sub_registry,
    subagent_job=job,
    readonly=atype.readonly,      # ← 新增
)
```

**Step 5: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q -k readonly
```

### Task 3.2：file 工具的权威强制

**Files:**
- Modify: `agent/tools/file.py`（`edit_file` 与 `write_file` 各一处）
- Test: `tests/test_hardening.py`

**Step 1: 写失败测试**

```python
import pytest
from pydantic_ai.exceptions import ModelRetry

from agent.file_state import ReadFileState
from agent.tools import file as file_tool


def _deps(readonly):
    return AgentDeps(read_file_state=ReadFileState(), tasks_store=None, readonly=readonly)


def test_readonly_deps_refuses_write_file(tmp_path):
    target = tmp_path / "x.py"
    with pytest.raises(ModelRetry, match="只读"):
        file_tool.write_file(SimpleNamespace(deps=_deps(True)), str(target), "x")
    assert not target.exists()          # 关键：拒绝发生在写盘之前


def test_readonly_deps_refuses_edit_file_after_read(tmp_path):
    target = tmp_path / "y.py"
    target.write_text("a\n", encoding="utf-8")
    deps = _deps(True)
    ctx = SimpleNamespace(deps=deps)
    file_tool.read_file(ctx, str(target))     # 先满足「先读后写」，证明拦的是只读而不是没读
    with pytest.raises(ModelRetry, match="只读"):
        file_tool.edit_file(ctx, str(target), "a", "b")
    assert target.read_text(encoding="utf-8") == "a\n"


def test_writable_deps_still_write(tmp_path):
    """不能误伤：readonly=False 时写盘照常成功。"""
    target = tmp_path / "z.py"
    ctx = SimpleNamespace(deps=_deps(False))
    assert "已写入" in file_tool.write_file(ctx, str(target), "ok\n")
    assert target.read_text(encoding="utf-8") == "ok\n"
```

**Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q -k readonly
```

Expected: 前两条 FAIL（`DID NOT RAISE ModelRetry`）——现在只读 deps 也能写盘。

**Step 3: 实现——两处硬拒（都放在写盘动作之前）**

```python
# agent/tools/file.py —— 加一个共用助手，放在 _mark_edit 旁边
def _refuse_if_readonly(ctx: RunContext[AgentDeps], path: str) -> None:
    """
    只读子代理的权威强制点。放在 file 工具里而不是权限 hook 里：
    即便 hook 被拆掉、被替换，或将来又新增一个写文件工具，这条约束依然成立。
    """
    if ctx.deps.readonly:
        raise ModelRetry(
            f"当前是只读子代理，禁止修改文件（{path}）。"
            "请改用只读方式完成任务（read_file / 只读的 run_command），"
            "或在最终报告里说明这一步没有做。"
        )
```

`edit_file`：在第 2 步「先读后写检查」**之前**调用 `_refuse_if_readonly(ctx, path)`——
只读拒绝要优先于一切别的校验，否则会先抛出「还没读过这个文件」把原因带偏。

`write_file`：在函数体第一行调用 `_refuse_if_readonly(ctx, path)`。

**Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_hardening.py -q -k readonly
```

### Task 3.3：hook 层补可读错误（让子代理拿到可操作的话）

**Files:**
- Modify: `subagents.py` 的 `_check_sub_permission`（三级之前插入第 0 级）
- Test: `tests/test_hardening.py`

**Step 1: 写失败测试**

```python
def test_sub_hook_refuses_write_for_readonly_agent():
    """只读子代理的写调用在 hook 层就被回填一句可操作的话，而不是落到通用错误处理器。"""
    import asyncio
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Hooks

    # 用真实类型表构造一个只读 Agent，验证 hook 拦住写调用
    atype = subagents.get_agent_type("explore")
    calls = []

    def respond(messages, info):
        if not calls:
            calls.append(1)
            return ModelResponse(parts=[ToolCallPart(tool_name="write_file", args='{"path": "x.py", "content": "x"}', tool_call_id="1")])
        return ModelResponse(parts=[TextPart("done")])

    readonly_agent = Agent(FunctionModel(respond), tools=subagents._build_tools(["read_file"]), capabilities=[subagents.sub_hooks])
    deps = AgentDeps(read_file_state=ReadFileState(), tasks_store=None, readonly=True)
    result = asyncio.run(readonly_agent.run("go", deps=deps))
    # 工具不存在时 SDK 会报未知工具；这里断言的是「没有真的执行写盘」这一事实
    assert not (__import__("pathlib").Path("x.py")).exists()
```

> 注：如果发现「工具不存在」这条路径让断言变得脆弱，就把这条测试改为直接同步调用
> `subagents._check_sub_permission(ctx, call=..., tool_def=..., args=..., handler=fake)` 并断言
> 返回文本含「只读」。以**能稳定复现**为准，不要为了好看牺牲可靠性。

**Step 2: 跑测试确认失败**

**Step 3: 实现——在 `_check_sub_permission` 顶部插入第 0 级**

```python
async def _check_sub_permission(ctx, *, call, tool_def, args, handler):
    """……（保留原 docstring，补一句第 0 级说明）"""
    # 第 0 级：只读子代理的写调用直接回填可操作说明。
    # 之所以在 hook 层也要拦一次：file 工具抛的 ModelRetry 会被通用错误处理器转成
    # 「工具执行出错」，子代理拿不到那句「请改用只读方式」的话，容易反复重试同一个写操作。
    # 真正的权威强制仍在 file 工具里（hook 被拆掉也依然生效）。
    if ctx.deps.readonly and call.tool_name in subagents_write_tools():
        return (
            f"当前是只读子代理，禁止调用 {call.tool_name}。"
            "请改用只读方式完成任务，或在最终报告里说明这一步没有做。"
        )
    # 第 1 级：规则能直接放行的（只读工具、bypass 模式等）照常放行
    ...
```

**Step 4: 跑测试确认通过**

**Step 5: 全量回归**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -3
```

Expected: `1 failed, 9x passed`（那个 pty 失败仍是既有问题）。

**Step 6: Commit**

```bash
git add agent/deps.py agent/tools/file.py subagents.py tests/test_hardening.py
git commit -m "feat: explore 只读由提示词升级为代码强制

EXPLORE_INSTRUCTIONS 写着「严禁做任何修改」，但代码层零强制，只读性完全
依赖模型听话。现在 AgentDeps 带 readonly 事实（追加在字段表末尾，避免位置
参数错位），由 AgentType 声明：内置 explore=True / general=False，自定义
类型按工具表推导（不静默改变既有行为）。权威强制在 file 工具里（hook 被
拆掉也生效），hook 层再补一句可操作的回填信息。"
```

---

## Commit 4：`docs: 记录本阶段边界`

**Files:**
- Modify: `README.md`（权限小节 + 子代理小节）
- Modify: `CHANGELOG.md`（`### Fixed` / `### Added`）
- Reference: `docs/plans/2026-09-12-sandbox-hardening-phase1.md`（方案）、本文件（计划）

**Step 1: README 必须写清的两条边界（不允许粉饰）**

1. **本阶段仍未提供内核级隔离**：三项加固都是应用层强制；用户点「允许」之后仍是完整用户权限执行。
2. **只读不限制 `run_command`**：explore 理论上能用 `echo x > file` 绕过「写文件」拦截；它仍受审批层覆盖（`run_command` 恒为 ask 起跳），不存在静默写入。

同时更正这两处已过时的描述：
- `README.md` 里「派发动作免审批」→ 改为「只读类型的常规派发免审批；带写能力的类型或破坏性 prompt 要审批」。
- `README.md` 里 explore 的「指令限制 shell 只做查看与搜索」→ 明确「只读由代码强制写文件，shell 仍靠指令 + 审批」。

**Step 2: CHANGELOG**

```markdown
### Fixed

- `classifier` 与 `memory/recall` 写死了端点与模型名：安全审查和记忆召回会绕过
  `DEEPSEEK_API_BASE` 打到官方端点，且审查用的模型与主模型不一致。现统一从 `agent.model` 取配置。

### Added

- `run_agent` 派发自检：目标类型带写能力或 prompt 含破坏性意图时要求审批，只读类型仍免审批。
- 子代理只读的代码级强制：`AgentDeps.readonly` + `AgentType.readonly`，`explore` 内置为只读，
  自定义类型按工具表推导；file 工具硬拒写盘。
```

**Step 3: Commit**

```bash
git add README.md CHANGELOG.md docs/plans/
git commit -m "docs: 记录第一阶段加固的边界

写明两条不能粉饰的边界：本阶段仍无内核级隔离；只读不限制 run_command。
同时更正 README 里已过时的「派发动作免审批」描述。"
```

---

## 最终验证（全部提交完成后跑一遍）

```bash
cd /Users/kuangzhiqin/PycharmProjects/coding_agent

echo '=== 1. 全量离线测试 ==='
.venv/bin/python -m pytest -q 2>&1 | tail -3
# Expected: 1 failed（既有 pty 问题）, 9x passed

echo '=== 2. ruff ==='
export UV_CACHE_DIR=/tmp/uv-cache-dsh UV_TOOL_DIR=/tmp/uv-tools-dsh UV_TOOL_BIN_DIR=/tmp/uv-bin-dsh
~/.local/bin/uvx ruff check .
# Expected: All checks passed!

echo '=== 3. compileall ==='
.venv/bin/python -m compileall -q main.py images.py subagents.py background_jobs.py agent UI memory scripts session.py permissions.py classifier.py compact.py file_history.py mcp_servers.py mentions.py tasks_store.py skills.py tests && echo COMPILE_OK

echo '=== 4. import check ==='
DEEPSEEK_API_KEY=ci-smoke .venv/bin/python -c "import main; from agent import agent; print('IMPORT_OK')"

echo '=== 5. 图片覆盖率门禁 ==='
.venv/bin/python -m pytest tests/test_images.py --cov=images --cov-branch --cov-report= -q 2>&1 | tail -2
.venv/bin/python -m coverage json -o /tmp/coverage-check.json >/dev/null 2>&1
.venv/bin/python scripts/check_image_coverage.py 2>&1 | tail -2
```

## 验收标准

1. `classifier` / `memory/recall` 的端点与模型名**只能**来自 `agent.model`——换自定义端点后必须跟随（有测试）。
2. `general` 或带破坏性 prompt 的派发会 `ask`；`explore` 的常规只读派发仍是 `allow`。
3. `explore` 的写盘在**代码层**被拒（`deps.readonly=True` → `ModelRetry`），且拒绝发生在**写盘之前**（测试断言文件未创建）。
4. `general` 与主 agent 的写文件行为逐字节不变（有反向用例）。
5. 五个门禁全绿（唯一的 pty 失败与本次改动无关）。
6. 4 个提交彼此独立，可单独 `git revert`。
7. README / CHANGELOG 明写两条边界，不含「已沙箱化」之类的不实表述。

## 未决事项（执行时如遇阻，停下来问，不要自行放宽）

- `tests/test_subagents.py` 若因新自检而 hang/失败：加 `BYPASS` 适配测试，**不要**为了过测试而削弱自检。
- 若 `_DESTRUCTIVE_PROMPT_WORDS` 误伤常见派发（如「重构」含「重」），收紧词表并在测试里留一条正例。
- 本阶段**不做**：file 工具的工作区边界、真沙箱、`run_command` 命令级限制（都属第二阶段，需单独决策）。
