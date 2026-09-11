from dataclasses import dataclass, field
from typing import Callable, Optional
import compact
import os
import re
import session
import mcp_servers
from memory import background as memory_background, store as memory_store
from rich.markdown import Heading, Markdown
from rich.markup import escape
from rich.padding import Padding
from rich.rule import Rule
import questionary
from agent.file_state import ReadFileState
from tasks_store import TasksStore
from file_history import FileHistory
from background_jobs import JobRegistry
from .render import console, print_step


class LeftAlignedHeading(Heading):
    """
    rich 默认把 Markdown 标题渲染成居中对齐，宽终端里看着像错位，覆盖成左对齐。
    """
    def __rich_console__(self,console,options):
        text =self.text
        text.justify = "left"
        yield text

# 全局替代Markdown 的标题渲染元素
Markdown.elements["heading_open"] = LeftAlignedHeading

@dataclass
class SessionState:
    """
    跨命令共享的会话状态，主循环把它传给每个命令处理函数。
    """

    history:list = field(default_factory=list)
    input_tokens :int = 0
    output_tokens:int =0
    model_name:str = ""
    # 当前会话 ID，用于把消息追加到对应的 jsonl 文件
    session_id: str = ""
    # 最近一轮 user input 触发的所有 model API 调用记录
    last_api_calls: list = field(default_factory=list)
    # 自动压缩连续失败的次数，达到上限后本会话不再重试
    compact_failures: int = 0
    # /rewind 回退对话后回填输入框的原 prompt，空串表示没有待回填
    pending_input: str = ""
    # 本会话的文件读取状态：read_file/edit_file/write_file 共享，/new 时换新实例
    read_file_state: ReadFileState = field(default_factory=ReadFileState)
    # 本会话的 task 存储：按 session_id 隔离落盘，/new /resume 时换新实例
    tasks_store: TasksStore = field(default=None)
    # 本会话的文件检查点存储：按 session_id 隔离落盘，/new /resume 时换新实例
    file_history: FileHistory | None = field(default=None)
    # 本会话已召回注入过的记忆文件名，避免同一记忆每轮重复注入；/new 清空、/resume 从历史回填
    surfaced_memories: set = field(default_factory=set)
    # 本会话的后台任务注册表：按 session_id 隔离日志目录，/new /resume 时换新实例
    job_registry: JobRegistry = field(default=None)

    def __post_init__(self):
        # tasks_store / file_history / job_registry 都依赖 session_id，没法用 default_factory（拿不到其他字段），在 __post_init__ 里按 session_id 建
        # field(default=None) 只是占位，到这里才真正赋值；/new /resume 改 session_id 后也会重建
        self.tasks_store = TasksStore(self.session_id)
        self.file_history = FileHistory(self.session_id)
        self.job_registry = JobRegistry(self.session_id)


@dataclass
class Command:
    name: str
    description: str
    # handler 返回 False 表示主循环应当退出
    handler: Callable[..., bool]
    # 是否接收命令名后面的参数串（如 /compact 的补充指令）
    takes_args: bool = False



def print_divider() -> None:
    """
    每轮交互之前打印一条分割线，区分输入区域。Rule 会自适应终端宽度。
    """
    console.print(Rule(style="grey50"))


# 这里是解决输出文字太长的问题
def _truncate(text, limit: int = 120) -> str:
    """
    截断并 escape，用于 tool 参数 / 返回值 / 用户输入这类可能过长的内容。
    """
    text = str(text).strip()
    text = text if len(text) <= limit else text[:limit] + "..."
    return escape(text)


def _full(text) -> str:
    """
    完整显示，只做 escape 不截断，用于 thinking 和 assistant text 这种用户关心的内容。
    """
    return escape(str(text).strip())


def _format_part_line(part) -> Optional[str]:
    """
    把一条消息里的单个 part 格式化为带 Rich markup 的字符串。
    版式：图标 + role 标签独占一行，内容换行到下一行，不用「|」分隔。
    """
    # 内容行统一缩进 2 格，和图标（占 2 格：图标 + 空格）后的 role 名对齐
    kind = part.part_kind
    if kind == "user-prompt":
        return f"[cyan]❯ user[/]\n  {_truncate(part.content)}"
    if kind == "thinking":
        # thinking 整块 dim，弱化视觉权重；不截断，完整保留思考过程
        return f"[dim]✻ thinking[/]\n  [dim]{_full(part.content)}[/]"
    if kind == "text":
        content = (part.content or "").strip()
        if not content:
            return None
        # assistant 是用户最关心的最终回答，完整显示
        return f"[green]● assistant[/]\n  {_full(content)}"
    if kind == "tool-call":
        return f"[yellow]⏺ tool_call[/]\n  [yellow dim]{part.tool_name}({_truncate(part.args)})[/]"
    if kind == "tool-return":
        return f"[magenta]✔ tool_return[/]\n  [magenta dim]{part.tool_name} -> {_truncate(part.content)}[/]"
    if kind == "retry-prompt":
        # 工具抛 ModelRetry 后，SDK 生成 retry-prompt 把错误反馈给模型
        return f"[yellow]✘ tool_retry[/]\n  [yellow dim]{part.tool_name} -> {_truncate(part.content)}[/]"
    return None


def print_assistant_markdown(content: str) -> None:
    """
    模型的回复天然是 Markdown 格式，整块渲染出来，而不是打印原始文本。
    """
    console.print("[green]● assistant[/]")
    # Markdown 是块级渲染对象，没法跟在行内前缀后面，所以另起一行渲染；左缩进 2 格和 role 名对齐
    console.print(Padding(Markdown(content), (0, 0, 0, 2)))


def print_part(part) -> None:
    """
    渲染单个消息 part：assistant 文本走 Markdown 块渲染，其余 part 是单行文本。
    """
    if part.part_kind == "text":
        content = (part.content or "").strip()
        if content:
            print_assistant_markdown(content)
            # 每个 role block 末尾留一个空行，块与块之间不那么挤
            console.print()
        return
    line = _format_part_line(part)
    if line:
        label, _, body = line.partition("\n")
        # body 形如 "  [markup]…"，去掉字面前导 2 空格，交给 print_step 用 Padding 缩进（折行续行也保持缩进）
        print_step(label, body[2:])


def print_agent_steps(new_messages) -> None:
    """
    主循环里调用：显示这一轮 Agent 新增的中间过程（thinking、文本、工具调用、工具返回）。
    """
    for msg in new_messages:
        for part in msg.parts:
            # 主循环里不重复显示用户刚刚输入的内容
            if part.part_kind == "user-prompt":
                continue
            print_part(part)


def cmd_exit(state: SessionState) -> bool:
    console.print("再见 👋")
    return False


def cmd_help(state: SessionState) -> bool:
    console.print("可用命令：")
    for cmd in COMMANDS.values():
        console.print(f"  /{cmd.name:<10} {cmd.description}")
    console.print()
    return True


def cmd_new(state: SessionState) -> bool:
    """
    开启新会话：清空历史、token 计数、API 调用记录。
    """
    state.history.clear()
    state.input_tokens = 0
    state.output_tokens = 0
    state.last_api_calls.clear()
    state.compact_failures = 0
    # 新会话还没召回过任何记忆，清空避免跳过本该注入的记忆
    state.surfaced_memories.clear()
    # 换一个新的 ReadFileState：新会话没读过任何文件，旧会话的读取状态不该带过来
    state.read_file_state = ReadFileState()
    # 换一个新的会话 ID，后续消息写进新文件
    state.session_id = session.new_session_id()
    # tasks_store / file_history / job_registry 按 session_id 隔离落盘，新会话用全新空 store
    state.tasks_store = TasksStore(state.session_id)
    state.file_history = FileHistory(state.session_id)
    # 旧会话还在跑的后台 job 先终止，避免进程泄露后新会话再也管不到它们
    killed = state.job_registry.shutdown()
    if killed:
        console.print(f"已终止旧会话 {killed} 个仍在运行的后台 job")
    state.job_registry = JobRegistry(state.session_id)
    console.print("已开启新会话\n")
    return True


def cmd_status(state: SessionState) -> bool:
    console.print(f"模型：           {state.model_name}")
    console.print(f"历史消息条数：    {len(state.history)}")
    used = compact.context_tokens(state.history)
    threshold = compact.compact_threshold()
    if used:
        console.print(f"当前上下文占用（估算）：{used:,} / {threshold:,} tokens（{used * 100 // threshold}%）")
    else:
        console.print(f"当前上下文占用（估算）：暂无数据（自动压缩阈值 {threshold:,} tokens）")
    console.print(f"累计输入 tokens：{state.input_tokens}")
    console.print(f"累计输出 tokens：{state.output_tokens}\n")
    return True

def _summary_line(mtime, prompt: str) -> str:
    """
    拼一条会话列表的展示文本：修改时间 + 首条用户输入摘要。
    """
    prompt = " ".join(str(prompt).split())
    if len(prompt) > 50:
        prompt = prompt[:50] + "..."
    return f"{mtime:%m-%d %H:%M}  {prompt}"


def _resurface_memories(history) -> set:
    """
    从恢复的历史里回扫已注入过的记忆文件名，避免 /resume 后重复召回同一批记忆。
    记忆召回时会把记忆文件的完整路径写进 <system-reminder>，这里按记忆目录前缀 + 文件名.md 提取。
    """
    prefix = str(memory_store.memory_dir())
    pattern = re.compile(re.escape(prefix) + r"/([\w.\-]+\.md)")
    surfaced = set()
    for msg in history:
        for part in getattr(msg, "parts", []):
            content = getattr(part, "content", "")
            if not isinstance(content, str):
                continue
            for m in pattern.finditer(content):
                surfaced.add(m.group(1))
    return surfaced
async def cmd_resume(state: SessionState) -> bool:
    """
    列出当前项目的历史会话，选中后恢复对话历史。
    """
    sessions = session.list_sessions()
    if not sessions:
        console.print("(当前项目还没有历史会话)\n")
        return True

    choices = [
        questionary.Choice(title=_summary_line(mtime, prompt), value=sid)
        for sid, mtime, prompt in sessions
    ]
    # 用 ask_async 而不是 ask：ask 内部会 asyncio.run()，跟外层事件循环冲突
    # 包在 in_terminal() 里：Repl 的 Application 还在跑，得先挂起它，让 questionary 自己的 Application 接管终端
    from prompt_toolkit.application import in_terminal
    async with in_terminal():
        selected = await questionary.select(
            "选择要恢复的会话（上下键移动，回车确认）：", choices=choices
        ).ask_async()
    # 用户按 Ctrl+C 取消选择
    if selected is None:
        return True

    # 还原对话历史，并把会话 ID 切换成选中的旧会话，后续消息继续追加到同一个文件
    state.history = session.load_history(selected)
    state.session_id = selected
    # 从恢复的历史里回填已注入过的记忆，避免重复召回同一批记忆
    state.surfaced_memories = _resurface_memories(state.history)
    # tasks_store / file_history 按 session_id 重建：旧会话磁盘上的数据会被 __init__ 灌进内存
    state.tasks_store = TasksStore(state.session_id)
    state.file_history = FileHistory(state.session_id)
    # 后台 job 注册表同样切换：旧会话还在跑的 job 先终止，避免进程泄露
    killed = state.job_registry.shutdown()
    if killed:
        console.print(f"已终止旧会话 {killed} 个仍在运行的后台 job")
    state.job_registry = JobRegistry(state.session_id)

    # jsonl 里每条模型回复都带 usage，把会话的 token 用量累加回来
    state.input_tokens = sum(
        m.usage.input_tokens for m in state.history if m.kind == "response"
    )
    state.output_tokens = sum(
        m.usage.output_tokens for m in state.history if m.kind == "response"
    )
    # 最近一轮的 API 调用记录只在进程内有效，没法恢复，清空
    state.last_api_calls.clear()
    state.compact_failures = 0

    # 把恢复的对话回放到屏幕上
    console.print(f"\n已恢复会话 {selected[:8]}，共 {len(state.history)} 条消息：\n")
    for msg in state.history:
        for part in msg.parts:
            # 回放和实时输出共用同一套 part 渲染逻辑
            print_part(part)
    console.print()
    return True

def cmd_api_detail(state: SessionState) -> bool:
    """
    显示最近一轮 user input 触发的所有 model API 调用元数据。
    """
    if not state.last_api_calls:
        console.print("(还没有任何模型调用记录，先发一条消息再来看)\n")
        return True

    console.print(f"最近一轮共发起 {len(state.last_api_calls)} 次 model API 调用\n")

    for i, call in enumerate(state.last_api_calls, 1):
        console.print(f"[bold]Call #{i}[/]")
        console.print("  Request:")
        console.print(f"    model:        {call.model}")
        console.print(f"    messages:     {call.messages_count} 条")
        if call.last_part is not None:
            preview = _format_part_line(call.last_part)
            if preview:
                console.print(f"    last_message: {preview}")
        console.print(f"    tools:        {', '.join(call.tools)}")
        console.print("  Response:")
        console.print(f"    finish_response: {call.finish_response}")
        console.print(f"    parts:         {', '.join(call.parts_kinds)}")
        console.print(f"    usage:         input={call.input_tokens}, output={call.output_tokens}")
        console.print()
    return True


async def cmd_rewind(state: SessionState) -> bool:
    """
    回退到过去的某个检查点：磁盘文件 + 对话历史一起退回，原输入回填输入框供改改重发。
    """
    fh = state.file_history
    if not fh or not fh.checkpoints:
        console.print("(还没有检查点，先正常聊一轮再 /rewind)\n")
        return True

    # 列检查点供选择：最新的排最前，默认高亮第一项 = 回退最近一轮（最常见）
    choices = []
    for cp in reversed(fh.checkpoints):
        prompt = " ".join(cp.prompt.split())
        if len(prompt) > 40:
            prompt = prompt[:40] + "…"
        changes = fh.turn_stats(cp)
        if changes:
            files = ", ".join(f"{c.action} {os.path.basename(c.path)}" for c in changes)
        else:
            files = "无文件改动"
        choices.append(questionary.Choice(title=f"{prompt}  ·  {files}", value=cp))

    from prompt_toolkit.application import in_terminal
    async with in_terminal():
        selected = await questionary.select(
            "选择要回退到的检查点（上下键移动，回车确认）：", choices=choices
        ).ask_async()
    # 用户按 Ctrl+C 取消选择
    if selected is None:
        return True

    cp = selected
    # 回退执行计划：从当前磁盘状态回到该检查点的累计差异
    plan = fh.diff_stats(cp)
    if plan:
        console.print("\n将回退，以下文件会被改动：")
        for c in plan:
            console.print(f"  {c.action:<7} {c.path}  [green]+{c.insertions}[/]/[red]-{c.deletions}[/]")
    else:
        console.print("\n此检查点不改动文件（仅回退对话历史）")
    async with in_terminal():
        ok = await questionary.confirm("确认回退？", default=True).ask_async()
    if not ok:
        console.print("已取消\n")
        return True

    # 1. 恢复磁盘文件到检查点时刻
    fh.rewind_files(cp)
    # 2. 受影响文件的读取登记失效：rewind_files 用 copy2 带回旧 mtime，不清的话 edit_file 可能基于陈旧内容编辑
    for c in plan:
        state.read_file_state.invalidate(c.path)
    # 3. 截断对话历史到检查点时刻并落盘，/resume 也能读到截断后的历史
    state.history = state.history[: cp.history_index]
    session.rewrite_messages(state.session_id, state.history)
    # 4. 原输入回填输入框，_process 的 finally 会把它塞回输入框供用户改改重发
    state.pending_input = cp.prompt
    # 5. 丢弃该检查点及之后的检查点（它们指向已不存在的消息）
    fh.drop_from(cp)

    prompt = " ".join(cp.prompt.split())
    if len(prompt) > 40:
        prompt = prompt[:40] + "…"
    console.print(f"\n已回退到「{prompt}」之前的检查点，原输入已回填输入框\n")
    return True


async def cmd_compact(state: SessionState, args: str = "") -> bool:
    """
    手动压缩上下文，可以带补充指令，如 /compact 重点保留文件改动。
    """
    try:
        await compact.run_compact(state, custom_instructions=args)
    except Exception as e:
        console.print(f"[red]压缩失败：{type(e).__name__}: {e}[/]\n")
    return True


async def cmd_dream(state: SessionState) -> bool:
    """
    手动触发记忆合并整理（dream）：把分散的记忆合并、过期记忆清理、索引修剪，不等自动闸门。
    """
    console.print("[cyan]✦ memory[/]  [dim]开始整理记忆，请稍候...[/]")
    try:
        await memory_background.dream(state.history, force=True, session_id=state.session_id)
    except Exception as e:
        console.print(f"[red]记忆整理失败：{type(e).__name__}: {e}[/]\n")
    return True


def cmd_mcp(state: SessionState) -> bool:
    """
    显示所有已配置 MCP server 的连接状态和工具清单。
    """
    if not mcp_servers.RECORDS:
        console.print(f"未配置任何 MCP server。可在项目根目录的 .mcp.json 或 {mcp_servers.USER_CONFIG} 中添加。\n")
        return True

    # 顶部一行汇总：server 总数 / 已连接 / 失败
    connected = sum(1 for r in mcp_servers.RECORDS if r.status == "connected")
    failed = sum(1 for r in mcp_servers.RECORDS if r.status == "failed")
    head = f"共 {len(mcp_servers.RECORDS)} 个 server"
    if connected:
        head += f"，{connected} 已连接"
    if failed:
        head += f"，{failed} 失败"
    console.print(f"[bold]{head}[/]\n")

    for record in mcp_servers.RECORDS:
        console.print(f"[bold]{record.server.id}[/]  [dim]{escape(record.transport)}[/]")
        if record.status == "connected":
            console.print(f"  [green]已连接[/]，{len(record.tools)} 个工具")
            for tool in record.tools:
                name = escape(tool.name)
                # 描述折叠成一行并截断，避免多行 description 撑乱排版
                desc = " ".join((tool.description or "").split())
                if desc:
                    console.print(f"    - [cyan]{name}[/]  [dim]{_truncate(desc, 80)}[/]")
                else:
                    console.print(f"    - [cyan]{name}[/]")
        else:
            console.print(f"  [red]连接失败[/]：{escape(record.error)}")
        console.print()
    return True


def cmd_jobs(state: SessionState) -> bool:
    """
    列出本会话的所有 job：id、类型、状态、描述和日志路径（后台命令、subagent 共用一张表）。
    """
    jobs = state.job_registry.list()
    if not jobs:
        console.print("(本会话还没有任何 job)\n")
        return True

    for job in jobs:
        icon = _JOB_STATUS_ICONS.get(job.status, "?")
        desc = " ".join(job.description.split())
        if len(desc) > 50:
            desc = desc[:50] + "..."
        console.print(f"{icon} [cyan]{job.id}[/] [dim]{job.kind}[/]  {escape(desc)}")
        console.print(f"   [dim]日志：{job.log_path}[/]")
    console.print()
    return True


# job 状态对应的显示图标，/jobs 面板用
_JOB_STATUS_ICONS = {
    "running": "[yellow]▶[/]",
    "completed": "[green]✔[/]",
    "failed": "[red]✘[/]",
    "killed": "[red]⊘[/]",
}


COMMANDS = {
    "new": Command("new", "开启新会话", cmd_new),
    "status": Command("status", "显示当前会话状态", cmd_status),
    "mcp": Command("mcp", "查看 MCP server 状态和工具", cmd_mcp),
    "jobs": Command("jobs", "列出后台 job（命令/subagent）", cmd_jobs),
    "api-detail": Command("api-detail", "显示最近一轮 model API 调用详情", cmd_api_detail),
    "rewind": Command("rewind", "回退到过去的检查点", cmd_rewind),
    "compact": Command("compact", "压缩上下文（可带补充指令）", cmd_compact, takes_args=True),
    "dream": Command("dream", "整理合并长期记忆", cmd_dream),
    "help": Command("help", "显示可用命令", cmd_help),
    "exit": Command("exit", "退出程序", cmd_exit),
    "resume": Command("resume", "恢复历史会话", cmd_resume)
}