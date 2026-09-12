# Coding Agent

基于 [pydantic-ai](https://ai.pydantic.dev/) 与 DeepSeek 的终端编程助手。可以读写文件、执行 shell 命令、根据错误迭代修复，并暴露每一轮模型 API 调用的元数据用于调试。

## 功能

- **工具调用**：文件读取、精确编辑与写入，shell 命令，任务管理和交互提问。
- **后台子代理**：`run_agent` 立即返回 job id，独立上下文执行，完成通知携带最终报告；支持并行派发、日志查看和 `job_kill` 终止。
- **权限审批**：子代理每次工具调用单独检查权限，需要人工审批时排队等待主界面空闲。
- **图片输入**：剪贴板粘贴、`@图片路径` 和 `read_file` 三条路径统一走校验与组装，支持 PNG / JPEG / GIF / WebP。
- **斜杠命令**：
  - `/new` 开启新会话
  - `/status` 显示当前会话状态（模型 / 历史 / token 累计）
  - `/api-detail` 显示最近一轮 user input 触发的所有 model API 调用元数据
  - `/agents` 列出内置和项目自定义的子代理类型
  - `/jobs` 查看后台 shell 和子代理任务
  - `/resume` 恢复历史会话
  - `/rewind` 回退文件与对话检查点（包括子代理通过文件工具做出的修改）
  - `/compact` 压缩上下文
  - `/dream` 整理长期记忆
  - `/help` 显示可用命令
  - `/exit` 退出程序
- **可视化**：基于 [rich](https://rich.readthedocs.io/) 的 Markdown 渲染、thinking / tool-call / tool-return 分块显示
- **API 元数据采集**：通过 pydantic-ai hooks 抓取每次模型调用的 request / response 元数据，便于优化与复盘

## 安装

要求 Python ≥ 3.12，推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
uv sync
```

默认包源与锁文件统一为阿里云镜像。若本机 `UV_DEFAULT_INDEX` 覆盖成不可访问的镜像，
可使用 `uv sync --default-index https://mirrors.aliyun.com/pypi/simple/`。

## 配置

在 `agent/` 目录下创建 `.env`（参考 `agent/.env.example`）：

```bash
cp agent/.env.example agent/.env
# 编辑 agent/.env，填入真实 DEEPSEEK_API_KEY
```

环境变量：

| 变量 | 用途 | 默认值 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥（必填） | - |
| `DEEPSEEK_API_BASE` | DeepSeek API 基址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 文本轮的模型名（普通提问、subagent、压缩、记忆提炼） | `deepseek-v4-flash` |
| `DEEPSEEK_VISION_MODEL` | 含图片的一轮切换到的视觉模型名；设成空串表示不提供图片输入 | `deepseek-v4-flash-vision-exp` |

## 运行

```bash
python main.py
```

启动后输入 `/help` 查看可用命令，或直接输入需求与 Agent 对话。

## 图片输入

三条路径都会经过 `images.load_image` 的同一层校验（扩展名与文件签名必须一致），再交给
`images.build_user_content` 按占位符位置组装成一轮图文消息：

```text
Ctrl+V（macOS/Linux）或 Alt+V（Windows）粘贴剪贴板图片
@screenshots/error.png 分析这个错误
请用 read_file 查看 /absolute/path/to/error.webp
```

粘贴后输入框会显示 `[Image #1]` 和待发送张数；ESC 或 `/new` 清空附件，发送成功后附件即被消费，
下一轮纯文本对话不会再带上它。GIF 按原始 GIF 载荷发送，能否逐帧理解取决于所配置的提供方。

限制与错误：

- 单张图片上限 **10 MiB**，单轮最多 **8 张**（剪贴板粘贴与 `@` 引用共用这个额度）。
- 不支持的格式、空文件、损坏文件、扩展名与内容不一致、超出大小或数量上限，都会给出带路径的
  可操作报错；本地校验失败时已粘贴的图片会保留，改完可以直接重发。

模型路由：

- 普通文本轮用 `DEEPSEEK_MODEL`；本轮内容里含图片块时整轮切到 `DEEPSEEK_VISION_MODEL`，
  `/status` 会同时显示两个模型名。
- 视觉模型未配置（`DEEPSEEK_VISION_MODEL` 为空串）时，图片轮会明确报错而不会静默降级。
- 注意：如果用纯文本提问让模型「用 read_file 读某张图」，这一轮由内容路由判为文本轮，
  模型必须是具备视觉能力的那个。需要这条路正常工作时，把 `DEEPSEEK_MODEL` 也设成视觉模型即可。

## 项目结构

```
coding_agent/
├── main.py              # REPL 入口，主循环
├── images.py            # 图片校验、附件组装与剪贴板读取
├── agent/
│   ├── core.py          # Agent 实例化（model / instructions / tools / hooks）
│   ├── tools/           # file / shell / agents / task / ask_user 工具
│   ├── model.py         # 主 Agent 与子代理共用的模型配置
│   ├── deps.py          # 文件状态、检查点与 job 注册表依赖
│   ├── hooks.py         # Hooks：抓取每次 model API 调用元数据
│   └── .env.example     # 环境变量模板（真实 .env 已被 .gitignore 忽略）
├── subagents.py         # 子代理类型、执行器与审批队列
├── background_jobs.py   # shell / agent 后台任务生命周期
├── session.py           # 会话持久化
├── compact.py           # 上下文压缩
├── file_history.py      # 文件检查点
├── memory/              # 长期记忆召回与提炼
├── mcp_servers.py       # MCP 服务配置与连接
├── tests/               # 离线回归测试
└── UI/
    ├── input_ui.py      # 输入框、后台任务计数与快捷键
    ├── render.py        # 终端渲染原语（console / print_step / banner）
    └── commands.py      # 斜杠命令系统与 SessionState
```

## 依赖

- `pydantic-ai>=2.18.0` - Agent 框架
- `prompt-toolkit>=3.0` - 终端输入（光标编辑、历史）
- `python-dotenv>=1.0` - `.env` 加载
- `rich>=13.0` - 终端 Markdown / 彩色输出
- `questionary>=2.1` - 历史会话与检查点选择

## 使用子代理

直接输入「派一个 explore 子代理调查项目结构，完成后给我报告」。主 Agent 调用
`run_agent(description, prompt, agent_type)` 后即可继续处理其他输入，完成报告通过
`<task-notification>` 的 `<result>` 字段交回。状态栏分别显示后台 `agent` 和 `shell` 数量。
`/jobs` 提供每个任务的状态及日志路径，日志位于
`~/.my-claude-code/jobs/<session_id>/<job_id>.log`。

内置 `explore` 用于调查，仅提供 `read_file` 和 `run_command`，指令限制 shell 只做查看与搜索；
`general` 额外提供 `edit_file` 和 `write_file`。子代理不会继承主对话历史，
也不能继续派发子代理、调用提问工具或修改主会话任务面板。每个子代理最多请求模型 40 次。

在项目的 `.my-claude-code/agents/reviewer.md` 中可以定义自己的类型：

```markdown
---
name: reviewer
description: 代码审查专家，检查正确性和潜在 bug
tools: read_file, run_command
---

只读审查代码，输出按严重程度排序的问题清单，并附上文件路径。
```

启动时加载这些文件。`name`、`description` 和正文必填；`tools` 省略时提供上述四种工具，
工具名拼错的配置会被跳过并记录警告。可用类型同时出现在 `/agents` 和主 Agent 的动态指令中。

派发动作免审批，实际操作按当前权限模式检查；`auto` 审查失败时回退到人工审批，
用户拒绝时将原因返回子代理。终止任务会清除待处理审批；切换会话和退出会等待任务清理，
子代理启动的后台命令也随之结束。

## 测试

```bash
uv sync
uv run pytest -q
```

测试使用本地模型替身与临时目录，覆盖派发、通知、权限、取消、会话切换、文件回退和自定义类型，
不调用真实模型 API，也不需要真实 API Key。
pytest 默认只收集 `tests/`；Git 忽略的 `test/` 中的本地手工脚本需要显式运行。

必跑的门禁是离线的，不需要 API Key，也不碰真实剪贴板：全量 pytest（含 workflow、Agent 链路、
本地 OpenAI 兼容协议和 CLI PTY E2E）、`ruff check .`、`compileall` 与 import check；
`images.py` 还要求行覆盖率 ≥ 90%、分支覆盖率 ≥ 85%。

真实模型端到端测试需单独执行（使用已配置的 API Key，会产生模型用量）：

```bash
uv run python scripts/live_subagents.py
PYTHONPATH=. no_proxy=api.deepseek.com uv run python scripts/live_images.py --repeat 3
```

`scripts/live_images.py` 覆盖直传图片、模型自己 `read_file` 读图、`@` 引用三条真实链路，
每个 `--repeat` 编号都是一次独立记录（不做隐式重试），任一场景失败即以非零码退出并点名失败编号。
它只记录场景名、模型名、耗时、是否组装出图片块和有界响应片段，不打印密钥、请求头、base64 或图片字节。

它在临时目录中验证主代理派发、子代理读取、报告回传、写文件审批和文件回退，结束后清理测试文件。
CI 仅运行离线测试。
