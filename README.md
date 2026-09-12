# Coding Agent

基于 [pydantic-ai](https://ai.pydantic.dev/) 与 DeepSeek 的终端编程助手。可以读写文件、执行 shell 命令、根据错误迭代修复，并暴露每一轮模型 API 调用的元数据用于调试。

## 功能

- **工具调用**：文件读取、精确编辑与写入，shell 命令，任务管理和交互提问。
- **后台子代理**：`run_agent` 立即返回 job id，独立上下文执行，完成通知携带最终报告；支持并行派发、日志查看和 `job_kill` 终止。
- **权限审批**：子代理每次工具调用单独检查权限，需要人工审批时排队等待主界面空闲。
- **渐进式 Skills**：每轮只注入 Skill 名称和描述，匹配任务后再加载完整工作流。
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
| `DEEPSEEK_MODEL` | 模型名 | `deepseek-v4-flash` |

## 运行

```bash
python main.py
```

启动后输入 `/help` 查看可用命令，或直接输入需求与 Agent 对话。

## 项目结构

```
coding_agent/
├── main.py              # REPL 入口，主循环
├── agent/
│   ├── core.py          # Agent 实例化（model / instructions / tools / hooks）
│   ├── tools/           # file / shell / agents / task / ask_user 工具
│   ├── model.py         # 主 Agent 与子代理共用的模型配置
│   ├── deps.py          # 文件状态、检查点与 job 注册表依赖
│   ├── hooks.py         # Hooks：抓取每次 model API 调用元数据
│   └── .env.example     # 环境变量模板（真实 .env 已被 .gitignore 忽略）
├── subagents.py         # 子代理类型、执行器与审批队列
├── skills.py            # Skill 发现、覆盖规则、目录生成与正文加载
├── background_jobs.py   # shell / agent 后台任务生命周期
├── session.py           # 会话持久化
├── compact.py           # 上下文压缩
├── file_history.py      # 文件检查点
├── memory/              # 长期记忆召回与提炼
├── mcp_servers.py       # MCP 服务配置与连接
├── tests/               # 离线回归测试
├── test/test_skills.py  # Skill 系统完整测试（支持可选真实模型测试）
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

## 使用 Skills

每个 Skill 是一个包含 `SKILL.md` 的目录。个人 Skill 对所有项目生效，项目 Skill 只对当前目录生效；
两者声明相同 `name` 时，项目定义覆盖个人定义：

```text
~/.my-claude-code/skills/<skill-name>/SKILL.md
<project>/.my-claude-code/skills/<skill-name>/SKILL.md
```

`SKILL.md` 的 `name` 和 `description` 必填。项目内附带了代码审查示例：

```markdown
---
name: reviewing-code
description: 审查代码改动中的正确性、回归风险和测试缺口。用户要求 code review 或合并前检查时使用。
---

# 代码审查

先阅读真实 diff 和附近的调用链，再给出结论。
```

Skill 分三层加载：Agent 每轮只看到所有 Skill 的名称和描述；匹配任务后调用 `load_skill`
读取对应 `SKILL.md` 正文；正文引用的 `references/` 和 `scripts/` 文件继续通过现有文件、命令工具按需使用。
没有命中的正文和资源不会进入上下文。启动时会显示当前发现的 Skill 名称，运行期间新增或修改 Skill
会在下一次模型请求时自动反映，无需重启。

## 测试

```bash
uv sync
uv run pytest -q
```

测试使用本地模型替身与临时目录，覆盖派发、通知、权限、取消、会话切换、文件回退和自定义类型，
不调用真实模型 API，也不需要真实 API Key。
pytest 默认只收集 `tests/`；Git 忽略的 `test/` 中的本地手工脚本需要显式运行。

真实模型端到端测试需单独执行（使用已配置的 API Key，会产生模型用量）：

```bash
uv run python scripts/live_subagents.py
```

它在临时目录中验证主代理派发、子代理读取、报告回传、写文件审批和文件回退，结束后清理测试文件。
CI 仅运行离线测试。

Skill 系统的完整离线测试脚本可单独运行：

```bash
uv run python -B test/test_skills.py
```

它覆盖 frontmatter、个人/项目覆盖、无效配置、渐进式正文加载、动态刷新、工具注册、权限和
instructions 注入。真实模型链路需要显式开启，会产生模型用量：

```bash
uv run python -B test/test_skills.py --live
```
