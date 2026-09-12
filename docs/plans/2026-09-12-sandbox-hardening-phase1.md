# 沙箱改造方案（第一阶段：必修三项）

**分支**：`opt_loop`（从 `main@260d71e` 拉出，闭环提交 `ab95fc2` 之上）
**日期**：2026-09-12
**范围**：**只做三项低成本必修**，不做真沙箱。真沙箱（Seatbelt / bwrap）明确列入非目标。

## 1. 背景：现在有什么、缺什么

先划清两个概念，本方案全程按这个口径写：

- **治理**：问「要不要让你做」——应用层判断，能 allow 或 ask，用户点允许后就是完整权限执行。
- **隔离**：让「想做也做不到」——内核级强制，应用层改不了。

当前审计结论（实测，不是推测）：

| 现状 | 证据 |
|---|---|
| 项目**没有任何内核级隔离** | 全仓搜 `sandbox-exec|seatbelt|bwrap|landlock|chroot|unshare|setuid|seccomp` 零命中 |
| file 工具**不做路径范围校验** | `agent/tools/file.py` 里搜 `sandbox / is_relative_to / workspace / compute_decision` 全为 False；实测 `write_file` 成功写到工作区外的 `/tmp` |
| 权限层**只 allow / ask，从不 deny** | 15 条高危判定实测全是 `ask`，唯一 deny 出口在 auto 模式的 classifier |
| 危险命令黑名单**只有 5 条正则** | `rm` / `sudo` / `dd` / `mkfs*` / `find ~|/`；`python -c`、重定向、`git clean -fdx`、`mv`、`curl|bash` 全部绕过 |
| `run_agent` **在只读白名单里免审批** | `permissions.py` 的 `READONLY_TOOLS` 含 `run_agent` |
| 「explore 只读」**只是提示词** | `EXPLORE_INSTRUCTIONS` 是自然语言约束；实测 explore 挂载的是**完整的** `run_command`，代码层没有任何强制 |
| `tests/` **零沙箱相关测试** | 权限断言仅 11 条，全是模式分支 |

本阶段只处理**应用层能被代码强制**的部分：端点一致性、派发审批、只读事实化。
**不承诺任何内核级隔离**——这一点必须在文档和 README 里说清楚，不能给出虚假的安全感。

## 2. 非目标（明确不做）

- **不做真沙箱**：不引入 Seatbelt profile / bwrap / Landlock / 容器。理由是项目定位是终端助手，
  用户本就期望它拥有自己的权限；真要隔离，更划算的是「在 DSH 沙箱或容器里跑它」，而不是在 Agent 内部自建围栏。
- **不给 file 工具加「工作区边界」**：那会改变现有产品行为（用户可能就想让它改工作区外的文件），
  属于需要单独决策的行为变更，留给第二阶段。本阶段只修 bug 与让既有承诺成立。
- **不重构 `permissions.py` 的四模式模型**（default / acceptEdits / auto / bypass 语义不变）。
- **不改 `_check_sub_permission` 的三层结构**（规则放行 → classifier → 人工审批），只在其上补一层只读强制。
- **不给 `explore` 的 `run_command` 加命令级限制**：它是本次审计之外的范围，且已被审批层覆盖。

## 3. 必修一：端点与模型名的硬编码（这是真 bug，不是加固）

### 现状

`classifier.py:21-26` 与 `memory/recall.py:24-27` 各自复制了一份客户端构造，且**写死了官方端点**：

```python
_client = AsyncOpenAI(
    api_key=API_KEY,
    base_url="https://api.deepseek.com",   # ← 无视 DEEPSEEK_API_BASE
)
CLASSIFIER_MODEL = "deepseek-v4-flash"      # ← 无视 DEEPSEEK_MODEL
```

而 `agent/model.py:32` 是正确读环境变量的：`API_BASE = os.getenv("DEEPSEEK_API_BASE", ...)`。
用户 `agent/.env` 里确实配了 `DEEPSEEK_API_BASE`。

### 影响（为什么这是必修）

1. **数据出网路径绕过用户配置**：主对话与子代理走用户配的端点，但 **auto 模式的安全审查**和
   **每轮记忆召回**静默打到官方 `api.deepseek.com`。用中转/自建网关（数据不出内网）的部署下，
   这两条链路会把用户输入与记忆内容送出去。classifier 送的是**对话转写**，recall 送的是**召回的对话内容**。
2. **模型名硬编码是当前就已经在生效的偏差**（实测，不是理论风险）。当前 `agent/.env` 里
   `DEEPSEEK_MODEL="deepseek-v4-flash-vision-exp"`，于是同一台机器上同时跑着两个不同的模型名：

   ```
   agent.model.MODEL_NAME        = deepseek-v4-flash-vision-exp   ← 主对话/子代理/压缩/记忆提炼
   classifier.CLASSIFIER_MODEL   = deepseek-v4-flash              ← 硬编码
   recall.RECALL_MODEL           = deepseek-v4-flash              ← 硬编码
   ```

   换模型（或网关只提供别的模型名）时，这两个模块会请求一个用户没配、网关可能不存在的模型名而失败；
   即便存在，也等于**安全审查用的模型与主模型不是同一个**，判据与能力都可能不一致。

### 方案

单一事实来源：两个模块都改成从 `agent.model` 取，与主对话共用同一份配置。

```python
# classifier.py / memory/recall.py
from agent.model import API_BASE, API_KEY, MODEL_NAME

_client = AsyncOpenAI(api_key=API_KEY, base_url=API_BASE)
CLASSIFIER_MODEL = MODEL_NAME      # recall 同理，删掉 RECALL_MODEL 或让它 = MODEL_NAME
```

- 删掉这两个模块各自的 `load_dotenv` 与 `API_KEY` 读取、以及「未配置就 RuntimeError」的重复检查——
  `agent.model` 已经用绝对路径加载 `.env` 并在缺 key 时抛错，重复一份只会漂移。
- 依赖方向安全：`classifier → agent.model → images`、`memory.recall → agent.model`，均无环
  （`agent.model` 不反向 import 二者）。
- `CLASSIFIER_MODEL` / `RECALL_MODEL` 保留为**模块级别名**（= `MODEL_NAME`），
  这样既修掉硬编码，又不破坏任何既有的按名字引用（测试/脚本可能引用它）。

### 验证

新增回归测试：断言 `classifier._client.base_url` 与 `recall._client.base_url` 都等于
`agent.model.API_BASE`，且 `CLASSIFIER_MODEL == RECALL_MODEL == agent.model.MODEL_NAME`。
**断言「三者一致」而不是断言「等于某个字面量」**——这样以后谁再偷偷写死都会被测试抓住。

## 4. 必修二：`run_agent` 的派发审批

### 现状与理由辨析

`READONLY_TOOLS` 含 `run_agent`，注释理由是「权限下沉：真正动系统的是 sub agent 的工具调用，那一层把关」。
这个理由**本身成立**——子代理的每次工具调用确实会再过一遍 `_check_sub_permission`（规则 → classifier → 人工审批）。
所以「派发免审批」不等于「危险操作免审批」。

但它有两个真实缺口：

1. **审批时机错位**：子代理在后台跑，审批冒泡要等主界面空闲才弹（`watch_approvals` 每 0.5s 扫一次，
   `repl.is_idle` 为真才弹）。用户可能已经离开。**派发那一刻是用户还在场、意图最清楚的时刻**，
   这个信息现在被浪费了。
2. **派发意图完全不可见**：主 agent 可以带着 destructive 的 prompt 派一个 `general`，
   用户在自己的对话里看不到任何提示（`run_agent` 不弹窗，只有日志文件里能看到）。

### 方案：自检（走既有 `register_self_check` 机制），而不是踢出白名单

**否决**「把 `run_agent` 从 `READONLY_TOOLS` 摘掉」：那样**每一次**派发都要点弹窗，
包括最常用的「派个 explore 查代码」——把免打扰路径也变成打扰路径，用户会转而常驻 bypass 模式，
**净安全收益为负**。

采纳**自检**：复用 `agent/tools/shell.py` 里 `run_command_self_check` 的既有模式
（`permissions.register_self_check`），只在「看起来要动系统」时要求审批。
自检优先级高于会话白名单（`compute_decision` 里 check 在 `session_allowed` 之前），
所以点过「不再询问」也盖不过它。

触发条件（任一命中即 `ask`）：

1. `agent_type` 不是只读类型（`general` 或自定义类型——它们带 `edit_file` / `write_file`）；
2. `prompt` 命中破坏性特征：复用 `shell.DANGEROUS_PATTERNS`（`rm` / `sudo` / `dd` / `mkfs` / `find ~|/`）
   **再加**几条 prompt 特有的语义词：`delete` / `删除` / `覆盖` / `overwrite` / `clean` / `reset --hard` / `chmod` / `chown`。

不触发的情形：「派个 explore 调查 X」这类只读派发仍然免审批——**保住免打扰路径**。

### 验证

- `run_agent(agent_type="explore", prompt="调查项目结构")` → `allow`
- `run_agent(agent_type="general", prompt="写个测试")` → `ask`
- `run_agent(agent_type="explore", prompt="rm -rf build 然后报告")` → `ask`（类型只读但意图破坏性）
- `bypass` 模式下全部 `allow`（用户显式选择，行为不变）

## 5. 必修三：explore 的只读强制，从提示词升级为代码事实

### 现状

`EXPLORE_INSTRUCTIONS` 写着「严禁做任何修改」，但代码层零强制：实测 explore 挂的就是
完整的 `run_command`，只读性 100% 依赖模型听话。

### 方案：在 `AgentDeps` 上传一个「只读」事实，由 file 工具硬拒

关键设计取舍：**为什么不在 `_check_sub_permission` 里按 agent 类型判断**（用户原提议的位置）？

因为 `sub_hooks` 是**所有子代理共用**的一个 `Hooks` 实例，钩子内部**拿不到 agent 类型**——
`AgentType` 不在 `RunContext` 上。要按类型判断就得把类型塞进 deps，那不如直接把**结论**（只读与否）
塞进 deps，判定点也就不必再查类型表。落在 file 工具里比落在 hook 里更硬：
**即使 hook 被拆掉、被替换、或将来加了新的写文件工具，约束依然生效**。

```python
# agent/deps.py —— 追加字段（必须放最后，不插队）
readonly: bool = False     # 只读子代理（explore）：file 工具硬拒写盘
```

```python
# subagents.py：注册时按类型声明
_register(AgentType(name="explore", ..., readonly=True))
_register(AgentType(name="general", ..., readonly=False))
# 自定义 agent：默认 readonly=False（它可能带 write 工具），但允许 frontmatter 显式声明
#   readonly: true  → 即使 tools 里写了 write_file，运行时也会被拒
```

```python
# subagents.run_subagent：派生 deps 时下发
deps = dataclasses.replace(parent_deps, read_file_state=..., readonly=atype.readonly, ...)
```

```python
# agent/tools/file.py：写盘前硬拒（edit_file 与 write_file 各一处）
if ctx.deps.readonly:
    raise ModelRetry(f"当前子代理是只读类型，禁止修改文件（{path}）。请改用只读方式完成任务，或在最终报告里说明这一步没有做。")
```

用 `ModelRetry` 而不是抛异常：与工具里既有的先读后写、mtime 冲突等约束形态一致，
错误会作为 retry-prompt 回填给子代理，它可以自己换个只读做法，而不是整个 job 崩掉。

### 能力矩阵（改造后）

| agent 类型 | read_file | run_command | edit/write_file | 写盘结果 |
|---|---|---|---|---|
| `explore`（内置） | ✅ | ✅ | 不挂载 + 运行时硬拒 | **代码级拒绝** |
| `general`（内置） | ✅ | ✅ | ✅ | 照旧走审批 |
| 自定义（`readonly: true`） | 按配置 | 按配置 | **即使配置里写了也拒** | **代码级拒绝** |
| 主 agent | ✅ | ✅ | ✅ | 照旧走审批 |

### 诚实记录一条边界

只读**不限制 `run_command`**。理论上 explore 可以用 `echo x > file` 绕过「写文件」的拦截。
不为此加命令级限制的理由：那需要解析 shell 语义（重定向、`tee`、`python -c`、`sed -i`……），
是本方案明确排除的范围；而且它**仍受 `compute_decision` 的审批覆盖**（`run_command` 恒为 ask 起跳），
不存在「静默写入」。这条边界要写进 README，不要假装只读等于不可写。

## 6. 测试方案（全部离线，不调真实模型）

新增 `tests/test_hardening.py`：

| 用例 | 断言 |
|---|---|
| 端点一致性 | `classifier._client.base_url == recall._client.base_url == agent.model.API_BASE` |
| 模型名一致性 | `CLASSIFIER_MODEL == RECALL_MODEL == agent.model.MODEL_NAME` |
| 派发自检矩阵 | explore+只读 prompt → allow；general → ask；explore+破坏性 prompt → ask；bypass → allow |
| explore 只读强制 | 构造 `deps.readonly=True` 调 `write_file` / `edit_file` → 都是 `ModelRetry`；`read_file` 正常 |
| general 不受影响 | `deps.readonly=False` 时写文件照常成功（确认没误伤） |
| 自定义 agent 声明 | `readonly: true` 的 `SKILL_AGENT` 走 `_register` 后 `atype.readonly is True` |

回归护栏（必须全绿）：既有的 86 项离线测试。**特别留意 `tests/test_subagents.py`**——
它用位置参数构造过 `AgentDeps`，`readonly` 字段必须追加在字段表末尾（`iteration` 之后），
否则会重演上一轮「`'NoneType' object has no attribute 'spawn_agent'`」那类错位回归。

## 7. 验收标准

1. 两个旁路模块的端点与模型名**只能**来自 `agent.model`，写死会被测试抓住。
2. `general` / 自定义类型的派发会弹审批；`explore` 的常规只读派发**仍然免审批**。
3. `explore` 调 `write_file` / `edit_file` 在**代码层**被拒（不是靠提示词），且拒绝信息对子代理可操作。
4. `general` 与主 agent 的写文件行为**逐字节不变**。
5. 全量离线 `pytest`、`ruff check .`、`compileall`、import check、图片覆盖率门禁全绿。
6. README / CHANGELOG 明确写出：「本阶段仍未提供内核级隔离；只读不限制 `run_command`」。

## 8. 实现清单与提交划分

建议拆成 **3 个提交**（便于单独回滚与将来的 cherry-pick）：

| # | 提交 | 文件 |
|---|---|---|
| 1 | `fix: 旁路模块复用主模型端点与模型名` | `classifier.py`、`memory/recall.py`、`agent/model.py`（如需导出）、`tests/test_hardening.py`（端点用例） |
| 2 | `feat: run_agent 派发自检` | `agent/tools/agents.py`、`permissions.py`（如需）、`tests/test_hardening.py`（自检用例） |
| 3 | `feat: explore 只读由提示词升级为代码强制` | `agent/deps.py`、`subagents.py`、`agent/tools/file.py`、`tests/test_hardening.py`（只读用例） |
| 4 | `docs: 记录本阶段边界` | `README.md`、`CHANGELOG.md`、本方案 |

与闭环提交 `ab95fc2` 的文件集**零交集**（闭环改的是 iteration/hooks/reminders/main/UI/tools-file-shell，
本阶段改的是 classifier/recall/permissions/subagents/deps/agents），互不冲突，可独立审阅。

## 9. 需要你先拍板的两点

1. **必修二的严格度**：我建议的是「自检」（只读派发免审批、写类型或破坏性 prompt 才问）。
   若你要更严的「一律审批」，请确认——代价是 explore 派发也要弹窗，我判断会促使你常驻 bypass，净收益为负。
2. **自定义 agent 的 `readonly` 默认值**：我建议默认 `False`（保持现状，带 write 工具就能写），
   想只读的显式在 frontmatter 写 `readonly: true`。反向（默认只读）更安全但会**静默改变**现有自定义 agent 的行为。
