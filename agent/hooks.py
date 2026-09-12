"""
挂在 Agent 上的 hooks，用来抓每次 model API 调用的元数据。

主循环在每轮 agent.iter() 之前清空 api_call_log，跑完后快照到 SessionState 里，
/api-detail 命令再把这一轮的所有调用展示给用户。
"""
from dataclasses import dataclass,field
from typing import Any
from UI.render import console, print_step
import asyncio
from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import ModelHTTPError, ModelAPIError, ModelRetry
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
import permissions
import classifier
from .reminders import (
    build_reminder_text,
    build_task_reminder_text,
    build_job_reminder_text,
    build_verify_reminder_text,
)
from .iteration import MAX_INTERVENTIONS, IterationState
import dataclasses
@dataclass
class ApiCall:
    """
    一次 model API 调用的元数据。before_model_request 创建并填充上半部分，
    after_model_request 填充下半部分。
    """
    # request
    #模型名称
    model: str
    #消息列表长度
    messages_count:int
    # 这次发送给模型messages 后最后一个消息的最后一个part
    last_part:Any
    # tool 列表
    tools:list
    #response 侧（after hook 填充）
    # 结束的条件，tool_calls 调用或者 stop
    finish_response: str =""
    #parts 的类型是text还是toolspart
    parts_kinds: list = field(default_factory=list)
    # 本次输入input_token 消耗
    input_tokens:int =0
    # 本次输出token 消耗
    output_tokens:int =0

# 主循环在每轮 run_sync 之前清空
api_call_log: list[ApiCall] = []

hooks = Hooks()

@hooks.on.before_model_request
async def _record_request(ctx,request_context):
    """
    每次发起 model 调用之前，创建一条 ApiCall 记录。
    """
    #是一个list 将这个msg list 取出来
    msgs = list(request_context.messages)
    last_part = msgs[-1].parts[-1] if msgs and msgs[-1].parts else None
    try:
        tool_names = [t.name for t in request_context.model_request_parameters.function_tools]
    except AttributeError:
        tool_names = []
    api_call_log.append(ApiCall(
        #记录模型的名字
        model=request_context.model.model_name,
        #记录消息的长度
        messages_count=len(msgs),
        # 记录最好一个parts
        last_part=last_part,
        #记录了tool_list
        tools=tool_names,
    ))
    return request_context

# 记录请求后的参数
@hooks.on.after_model_request
async def _record_response(ctx,request_context,response):
    """
    每次 model 调用返回后，填充上面这条 ApiCall 的 response 字段。
    """
    #调用后直接补充后的字段
    if api_call_log:
        call = api_call_log[-1]
        call.finish_response = str(response.finish_reason) if response.finish_reason else "unknown"
        call.parts_kinds =[p.part_kind for p in response.parts]
        call.input_tokens = response.usage.input_tokens
        call.output_tokens = response.usage.output_tokens
    return response



MAX_RETRIES = 3

@hooks.on.model_request
async def _retry_on_error(ctx, *, request_context, handler):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await handler(request_context)
        except ModelHTTPError as e:
            # http 错误，根据错误码进行判断

            if e.status_code < 500:
                # 4xx 的错误，不重试直接报错
                raise
            if attempt >= MAX_RETRIES:
                console.print(f"[bold red]✗ HTTP {e.status_code}，重试 {MAX_RETRIES} 次后仍失败[/]")
                raise

            # 指数退避算法
            wait = 2 ** attempt
            console.print(
                f"[bold yellow]⟳ HTTP {e.status_code}，{wait}s 后重试 "
                f"({attempt + 1}/{MAX_RETRIES})...[/]"
            )
            await asyncio.sleep(wait)

        except ModelAPIError:
            # dns 等网络不通的错误，自动重试
            if attempt >= MAX_RETRIES:
                console.print(f"[bold red]✗ 网络连接失败，重试 {MAX_RETRIES} 次后仍无法连接[/]")
                raise

            # 指数退避算法
            wait = 2 ** attempt
            console.print(
                f"[bold yellow]⟳ 网络连接失败，{wait}s 后重试 "
                f"({attempt + 1}/{MAX_RETRIES})...[/]"
            )
            await asyncio.sleep(wait)


@hooks.on.tool_execute_error
async def _handle_tool_error(ctx, *, call, tool_def, args, error):
    console.print(f"[bold red]✗ 工具 {call.tool_name} 出错：{error}[/]")
    return f"工具执行出错：{type(error).__name__}: {error}"


@hooks.on.tool_execute
async def _check_permission(ctx, *, call, tool_def, args, handler):
    """
    工具执行前的权限关卡。allow 就调用 handler 真正执行；
    ask 就弹审批列表；deny 则把拒绝原因当作工具结果回填，让模型自行纠正。
    auto 模式下，ask 不直接弹窗，先交给 LLM classifier 判定。
    """
    decision = permissions.compute_decision(call.tool_name, args)
    if decision == "allow":
        # 放行，handler(args) 才是真正执行工具的那一步
        return await handler(args)

    # decision == "ask" 且当前是 auto 模式：让 classifier 替用户做决定
    if permissions.state.mode == permissions.AUTO:
        # 审查过程对齐成和 thinking、tool_call 一样的「图标 + 标签独占一行、内容换行」格式，用蓝色让用户一眼看到 classifier 在工作
        print_step("[blue]◆ auto_check[/]", f"[blue dim]正在请 LLM 审查 {call.tool_name}...[/]")
        verdict = await classifier.classify(ctx.messages, call.tool_name, args)

        if verdict.get("error"):
            # classifier 自己出错，回退到下面的人工审批弹窗，不能因为审查失败就放行
            print_step("[yellow]⚠ auto_check[/]", f"[yellow]{verdict['reason']}[/]")
        elif not verdict["should_block"]:
            # classifier 判定安全，放行执行，并把理由打出来让用户随时能审计；标签用 ✔ 表示放行
            print_step("[blue]✔ auto_check[/]", f"[blue dim]放行：{verdict['reason']}[/]")
            return await handler(args)
        else:
            # classifier 判定危险：和人工拒绝一样回填给模型，多带上一句拦截理由；标签用 ✘ 表示拦截
            print_step("[red]✘ auto_check[/]", f"[red]拦截：{verdict['reason']}[/]")
            return (
                f"安全检查拦截了这次 {call.tool_name} 调用，没有执行。拦截理由：{verdict['reason']}。"
                "不要尝试绕过拦截，请停下来向用户说明情况，由用户决定接下来怎么做。"
            )

    # default / acceptEdits 模式的 ask，或 auto 模式下 classifier 出错的回退：弹审批让用户决定
    choice = await permissions.prompt_approval(call.tool_name, args)
    if choice == "once":
        return await handler(args)
    if choice == "always":
        # 记进会话白名单，本会话内这个工具不再询问
        permissions.state.session_allowed.add(call.tool_name)
        return await handler(args)

    # 拒绝：不执行工具，把拒绝原因回填给模型，让它停下来等用户发话，而不是自作主张绕过去
    return f"用户拒绝了对 {call.tool_name} 的调用，这次调用没有执行。请停下手上的事，等用户告诉你接下来该怎么做。"


# 添加是否进行模型检查
# ---------- system-reminder 注入 ----------

# 4 个 task 管理工具名：扫描历史时只要看到其中任意一个，就认为模型最近「碰过」task 系统，不需要再提醒
_TASK_MANAGEMENT_TOOLS = {"task_create", "task_list", "task_get", "task_update"}
# 沉默够这么多轮还没碰 task 工具，才允许发一次 task reminder，避免刚建完 task 就被反复提醒
TASK_REMINDER_TURNS_SINCE_WRITE = 3
# 两次 task reminder 之间至少隔这么多轮，防止 reminder 自己刷屏
TASK_REMINDER_TURNS_BETWEEN = 5


def _scan_task_turn_counters(messages) -> tuple[int, int]:
    """
    一次反向扫描历史，同时算出 (距上次 task 管理工具多少轮, 距上次 task_reminder 多少轮)。两个结果都拿到就早退，避免长 history 下扫两遍。
    """
    since_mgmt = 0
    since_reminder = 0
    found_mgmt = False
    found_reminder = False
    for msg in reversed(messages):
        if isinstance(msg, ModelResponse):
            if not found_mgmt:
                for part in msg.parts:
                    if isinstance(part, ToolCallPart) and part.tool_name in _TASK_MANAGEMENT_TOOLS:
                        found_mgmt = True
                        break
                if not found_mgmt:
                    since_mgmt += 1
            if not found_reminder:
                since_reminder += 1
        elif isinstance(msg, ModelRequest) and not found_reminder:
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str) and _REMINDER_SENTINELS["task"] in part.content:
                    found_reminder = True
                    break
        if found_mgmt and found_reminder:
            break
    return since_mgmt, since_reminder


def _build_file_reminder(ctx, messages) -> str | None:
    # 单次扫描的 readFileState 变更检测；只看 state，不看 messages
    return build_reminder_text(ctx.deps.read_file_state)


def _build_task_reminder(ctx, messages) -> str | None:
    # task reminder 的两道阈值都过了才发：沉默够久 + 上次提醒也够久了
    since_mgmt, since_reminder = _scan_task_turn_counters(messages)
    if since_mgmt < TASK_REMINDER_TURNS_SINCE_WRITE:
        return None
    if since_reminder < TASK_REMINDER_TURNS_BETWEEN:
        return None
    return build_task_reminder_text(ctx.deps.tasks_store)


def _build_job_reminder(ctx, messages) -> str | None:
    # 后台 job 完成通知：从注册表取已结束但还没通知的 job
    if ctx.deps.job_registry is None:
        return None
    return build_job_reminder_text(ctx.deps.job_registry)


# 注册要在 before_model_request 触发的 reminder builder：每条 (sentinel, builder)，sentinel 仅用于回扫识别（task reminder 复用）
_REMINDER_SENTINELS = {
    # task reminder 的识别串就是它正文里 builder 必定带的那句话，不再单独嵌一个 marker
    "task": "task 工具最近没有被使用",
}
_REMINDER_BUILDERS = (_build_file_reminder, _build_task_reminder, _build_job_reminder)


def _has_tool_call(response) -> bool:
    # 「模型是不是准备收尾」的判据：回复里没有任何工具调用。
    # pydantic-ai 的 CallToolsNode._handle_final_result 正是拿这个当 run 的终点，
    # 所以这里拦住的时机与它结束的时机严格对齐——不会误伤正常的工具轮次。
    return any(part.part_kind == "tool-call" for part in response.parts)


@hooks.on.after_model_request
async def _enforce_verification(ctx, *, request_context, response):
    """
    编辑—验证—纠错闭环的闸门：模型想收尾但本轮改过文件却没拿到通过的验证时，
    拒收这次回复并抛出 ModelRetry，把验证要求作为重试提示灌回去。

    为什么必须放在这一层：End 由模型单方面决定（_handle_final_result），
    在「最后一次模型请求」之后就再没有下一次请求可供注入——before_model_request
    那类注入在这种收尾回合里最多只能生效一次。after_model_request 是唯一能在
    收尾动作发生前把它拦下来的位置（ModelRetry 会触发新一次模型请求）。

    ModelRetry 的预算由 core.py 的 Agent(retries=...) 控制；本函数自己再设一道
    MAX_INTERVENTIONS 上限，确保一定会先于预算耗尽而停手，不会把整个 run 打挂。
    """
    iteration: IterationState | None = getattr(ctx.deps, "iteration", None)
    if iteration is None or not iteration.needs_verification() or iteration.budget_exhausted():
        return response
    if _has_tool_call(response):
        # 还在干活，不必打扰；等它真的想结束时再拦
        return response

    # 先记账再拼正文：正文里的「已连续介入 N 次」要包含这一次，
    # 而且最后一次拦停必须自己带上封顶口径——它之后模型就被放行、不再回话了
    iteration.interventions += 1
    capped = iteration.interventions >= MAX_INTERVENTIONS
    text = build_verify_reminder_text(iteration, capped=capped)
    if not text:
        return response

    lead = f"第 {iteration.interventions}/{MAX_INTERVENTIONS} 次"
    print_step(
        "[blue]◈ verify[/]",
        f"[blue dim]本轮改过文件但未通过验证，拦停并要求{'交代' if capped else '补验证'}（{lead}）[/]",
    )
    raise ModelRetry(text)


@hooks.on.before_model_request
async def _inject_reminders(ctx, request_context):
    # 逐个 builder 跑一遍，收集返回的非 None 文本，拼成一条 system-reminder 注入到 messages 末尾
    # 每个 builder 自己负责阈值判断和从 ctx.deps 取对应状态（file reminder 看 read_file_state，task reminder 看 tasks_store）
    texts = []
    for builder in _REMINDER_BUILDERS:
        text = builder(ctx, request_context.messages)
        if text:
            texts.append(text)
    if not texts:
        return request_context

    combined = "\n\n".join(texts)
    reminder = ModelRequest(parts=[UserPromptPart(content=combined)])
    new_messages = list(request_context.messages) + [reminder]
    print_step("[dim]◇ system[/]", f"[dim]{combined[:200]}[/]")
    return dataclasses.replace(request_context, messages=new_messages)