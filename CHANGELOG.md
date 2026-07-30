# Changelog

本项目变更遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- `auto` 权限模式：`ask` 的工具调用先交给旁路 LLM classifier 判定能否自动放行，classifier 拦截或出错时回退人工审批（fail-closed）。
- `classifier.py`：把对话历史投影成防注入转写，调用 DeepSeek 裁决单次工具调用。转写只保留用户原话和工具调用，丢弃模型文本和工具输出；参数值超长截断防撑爆。

### Fixed

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

## [0.1.0] - 2026-07-25

### Added

- 项目首个可用版本：基于 pydantic-ai + DeepSeek 的终端编程助手。
- 工具：`read_file` / `write_file` / `run_command`
- 斜杠命令：`/new` / `/status` / `/api-detail` / `/help` / `/exit`
- 基于 rich 的 Markdown 渲染与 thinking / tool-call / tool-return 分块显示。
- 通过 pydantic-ai hooks 抓取每次模型 API 调用的元数据。
