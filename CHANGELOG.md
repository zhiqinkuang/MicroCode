# Changelog

本项目变更遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- Eval 编排器 `scripts/eval/run_matrix.py`：按「夹具 × 版本 × 重复」批量跑并产出可比较的汇总表。
  版本定义集中在 VERSIONS 字典里（V0 基线 / V1 有 skill / V2 有 subagent / V3 全量），
  汇总**必须给出同版本内的极差**——版本间差异若小于组内极差，就无法与模型随机性区分。
- Eval 夹具 `tasks/order-report/`：多文件、需跨模块理解语义的订单报表任务。
  折扣算对了但**税基取错变量**，单看 `pricing.py` 的代码形态是「对」的，
  只有对照 README 的业务口径才能发现——用来量「探索」与「上下文隔离」，而不是一行式 bug。
  项目内自带 `money-rules` skill 讲金额约定，供「按需加载 Skill」这条机制使用。
- 夹具自证测试：守住「夹具基线必须失败」与「夹具必须有解」。
  前者防的是一个会静默烧 token 的失败模式——若谁把夹具里的 bug 顺手修好，
  基线不再失败，评测会把任务判成「无可验证起点」却仍然跑完整轮。
- Eval 管线（`scripts/eval/`）：
  - `run_task.py`：单任务运行器。每次运行在临时目录建隔离工作区与临时 HOME，
    跑基线测试（必须先失败，否则任务没有可验证起点）、驱动 agent、
    跑最终测试与**模型看不到的隐藏用例**，产出结构化运行记录。
    记录里存的是原始事实（测试结果、文件内容快照与 diff、事件流、token），
    判定留给纯函数，于是判定器改了可以对历史记录**复判**而不必重跑。
  - `judge.py`：纯粹的判定器（只读记录、不执行任何东西、不看当前文件系统）。
    判据：基线必须失败 → 最终测试通过 → **只读白名单未被改动** → 白名单内改动 →
    测试收集数未减少 → diff 里无跳过/恒真手法 → 隐藏用例通过。
    用白名单而不是「列举作弊手法」：漏掉一种手法等于放行，漏掉一条白名单只是误杀。
  - `tasks/example-fix-addition/`：演示夹具，同时是 CI 里的端到端测试素材。
- 三个**消融实验开关**（默认全关，产品默认行为一字不变）：
  `CODING_AGENT_DISABLE_SKILLS`、`CODING_AGENT_DISABLE_SUBAGENTS`，
  以及运行器的 `--permission-mode`。用于把「全量 vs 基线」拆成各机制的独立贡献。
- `tests/test_eval_pipeline.py`：28 项离线测试。含判定器对十类标注样本的
  **零误判门禁**（含改测试、删测试、跳过、硬编码、改判定配置等作弊手法），
  以及一条跑完整条链路的端到端用例。
- `tests/test_token_usage.py`：子代理 token 计量的 7 项离线回归（记在 job 上、并入累计、
  重复结算不重复计入、注册表结算契约、晚跑完的子代理由下一轮兜住、收尾结算兜底）。
- `tests/test_hardening_e2e.py`：加固相关的完整离线测试（45 项）。含三条真实端到端链路——
  ① 主 agent 经真实 `run_agent → JobRegistry.spawn_agent → run_subagent` 派发 explore，
  断言只读子代理没写出任何文件、报告回流进 `job.result`；② 同链路的 general 反向对照（必须写出）；
  ③ 把 `DEEPSEEK_API_BASE` 指向本地假服务，断言 `classifier.classify` / `recall._select`
  真的打到该端点（不是自证 `base_url` 相等）。另有权限决策矩阵、mode 循环、
  classifier 的 fail-closed 与防注入转写、file 工具的全部约束分支、iteration / reminders / job 注册表。
- 编辑—验证—纠错闭环：`edit_file` / `write_file` 写盘后记账，`run_command(verify=True)` 声明验证；
  模型想收尾但没拿到通过的验证时，`after_model_request` 闸门抛 `ModelRetry` 拒收该回合，
  把改动清单、退出码与输出尾部灌回去，驱动它继续修。最多拦停 3 次后有界放行，并要求如实交代。
- `agent/iteration.py`：`IterationState` 会话级闭环状态（改动路径、验证记录、拦停计数），
  按轮重置、不落盘；`agent/reminders.py` 的 `build_verify_reminder_text` 负责要求正文。
- `tests/test_opt_loop.py`：8 项离线回归（零拦停、拦停封顶、干活时不打扰、失败回流、
  验证通过不拦、每轮清账、builder 契约、verify 记账）。
- 渐进式 Skill 系统：发现个人级和项目级 `SKILL.md`，每轮仅注入名称与描述，使用 `load_skill` 按需加载正文。
- 项目级 `reviewing-code` 示例 Skill，以及可选真实模型链路的完整 `test/test_skills.py` 测试脚本。
- 后台 `run_agent`：内置 explore / general、自定义项目级 agent、独立上下文与最终报告通知。
- `/agents` 类型清单、分类后台任务计数、子代理审批冒泡和离线回归测试。
- `auto` 权限模式：`ask` 的工具调用先交给旁路 LLM classifier 判定能否自动放行，classifier 拦截或出错时回退人工审批（fail-closed）。
- `classifier.py`：把对话历史投影成防注入转写，调用 DeepSeek 裁决单次工具调用。转写只保留用户原话和工具调用，丢弃模型文本和工具输出；参数值超长截断防撑爆。
- 图片输入：剪贴板粘贴（Ctrl+V / Alt+V）、`@图片路径` 与后台 `read_file` 读图三条路径统一校验与组装，
  支持 PNG / JPEG / GIF / WebP，单张 10 MiB、单轮 8 张。
- 视觉模型路由：含图片的一轮切到 `DEEPSEEK_VISION_MODEL`，普通文本轮继续用 `DEEPSEEK_MODEL`，`/status` 同时显示两者。
- `scripts/live_images.py` 真实视觉发布冒烟、`scripts/check_image_coverage.py` 分支覆盖率门禁，
  以及离线覆盖率单测、附件生命周期、FunctionModel Agent 链路、本地 OpenAI 兼容协议与 CLI PTY E2E。

### Fixed

- **子代理的 token 用量完全没被计入**：`main.py` 只把主 agent 的 `result.usage` 累加进
  `SessionState`，而子代理在 `run_subagent` 里独立跑，它的用量既不入 job 也不入会话累计，
  `/status` 看到的一直是偏低的数字。子代理恰是 token 大户（全新上下文要完整交代背景 +
  最多 40 轮 + 最终报告），所以任何用过 `run_agent` 的一轮，token 统计都系统性偏低。
  现在 `run_subagent` 把用量记进 `job.usage`，`SessionState.settle_job_usage()` 幂等地
  并入累计（`job.usage_settled` 保证只并一次）。
  **结算只发生在能改到 `state` 的地方**：`run_agent_loop` 的首尾与 `watch_jobs` 的空闲轮询。
  刻意不在 `build_job_reminder_text` 里结算——那个函数只有 registry、拿不到 state，
  调用 `registry.settle_usage()` 会把 job 标记成「已结算」却不真正累加，用量永久丢失
  （实现过程中确实踩到过，由测试抓出）。
- `read_file` 在 offset 越过文件末尾时返回 `"(空文件)"`：它复用了 `read_and_register` 的返回值，
  而那个「文件只有 N 行，但 offset 是 M」的分支是给 @ 引用直接返回给模型用的，被绕过之后
  空切片走到了 `_with_line_numbers("")`。后果是模型会误判「这个文件是空的」——与事实相反，
  且导向的下一步完全错误（放弃文件 vs 改 offset）。现在该分支在 `read_file` 里也生效，
  并且这条路径不再登记 offset（不会被去重逻辑认成「读过这一段」）。
  由新增的端到端测试抓出。
- 闭环闸门消耗的是**文本输出**重试预算而不是工具预算：`Agent` 默认 `output` 预算只有 1，
  第二次拦停会以 `UnexpectedModelBehavior: Exceeded maximum output retries` 把整个 run 打挂。
  已在 `agent/core.py` 设 `retries={"output": MAX_INTERVENTIONS + 1}`。
- 新增的 `AgentDeps.iteration` 字段原本插在字段表中间，会让使用位置参数构造 deps 的调用方错位
  （子代理测试实测报 `'NoneType' object has no attribute 'spawn_agent'`）；已把它放到字段表最后。
- 图片轮偶发「我没有收到图片」：根因是系统提示词与文件工具共同构成的上下文中，模型误判内联图片不存在
  （线载荷经本地假服务验证图片块始终正确发出）。指令里明确说明图片随消息直接到达后，实测产品路径
  拒答率从 5/40 降到 0/60。
- 子代理取消后移除失效审批；审批窗口期间暂停后台通知触发的新对话。
- 切换会话和退出等待后台任务清理；清空会话级权限白名单，已终止任务保持 killed 状态。
- 自定义 agent 工具名校验，避免配置和实际可用工具不一致。
- 补充 `questionary` 运行依赖和 `pytest` 开发依赖。
- 包源与现有锁文件统一为阿里云镜像，并更新依赖锁文件。
- 修复 `agent/core.py` 因 `load_dotenv()` 无参调用导致 `.env` 无法加载、程序无法启动的问题。
  现使用绝对路径 `Path(__file__).parent / ".env"` 加载。
- 修正 DeepSeek 模型名：从无效的 `deepseek-chat` 改为 `deepseek-v4-flash`
  （DeepSeek API 现仅支持 `deepseek-v4-pro` / `deepseek-v4-flash`），
  并改为从环境变量 `DEEPSEEK_MODEL` 读取，便于切换。
- 修正 `result.usage()` 误用：pydantic-ai 2.18 中 `result.usage` 已是 `RunUsage` 对象而非方法。
- 修正 `SessionState` 字段命名不一致：`input_token`/`output_token` 统一为 `input_tokens`/`output_tokens`，
  与 `main.py` 主循环和 `cmd_status` / `cmd_new` 中的访问保持一致。
- 修正 `cmd_api_detail` 中 `call.finish_reason` 字段访问错误，改为 `ApiCall.finish_response`。

### Security

- `.gitignore` 增加 `.env` / `.env.*` 忽略规则，避免泄露真实 API 密钥。
- 新增 `agent/.env.example` 作为环境变量模板，真实 `.env` 不再纳入版本控制。

### Chores

- `pyproject.toml` 补全缺失依赖声明：`prompt-toolkit` / `python-dotenv` / `rich`
  （此前仅声明 `pydantic-ai`，靠传递依赖侥幸工作）。
- 错误信息更精确：`RuntimeError("请先设置环境变量 API_KEY")` 改为
  `RuntimeError("请先在 agent/.env 中设置 DEEPSEEK_API_KEY")`。

### Chores

- 图片摘要规则下沉到 `images.summarize_content`，终端回放与会话列表共用一份实现（此前两处各写一遍，
  未知字典还可能回显 data 字段）；新增看守 INSTRUCTIONS 图片说明的回归测试，防止拒答问题静默复发。

## [0.1.0] - 2026-07-25

### Added

- 项目首个可用版本：基于 pydantic-ai + DeepSeek 的终端编程助手。
- 工具：`read_file` / `write_file` / `run_command`
- 斜杠命令：`/new` / `/status` / `/api-detail` / `/help` / `/exit`
- 基于 rich 的 Markdown 渲染与 thinking / tool-call / tool-return 分块显示。
- 通过 pydantic-ai hooks 抓取每次模型 API 调用的元数据。
