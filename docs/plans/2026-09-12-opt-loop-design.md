# opt_loop：编辑—验证—纠错闭环设计

**分支**：`opt_loop`（从 `main@260d71e` 拉出）
**日期**：2026-09-12
**状态**：设计已定，待实现

## 1. 问题：缺的不是 loop，是闭环的判据

现有链路**已经是多轮工具循环**。pydantic-ai 的图驱动里，`CallToolsNode` 的职责原文是
"processes a model response, and decides whether to end the run or make a new request"
（`_agent_graph.py:1674`），而决定权在 `_handle_final_result()`（`_agent_graph.py:2080-2083`）：
**模型一旦返回不含 tool call 的回复，立刻 `return End(final_result)`，本轮 run 结束。**

已经具备的失败回流：

| 机制 | 位置 | 覆盖范围 |
|---|---|---|
| ModelRetry | 19 处 `agent/tools/*.py` | 工具参数/前置条件错误回填 |
| tool_execute_error hook | `agent/hooks.py:129` | 工具抛异常回填 |
| system-reminder 注入 | `agent/hooks.py:248` `_REMINDER_BUILDERS` | 文件陈旧、task 提醒、job 完成通知 |
| model_request 重试 | `agent/hooks.py:89` | HTTP 5xx / 网络抖动 |

**真正的缺口**：验证与否完全由模型自觉。`agent/core.py:32` 的「写代码，然后运行验证」是一句
**soft instruction**——模型可以无视它，写完代码直接回复「已完成」，系统不会有任何反应，
因为「结束」这个动作由模型单方面决定。这正是「一次性代码生成」体感的来源。

## 2. 目标与非目标

**目标**

1. 系统能判定「这一轮改过文件，但没有任何验证证据」，并**强制介入**。
2. 验证失败时，命令输出与退出码**持续回流**到模型的下一轮上下文，驱动它继续修。
3. 闭环**有界**：达到上限必须停下，并向用户明确交代「什么还没验证通过」。
4. 不依赖模型自觉——介入是系统行为，不是提示词请求。

**非目标（YAGNI）**

- 不自动扫描「改动了哪个函数」来精确选择测试子集，全量验证命令由声明提供。
- 系统**不擅自执行任意命令**：只注入要求、只回流结果，跑什么命令由模型声明（保留审计）。
- 不引入第 12 个工具，避免工具面继续膨胀。
- 不改 `agent.iter()` 的图驱动方式，不改 subagent 的循环语义。

## 3. 机制选择：`after_model_request` 抛 `ModelRetry`

pydantic-ai 提供三个可介入点，逐一评估后选了第三个：

- `wrap_node_run`（`abstract.py:538`）：能改写节点返回值，**理论上可把 `End` 换成
  `ModelRequestNode` 强制续跑**。最硬，但直接篡改图推进，风险与维护成本都高。**否决**。
- `before_model_request`（`abstract.py:610`）：返回可修改的 `ModelRequestContext`，
  **能改写 messages**，`_inject_reminders` 已在用。**实测否决**：注入发生在模型
  **发出请求之前**，而「模型已经回复完、正准备结束」这个时点上**不会再有一次 model request**——
  所以这种注入在收尾回合里最多只能生效一次（首版实现实测 `interventions` 恒为 1，
  做不到「持续回流」，闭环退化成一次催促）。
- `after_model_request`（`abstract.py:618`）：拿到 **response 之后**才调用，
  文档明确允许 "Raise `ModelRetry` to reject the response and ask the model to try again"。
  **选它**。它是唯一能在「收尾动作已经产生、但 `End` 还没被采纳」的窗口里把它拦下来的位置。

拦停判据是「**回复里没有任何工具调用**」——这正是 `CallToolsNode._handle_final_result`
采纳 `End` 的同一个条件（`_agent_graph.py:2080`），所以闸门打开的时机与 run 结束的时机
严格对齐，不会误伤正常的工具轮次（模型还在干活时绝不打扰）。

代价与护栏：`ModelRetry` 要花重试预算，且**收尾回合没有工具调用，走的是文本输出路径**，
消耗的是 `output` 预算而不是 `tools` 预算（首版只放开 `tools`，实测第二次拦停就以
`UnexpectedModelBehavior: Exceeded maximum output retries (1)` 把整个 run 打挂）。
现在 `agent/core.py` 设 `retries={"output": MAX_INTERVENTIONS + 1}`，保证闸门**先于预算耗尽
而按上限自己停手**。

命令执行与输出回流仍然走正常的 tool 链路（`run_command`），
因此权限审批、日志落盘、job 通知全部自动复用。

## 4. 状态：`agent/iteration.py` 的 `IterationState`

会话级状态（和 `ReadFileState` 同层），由 `main.py` 构造 `AgentDeps` 时注入。

```python
@dataclass
class VerificationRun:
    command: str
    exit_code: int | None
    output_tail: str
    passed: bool

@dataclass
class IterationState:
    edited_paths: list[str]          # 本轮被 edit_file / write_file 改过的路径（保序去重）
    verification_runs: list[VerificationRun]  # 按序记录；判定只看最后一次
    interventions: int = 0           # 已经拦停过几次
    # 模块级 MAX_INTERVENTIONS = 3：hooks 与 core.py 的重试预算共用这一个常量

    def mark_edit(self, path: str) -> None
    def mark_verification(self, command, exit_code, output) -> None
    def last_verification(self) -> VerificationRun | None
    def needs_verification(self) -> bool        # 有改动 且 最后一次验证未通过（或没验证过）
    def budget_exhausted(self) -> bool          # 拦停次数已用光
    def clear(self) -> None                     # 每轮用户输入开始时重置
```

**只看最后一次验证**：先失败后通过是闭环的正常路径；反过来「先通过、后又改坏」由测试失败
重新触发，不需要额外逻辑。**改动记录按轮重置**：`clear()` 同时把 `interventions` 归零，
上一轮的欠账不压到这一轮头上。**不落盘**：闭环是运行时概念，重放历史时不该复活「未验证」判定，
所以 `/resume` 恢复会话时直接重建为空状态——否则一恢复就被一条催验证提醒迎面砸中。

**验证命令的来源**：给 `run_command` 增加可选参数 `verify: bool = False`。
模型对「这个项目的验证方式」最清楚（pytest？`uv run pytest`？`npm test`？），
由它声明既准确又保留了审计记录。系统只负责判定「声明过没有、过没通过」。

## 5. 闭环流程

```
用户输入
  └─ IterationState.clear()
     └─ run_agent_loop → agent.iter()
          ├─ 每次 edit_file / write_file  → mark_edit(path)
          ├─ run_command(verify=True)     → mark_verification(命令, exit_code, 输出)
          │
          ├─ after_model_request（闸门 _enforce_verification）
          │    模型回复里没有工具调用（= 想收尾）且 needs_verification() 为真
          │      → interventions += 1
          │      → raise ModelRetry(验证要求)，拒绝这次收尾，逼它再来一轮
          │    提示内容：
          │      · 本轮已改动的文件清单
          │      · 上次验证命令 + 退出码 + 输出尾部
          │      · 最后一次拦停换成「别再试了，如实向用户交代」
          │
          └─ 模型继续迭代（修 → 再验证）→ 直到验证通过
                 └─ 通过 → needs_verification() 为假 → 不再拦停 → 正常结束
```

**终止条件**（三者取或）：验证通过 / 本轮无文件改动 / 拦停次数达上限。
所谓「有界」是实测出来的：模型始终不验证时，正好拦停 3 次、模型共跑 5 轮
（1 轮写文件 + 3 轮被拦 + 1 轮放行收尾），然后 run 正常结束而不是异常退出。
第 3 次拦停必须**自己带上**面向用户的交代要求（说清改了哪些文件、验证命令是什么、
失败输出的关键行、以及判断修不动的原因）——它之后模型就被放行了，没有第二次传达机会。

## 6. 与现有机制的关系

- **和 `build_reminder_text``（文件陈旧提醒）互补**：那条管「你读的东西过期了」，
  这条管「你改的东西没验证」。
- **和 task 工具互补**：task 面板管流程可见性，本机制管正确性收口。
- **和 subagent 的关系**：`run_subagent` 是独立循环，同样可以挂这套（`sub_hooks` 已有
  自己的 reminder 注入）。**首版只做主 agent**，subagent 留待验证主机制后再决定。
- **和 compact 的关系**：压缩会把历史总结掉，但 `IterationState` 是**进程内会话级状态**，
  不受压缩影响——这正是把它做成独立状态对象而不是从历史里反推的原因。
- **和 `/api-detail` 的关系**：介入会增加 model 调用次数，`/api-detail` 天然会多出几条记录，
  这是可观测的好处，不需要额外改动。

## 7. 风险与护栏

| 风险 | 护栏 |
|---|---|
| 模型不改文件却疯狂介入 | 只在 `edited_paths` 非空时拦停；纯问答轮实测零拦停 |
| 模型反复验证失败、无限循环 | `MAX_INTERVENTIONS = 3` 硬上限 + 超限强制交代 + 放行，run 正常结束 |
| 拦停消耗重试预算把 run 打挂 | `core.py` 设 `retries={"output": MAX_INTERVENTIONS + 1}`；实测只放开 `tools` 时第二次拦停就崩（收尾回合走文本输出路径，花的是 output 预算） |
| 模型还在正常调工具时被打扰 | 判据是「回复里没有工具调用」，与 `_handle_final_result` 采纳 `End` 的条件一致 |
| 模型声明 `verify=True` 却选了不相关的命令（如 `ls`） | 首版接受（选择权交给模型，输出对用户可见）；后续可加验证命令白名单配置 |
| 模型问用户问题却被当成「想收尾」拦停 | 接受这一次多余拦停：提示只是要求补验证，代价一次重试预算，有上限兜住 |
| retry-prompt 污染对话历史 | 与既有 ModelRetry 完全同一机制（工具抛 ModelRetry 早就在用），历史里的形态是 `retry-prompt` part，属于协议内合法消息 |
| 影响既有子代理/图片测试 | 实测抓到一次真回归：新字段插进 `AgentDeps` 中间会让位置参数构造方错位。已把 `iteration` 放到字段表最后，86 项离线测试全绿 |
| subagent 循环被打挂 | subagent 的 `AgentDeps` 不带 `iteration`，闸门取到 None 直接放行，行为不变 |
| 用户按 ESC 打断 | 拦停发生在一次 run 内部，`asyncio.CancelledError` 语义不变，整体仍可一次打断 |

## 8. 验收标准

1. 模型只改文件不验证 → 每次都拦停，最多 `MAX_INTERVENTIONS` 次；拦停在系统侧，终端可观测。
2. 验证命令退出码非 0 → 退出码与输出尾部随拦停提示回流，模型下一轮能看到失败原文并继续修。
3. 验证通过 → 全程零拦停，本轮正常结束，不产生额外 API 调用。
4. 连续 3 次仍失败 → 停止拦停并放行，模型被要求向用户明确交代未通过项。
5. 本轮没改任何文件（纯问答/纯查询）→ 零拦停。
6. 全部离线测试通过（FunctionModel 替身，不调真实 API），`ruff check .`、`compileall`、
   import check、图片覆盖率门禁全绿。

**实测结果（2026-09-12，`opt_loop` 分支）**：`tests/test_opt_loop.py` 8 项全绿；
全量离线 `pytest` 86 passed（唯一失败 `test_image_cli_e2e.py` 是沙箱 pty 不足，改动前即存在）；
`ruff check .` All checks passed；`compileall` / import check 通过；图片分支覆盖率 94.1%。

## 9. 实现清单（已全部落地）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `agent/iteration.py`（新增） | `IterationState` + `VerificationRun` + 输出截尾；`MAX_INTERVENTIONS = 3` |
| 2 | `agent/deps.py` | 新增 `iteration` 字段，**放在字段表最后**（不插队，保住位置参数构造方） |
| 3 | `agent/tools/file.py` | `edit_file` / `write_file` 写盘成功后 `mark_edit(path)`（抽成 `_mark_edit` 助手） |
| 4 | `agent/tools/shell.py` | `run_command` 增加 `verify: bool = False`；抽出 `_wait_foreground` 复用等待逻辑，新增 `run_verification` 记账 |
| 5 | `agent/reminders.py` | 新增 `build_verify_reminder_text(state, capped=False)` |
| 6 | `agent/hooks.py` | 新增 `_enforce_verification`，挂在 `@hooks.on.after_model_request`；新增 `_has_tool_call` 判据 |
| 7 | `agent/core.py` | `INSTRUCTIONS` 增加 verify 约定；`Agent(retries={"output": MAX_INTERVENTIONS + 1})` |
| 8 | `main.py` | `run_agent_loop` 每轮开头 `state.iteration.clear()`，并把它注入 `AgentDeps` |
| 9 | `UI/commands.py` | `SessionState.iteration` 字段；`/new` `/resume` 重建实例 |
| 10 | `tests/test_opt_loop.py`（新增） | 8 项离线回归：零拦停、拦停封顶、干活时不打扰、失败回流、通过不拦、每轮清账、builder 契约、verify 记账 |
| 11 | `README.md` / `CHANGELOG.md` | 记录机制与用法 |

## 10. 已知边界与后续

- **首版只覆盖主 agent**：subagent 的 `AgentDeps` 不带 `iteration`，行为不变。
  要扩到 subagent，只需在 `subagents.run_subagent` 派生 deps 时带上自己的 `IterationState`
  并给 `sub_hooks` 挂同一个闸门。
- **验证命令由模型声明**，系统不校验它是否真的在验证（`verify=True ls` 也能过）。
  后续可加项目级验证命令配置（如 `.my-claude-code/verify.json`）作为兜底。
- **拦停不区分「模型在提问」**：模型用纯文本向用户提问时也会被拦一次，
  代价是一次重试预算，有上限兜住，暂不为它加分类逻辑。
