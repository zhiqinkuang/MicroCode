import asyncio
import logging
import compact
import permissions
from pydantic_ai import Agent
from pydantic_graph import End
from prompt_toolkit.application.current import set_app
import session
import mcp_servers
# 长期记忆三件套：store 建目录、recall 每轮前召回、background 每轮后提炼 + 退出收尾
from memory import store as memory_store, recall as memory_recall, background as memory_background
from agent import agent, MODEL_NAME, api_call_log
from agent.deps import AgentDeps
from UI.input_ui import Repl
from UI.render import print_welcome_banner
from UI.commands import (
    COMMANDS,
    SessionState,
    console,
    print_part,
)
from mentions import build_mention_messages, extract_at_mentions
from agent.reminders import build_job_reminder_text
import subagents

logger = logging.getLogger(__name__)

# 识别用户输入的指令
async def handle_command(user_input, state):
    """
    处理以 / 开头的输入。
    返回 'pass'：不是已知命令，主循环继续交给 Agent 当普通输入处理；
    返回 'continue'：命令已处理，主循环跳到下一轮；
    返回 'break'：命令要求退出主循环。
    """
    if not user_input.startswith("/"):
        return "pass"
    # 第一个 token 是命令名；空输入（只有 "/"）或拼错的命令都不当命令处理
    # maxsplit=1 保留命令名后面的整段参数（如 /compact 的补充指令），不拆散
    parts = user_input[1:].split(maxsplit=1)
    cmd_name = parts[0] if parts else ""
    command = COMMANDS.get(cmd_name)
    if command is None:
        # 以 / 开头但不是已知命令：当成普通输入交给 Agent，避免误判路径/代码片段
        return "pass"
    # takes_args 的命令（如 /compact）接收命令名后面的参数串
    args = parts[1] if len(parts) > 1 else ""
    result = command.handler(state, args) if command.takes_args else command.handler(state)
    # 用 asyncio.iscoroutine 兼容 async / 同步两种 handler
    if asyncio.iscoroutine(result):
        result = await result
    return "continue" if result else "break"

#异步启动agent loop
async def run_agent_loop(user_input, state):
    """
    用 agent.iter() 自己驱动 Agent 图，每跑完一个节点就把新增的 part 实时打出来，
    跑完后再把结果（history、token、API 调用元数据）同步到 state。
    """
    api_call_log.clear()

    # deps 把 read_file_state / tasks_store / file_history / job_registry 打包成 AgentDeps 注入：file 工具取 .read_file_state 和 .file_history（写盘前 track_edit 留检查点），task 工具取 .tasks_store，shell 工具取 .job_registry，hooks 也从同一个 deps 读状态
    deps = AgentDeps(
        read_file_state=state.read_file_state,
        tasks_store=state.tasks_store,
        file_history=state.file_history,
        job_registry=state.job_registry,
    )
    async with agent.iter(user_input, message_history=state.history, deps=deps,toolsets=mcp_servers.active_toolsets(),) as run:
        node = run.next_node

        while not isinstance(node, End):
            node = await run.next(node)

            if Agent.is_call_tools_node(node):
                # 模型刚回复完，从 model_response.parts 里把 thinking / text / tool-call 打出来
                for part in node.model_response.parts:
                    print_part(part)

            elif Agent.is_model_request_node(node):
                # 工具刚执行完，从 request.parts 里找 tool-return 打出来
                for part in node.request.parts:
                    if part.part_kind == "tool-return":
                        print_part(part)

    # 跑完一轮，同步结果到 state（中间过程已在循环里实时打印，这里不再补打）
    result = run.result
    new_messages = result.new_messages()
    state.history = result.all_messages()
    usage = result.usage
    state.input_tokens += usage.input_tokens
    state.output_tokens += usage.output_tokens
    state.last_api_calls = list(api_call_log)

    # 把本轮新增消息追加到会话文件，/resume 才能读到
    if state.session_id:
        session.append_messages(state.session_id, new_messages)
    # 每轮结束后后台提炼记忆：主对话这轮已自己写过记忆就跳过，否则 fork 对话提炼值得保存的
    memory_background.schedule(state, new_messages)

def inject_at_mentions(user_input, state):
    paths = extract_at_mentions(user_input)
    if not paths:
        return
    mention_messages = build_mention_messages(paths, state.read_file_state)
    if not mention_messages:
        return
    # 塞进历史：模型下一轮就能看到这些「读文件」记录
    state.history += mention_messages
    # 持久化，/resume 恢复会话时能连同引用的文件一起还原
    session.append_messages(state.session_id, mention_messages)
    # 终端回显注入了哪些文件，让你看到 @ 确实生效
    for msg in mention_messages:
        for part in msg.parts:
            print_part(part)

async def watch_jobs(repl, state):
    """
    每秒扫一次注册表：程序空闲且攒着未通知的完成 job 时，
    把通知文本作为系统输入提交，激活 Agent 循环（和用户手动发一条消息的效果类似）。
    """
    while True:
        await asyncio.sleep(1)
        if not repl.is_idle:
            continue
        text = build_job_reminder_text(state.job_registry)
        if text:
            repl.submit_system(text)

async def watch_approvals(repl):
    """
    盯 sub agent 冒泡上来的审批队列：用户空闲（主 agent 这轮干完、输入框等输入）时才弹窗，
    不按 sub agent 的随机节奏打断用户手头的事。
    """
    while True:
        await asyncio.sleep(0.5)
        if not repl.is_idle:
            continue
        req = subagents.pop_pending_approval()
        if req is None:
            continue
        repl.approval_active = True
        try:
            # watcher 在 REPL 启动前创建，没有继承 Application 的上下文。
            # 显式绑定后 in_terminal() 才会暂停真实输入框并交出终端。
            with set_app(repl.app):
                choice = await permissions.prompt_approval(
                    req.tool_name, req.args,
                    requester=f"sub agent「{req.job.description}」（job {req.job.id}）请求：",
                )
            if not req.future.done():
                req.future.set_result(choice)
        finally:
            repl.approval_active = False
            if not req.future.done():
                req.future.cancel()

async def main():
    from rich.logging import RichHandler

    logging.basicConfig(
        level=logging.INFO, format="%(message)s",
        handlers=[RichHandler(console=console, show_time=False, show_path=False)],
    )
    sid = session.new_session_id()
    # file_history 在 SessionState.__post_init__ 里按 session_id 建好，第一轮用户输入就会 make_checkpoint，/rewind 一开始就有得回退
    state = SessionState(
        model_name=MODEL_NAME,
        session_id=sid,
    )
    print_welcome_banner("Coding Agent")

    # 建好记忆目录：system prompt 告诉模型「目录已存在，不要 mkdir」，这里必须先建
    memory_store.ensure_memory_dir()

    # 启动时并发连接 .mcp.json 配置的 MCP server；/mcp 展示与 active_toolsets() 注入 Agent 都依赖 RECORDS
    summary = await mcp_servers.startup()
    if summary:
        console.print(summary)

    # 加载 .my-claude-code/agents/ 下的自定义 sub agent，和内置类型一起进动态类型清单
    custom = subagents.load_custom_agents()
    if custom:
        logger.info("已加载 %s 个自定义 sub agent（/agents 查看）", custom)

    repl = Repl(state)

    # 常驻协程盯后台 job：Agent 闲着等输入时，完成的 job 也能推通知激活下一轮
    jobs_watcher = asyncio.ensure_future(watch_jobs(repl, state))
    # 常驻协程盯 sub agent 冒泡上来的审批：用户空闲时才弹窗
    approvals_watcher = asyncio.ensure_future(watch_approvals(repl))

    async def on_submit(text):
        # / 开头：先尝试当作命令解析；未命中的命令原样当作普通输入交给 Agent
        if text.startswith("/"):
            action = await handle_command(text, state)
            if action == "break":
                repl.exit()
                return
            if action == "continue":
                return
            # action == "pass"：以 / 开头但不是已知命令，继续走 Agent 分支
        # 发请求前检查上下文水位，越过阈值就先自动压缩再继续
        await compact.auto_compact_if_needed(state)
        # 普通输入：在历史变动之前给被跟踪文件记一个检查点，/rewind 才有得回退。
        # history_index 取此刻历史长度，回退对话就是截断到这个下标；prompt 存原输入，回退后回填输入框
        if state.file_history:
            state.file_history.make_checkpoint(len(state.history), text)
        # 先把 @ 引用解析进历史，再交给 Agent，开启 working 指示器
        repl.start_working()
        try:
            inject_at_mentions(text, state)
            # 召回相关长期记忆塞进历史后再交给 Agent（和 @ 引用一样持久化，/resume 可还原）
            await memory_recall.inject_memories(text, state)
            await run_agent_loop(text, state)
        except asyncio.CancelledError:
            # 用户按 ESC / Ctrl+C 打断：交给 _process 的 except 统一打印中断信息
            raise
        except Exception as e:
            console.print(f"\n[bold red]✗ {type(e).__name__}: {e}[/]\n")

    try:
        await repl.run(on_submit)
    finally:
        # 停掉空闲轮询，等在途的后台记忆任务收尾（可能正在写记忆文件），
        # 终止本会话还在跑的后台 job（避免进程泄露），最后断开 MCP 连接
        jobs_watcher.cancel()
        approvals_watcher.cancel()
        await asyncio.gather(jobs_watcher, approvals_watcher, return_exceptions=True)
        killed = await state.job_registry.aclose()
        await memory_background.drain()
        if killed:
            console.print(f"已终止 {killed} 个仍在运行的后台 job")
        await mcp_servers.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
