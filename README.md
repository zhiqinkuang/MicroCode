# Coding Agent

基于 [pydantic-ai](https://ai.pydantic.dev/) 与 DeepSeek 的终端编程助手。可以读写文件、执行 shell 命令、根据错误迭代修复，并暴露每一轮模型 API 调用的元数据用于调试。

## 功能

- **工具调用**：`read_file` / `write_file` / `run_command`
- **斜杠命令**：
  - `/new` 开启新会话
  - `/status` 显示当前会话状态（模型 / 历史 / token 累计）
  - `/api-detail` 显示最近一轮 user input 触发的所有 model API 调用元数据
  - `/help` 显示可用命令
  - `/exit` 退出程序
- **可视化**：基于 [rich](https://rich.readthedocs.io/) 的 Markdown 渲染、thinking / tool-call / tool-return 分块显示
- **API 元数据采集**：通过 pydantic-ai hooks 抓取每次模型调用的 request / response 元数据，便于优化与复盘

## 安装

要求 Python ≥ 3.12，推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
uv sync
```

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
│   ├── tools.py         # 工具函数：read_file / write_file / run_command
│   ├── hooks.py         # Hooks：抓取每次 model API 调用元数据
│   └── .env.example     # 环境变量模板（真实 .env 已被 .gitignore 忽略）
└── UI/
    ├── render.py        # 终端渲染原语（console / print_step / banner）
    └── commands.py      # 斜杠命令系统与 SessionState
```

## 依赖

- `pydantic-ai>=2.18.0` - Agent 框架
- `prompt-toolkit>=3.0` - 终端输入（光标编辑、历史）
- `python-dotenv>=1.0` - `.env` 加载
- `rich>=13.0` - 终端 Markdown / 彩色输出
