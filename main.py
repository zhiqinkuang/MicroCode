import asyncio

from pydantic_ai import Agent
from pydantic_graph import End
import session
from agent import agent, MODEL_NAME, api_call_log
from agent.deps import AgentDeps
from UI.input_ui import Repl
from UI.commands import (
    COMMANDS,
    SessionState,
    console,
    print_part,
    print_welcome_banner,
)
from mentions import build_mention_messages, extract_at_mentions

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
    parts = user_input[1:].split()
    cmd_name = parts[0] if parts else ""
    command = COMMANDS.get(cmd_name)
    if command is None:
        # 以 / 开头但不是已知命令：当成普通输入交给 Agent，避免误判路径/代码片段
        return "pass"
    # 用 asyncio.iscoroutine 兼容 async / 同步两种 handler
    result = command.handler(state)
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

    # deps 把 read_file_state 和 tasks_store 打包成 AgentDeps 注入：file 工具取 .read_file_state，task 工具取 .tasks_store，hooks 也从同一个 deps 读两边状态
    deps = AgentDeps(read_file_state=state.read_file_state, tasks_store=state.tasks_store)
    async with agent.iter(user_input, message_history=state.history, deps=deps) as run:
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

async def main():
    state = SessionState(model_name=MODEL_NAME, session_id=session.new_session_id())
    print_welcome_banner("Coding Agent")

    repl = Repl(state)

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

        # 普通输入：先把 @ 引用解析进历史，再交给 Agent，开启 working 指示器
        repl.start_working()
        try:
            inject_at_mentions(text, state)
            await run_agent_loop(text, state)
        except asyncio.CancelledError:
            # 用户按 ESC / Ctrl+C 打断：交给 _process 的 except 统一打印中断信息
            raise
        except Exception as e:
            console.print(f"\n[bold red]✗ {type(e).__name__}: {e}[/]\n")

    await repl.run(on_submit)


if __name__ == "__main__":
    asyncio.run(main())
