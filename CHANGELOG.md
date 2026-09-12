# Changelog

本项目变更遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

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
