"""
MCP server 注册表：从 .mcp.json 读取 server 配置，启动时建立连接，把连上的 server 作为 toolsets 交给 Agent。
"""
import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.toolsets.prefixed import PrefixedToolset

# 用户级 + 项目级两份配置，同名时项目级优先
USER_CONFIG = Path.home() / ".my-claude-code" / "mcp.json"
PROJECT_CONFIG = Path(".mcp.json")

LOG_DIR = Path.home() / ".my-claude-code" / "mcp-logs"


def _build_toolset(name: str, cfg: dict) -> tuple[MCPToolset, str]:
    """
    把单个 server 配置项构造成 MCPToolset：command 走 stdio（stderr 落盘日志，不污染终端 UI），
    url 走 streamable-http。统一 30s 初始化超时（uvx/npx 首次要现场拉包）、附带 server instructions。
    返回 (toolset, 传输描述)。
    """
    if "command" in cfg:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        transport = StdioTransport(
            command=cfg["command"],
            args=list(cfg.get("args") or []),
            env=cfg.get("env"),
            cwd=str(cfg["cwd"]) if cfg.get("cwd") is not None else None,
            # 子进程 stderr 重定向到日志文件，不污染终端 UI（替代旧 QuietStdioServer）
            log_file=LOG_DIR / f"{name}.log",
        )
        toolset = MCPToolset(transport, id=name, init_timeout=30, include_instructions=True)
        desc = "stdio: " + " ".join([cfg["command"], *(cfg.get("args") or [])])
        return toolset, desc
    if "url" in cfg:
        toolset = MCPToolset(
            cfg["url"], id=name, headers=cfg.get("headers"),
            init_timeout=30, include_instructions=True,
        )
        return toolset, f"http: {cfg['url']}"
    raise ValueError(f"MCP server config {name!r} must have either `command` or `url`")


@dataclass
class ServerRecord:
    """
    单个 MCP server 的连接记录。
    server 为底层 MCPToolset（负责连接与 list_tools），
    prefixed 为按 mcp__<id>__<tool> 前缀包装、交给 Agent 的 toolset。
    """
    server: MCPToolset
    prefixed: PrefixedToolset
    transport: str = ""
    # pending / connected / failed
    status: str = "pending"
    error: str = ""
    # 连接成功后由 list_tools() 填入，保留 name/description 供 /mcp 展示
    tools: list = field(default_factory=list)


# 进程级状态，/new、/resume 换会话不重连
RECORDS: list[ServerRecord] = []
_stack = AsyncExitStack()


def load_servers() -> None:
    """
    读取并合并两级配置，每个 server 的工具名前缀成全限定的 mcp__<server>__<tool> 形式。
    """
    merged: dict[str, tuple[MCPToolset, str]] = {}
    for config_path in (USER_CONFIG, PROJECT_CONFIG):
        if not config_path.is_file():
            continue
        config = json.loads(config_path.read_text())
        for name, cfg in (config.get("mcpServers") or {}).items():
            merged[name] = _build_toolset(name, cfg)

    RECORDS.clear()
    RECORDS.extend(
        ServerRecord(
            server=toolset,
            prefixed=toolset.prefixed(f"mcp__{toolset.id}_"),
            transport=desc,
        )
        for toolset, desc in merged.values()
    )


async def startup() -> str:
    """
    并发连接配置的 server，返回一行连接摘要；单个失败只记录在案，不影响其余 server 和程序启动。
    """
    load_servers()
    await asyncio.gather(*(_connect(record) for record in RECORDS))

    connected = [r for r in RECORDS if r.status == "connected"]
    failed = [r for r in RECORDS if r.status == "failed"]
    parts = []
    if connected:
        parts.append(
            "已连接 MCP server：" + "、".join(f"{r.server.id}（{len(r.tools)} 个工具）" for r in connected)
        )
    if failed:
        parts.append("连接失败：" + "、".join(r.server.id for r in failed) + "（详情见 /mcp）")
    return "；".join(parts)


async def _connect(record: ServerRecord) -> None:
    try:
        await _stack.enter_async_context(record.server)
        record.tools = list(await record.server.list_tools())
        record.status = "connected"
    except Exception as e:
        # 剥开异常组取最里层报错
        while getattr(e, "exceptions", None):
            e = e.exceptions[0]
        record.status = "failed"
        record.error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


async def shutdown() -> None:
    """
    退出时断开所有 server；子进程已死导致的报错直接忽略。
    """
    try:
        await _stack.aclose()
    except Exception:
        pass


def active_toolsets() -> list[PrefixedToolset]:
    return [record.prefixed for record in RECORDS if record.status == "connected"]
