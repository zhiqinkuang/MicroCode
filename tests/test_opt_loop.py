"""
编辑—验证—纠错闭环的离线回归：全部用 FunctionModel 替身，不发真实模型请求。

闭环的闸门在 agent/hooks.py 的 _enforce_verification（after_model_request）：
模型想收尾（回复里没有工具调用）但本轮改过文件却没拿到通过的验证时，
拒收这次回复并抛 ModelRetry，把验证要求作为重试提示灌回去，驱动模型继续修。

覆盖四件事：
1. 纯文本轮（没有文件改动）零介入——旧行为逐字节不变；
2. 改了文件却没验证 → 被拦停最多 MAX_INTERVENTIONS 次，然后放行收尾（有界，不打死 run）；
3. 验证失败的输出随注入消息回流，模型下一轮看得到报错原文；
4. 验证通过后不再拦停。
"""
import asyncio
import json
import sys

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import main
import permissions
from agent.iteration import MAX_INTERVENTIONS, IterationState
from agent.reminders import build_verify_reminder_text
from agent.tools.shell import run_command
from UI.commands import SessionState

# 判定模型「想收尾」的那句话，和提醒正文里的措辞保持一致
CUE = "没有取得一次通过的验证"


def call_tool(name, args, call_id):
    return ToolCallPart(tool_name=name, args=json.dumps(args), tool_call_id=call_id)


def final(text="done"):
    return ModelResponse(parts=[TextPart(text)])


def configure_run(monkeypatch):
    # 离线链路：不连 MCP、不跑后台记忆提炼、不渲染终端 part
    monkeypatch.setattr(main.mcp_servers, "active_toolsets", lambda: [])
    monkeypatch.setattr(main.memory_background, "schedule", lambda state, messages: None)
    monkeypatch.setattr(main, "print_part", lambda part: None)
    # bypass 免审批，让工具调用在测试里直接执行
    permissions.state.mode = permissions.BYPASS


def run_turn(state, respond):
    """跑一轮 run_agent_loop，模型调用全部走 FunctionModel 替身。"""
    asyncio.run(main.run_agent_loop("go", state, model=FunctionModel(respond)))


def prompts(messages, kind):
    """按 part 类型取出该轮请求里给模型看的内容。user-prompt 是注入消息，retry-prompt 是拦停提示。"""
    return [
        part.content
        for message in messages
        for part in message.parts
        if part.part_kind == kind
    ]


def test_plain_text_turn_has_no_verification_intervention(monkeypatch):
    """纯对话轮：一次模型调用后正常结束，不产生任何拦停。"""
    configure_run(monkeypatch)
    requests = []

    def respond(messages, info):
        requests.append(list(messages))
        return final("你好")

    state = SessionState(session_id="closure-text")
    try:
        run_turn(state, respond)
        assert state.iteration.edited_paths == []
        assert state.iteration.interventions == 0
        assert len(requests) == 1
        assert prompts(requests[0], "retry-prompt") == []
    finally:
        asyncio.run(state.job_registry.aclose())


def test_edit_without_verification_is_gated_and_bounded(monkeypatch, tmp_path):
    """模型只改文件、从不验证 → 每次想收尾都被拦停，直到 MAX_INTERVENTIONS 后放行。"""
    configure_run(monkeypatch)
    target = tmp_path / "sample.py"
    requests = []

    def respond(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            return ModelResponse(parts=[call_tool("write_file", {"path": str(target), "content": "x = 1\n"}, "c1")])
        return final("已经写好了")

    state = SessionState(session_id="closure-gated")
    try:
        run_turn(state, respond)
        assert str(target) in state.iteration.edited_paths
        # 拦停次数正好封顶：既没漏拦，也没有无限循环
        assert state.iteration.interventions == MAX_INTERVENTIONS
        # 模型确实多跑了几轮：首轮写文件 + 封顶次数的拦停 + 最后放行的那次收尾
        assert len(requests) == 1 + MAX_INTERVENTIONS + 1
        # retry-prompt 是累积的：第 N 次拦停从第 N+2 个请求开始可见。
        # 首次拦停发生在 requests[1]（写文件之后的收尾回合），它当场被拒，所以 requests[1] 里看不到
        assert prompts(requests[1], "retry-prompt") == []
        assert len(prompts(requests[2], "retry-prompt")) == 1
        assert len(prompts(requests[3], "retry-prompt")) == 2
        assert len(prompts(requests[4], "retry-prompt")) == MAX_INTERVENTIONS
        # 第 3 次（即最后一次）拦停用的是封顶口径：要求向用户交代，而不是继续催着修。
        # 必须由这次拦停自己带上——它之后模型就被放行了，没有第二次机会传达
        final_rejection = str(prompts(requests[-1], "retry-prompt")[-1])
        assert "不再继续拦停" in final_rejection
        assert "由用户决定下一步" in final_rejection
        assert "已连续介入 3 次" in final_rejection  # 计数包含这一次拦停
        # 拦停提示里带上了改动文件与下一步要求
        retry_text = " ".join(str(p) for p in prompts(requests[-1], "retry-prompt"))
        assert CUE in retry_text
        assert str(target) in retry_text
        # 有界放行：run 正常结束，拿到了模型的最终回复而不是异常
        assert state.history[-1].parts[-1].content == "已经写好了"
    finally:
        asyncio.run(state.job_registry.aclose())


def test_gate_does_not_fire_while_model_still_calls_tools(monkeypatch, tmp_path):
    """模型还在调工具（正常干活）时不该被拦停——拦停只发生在收尾回合。"""
    configure_run(monkeypatch)
    target = tmp_path / "busy.py"
    requests = []

    def respond(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            return ModelResponse(parts=[call_tool("write_file", {"path": str(target), "content": "z\n"}, "c1")])
        if len(requests) == 2:
            # 这一轮仍然有工具调用：说明它还在干活，不是收尾
            return ModelResponse(parts=[call_tool("run_command", {"command": "true", "verify": True}, "c2")])
        return final("改完并验证通过")

    state = SessionState(session_id="closure-busy")
    try:
        run_turn(state, respond)
        # 一次都没拦：第 2 轮有工具调用，第 3 轮收尾时验证已通过
        assert state.iteration.interventions == 0
        assert state.iteration.last_verification().passed is True
        assert len(requests) == 3
    finally:
        asyncio.run(state.job_registry.aclose())


def test_failed_verification_output_flows_back(monkeypatch, tmp_path):
    """验证失败时，退出码与输出尾部随拦停提示回流到模型的下一轮上下文。"""
    configure_run(monkeypatch)
    target = tmp_path / "broken.py"
    # 把失败脚本写成文件再执行：避免命令串里的嵌套引号，也让断言能锚定在唯一的标记行上
    probe = tmp_path / "probe.py"
    probe.write_text("print('BOOM_MARKER')\nraise SystemExit(3)\n", encoding="utf-8")
    fail_command = f"{sys.executable} {probe}"
    requests = []

    def respond(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            return ModelResponse(parts=[call_tool("write_file", {"path": str(target), "content": "broken\n"}, "c1")])
        if len(requests) == 2:
            return ModelResponse(parts=[call_tool("run_command", {"command": fail_command, "verify": True}, "c2")])
        return final("我以为修好了")

    state = SessionState(session_id="closure-fail")
    try:
        run_turn(state, respond)
        last = state.iteration.last_verification()
        assert last is not None and last.passed is False and last.exit_code == 3
        retry_text = " ".join(str(p) for p in prompts(requests[-1], "retry-prompt"))
        assert "BOOM_MARKER\n" in retry_text  # 报错原文回流（带换行，避免和命令串里的脚本路径撞车）
        assert "退出码：3" in retry_text
        assert fail_command in retry_text
    finally:
        asyncio.run(state.job_registry.aclose())


def test_passing_verification_is_never_interrupted(monkeypatch, tmp_path):
    """模型改完文件后声明 verify=True 且命令通过 → 全程零拦停。"""
    configure_run(monkeypatch)
    target = tmp_path / "ok.py"
    requests = []

    def respond(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            return ModelResponse(parts=[call_tool("write_file", {"path": str(target), "content": "ok\n"}, "c1")])
        if len(requests) == 2:
            return ModelResponse(parts=[call_tool("run_command", {"command": "true", "verify": True}, "c2")])
        return final("已完成并验证通过")

    state = SessionState(session_id="closure-pass")
    try:
        run_turn(state, respond)
        last = state.iteration.last_verification()
        assert last is not None and last.passed and last.exit_code == 0
        assert state.iteration.needs_verification() is False
        assert state.iteration.interventions == 0
        assert all(prompts(messages, "retry-prompt") == [] for messages in requests)
    finally:
        asyncio.run(state.job_registry.aclose())


def test_iteration_state_reset_between_turns(monkeypatch, tmp_path):
    """每轮用户输入重新开始记账：上一轮攒下的改动、验证结果和介入计数都不带到下一轮。"""
    configure_run(monkeypatch)
    target = tmp_path / "once.py"
    counter = {"requests": 0, "turn_start": 1}

    def respond(messages, info):
        counter["requests"] += 1
        # 每轮开头都先改一次文件：这样两轮结束时 edited_paths 都非空，
        # 能把「本轮清账后重新累积」和「上一轮旧账残留」区分开
        if counter["requests"] == counter["turn_start"]:
            return ModelResponse(parts=[call_tool("write_file", {"path": str(target), "content": "y\n"}, "c1")])
        return final("done")

    state = SessionState(session_id="closure-reset")
    try:
        run_turn(state, respond)
        first_turn_requests = counter["requests"]
        assert state.iteration.edited_paths == [str(target)]
        assert state.iteration.interventions == MAX_INTERVENTIONS

        # 第二轮：run_agent_loop 先清掉上一轮的账（interventions 归零、验证记录清空），再重新累积
        counter["turn_start"] = first_turn_requests + 1
        run_turn(state, respond)
        assert counter["requests"] > first_turn_requests  # 第二轮确实跑了新的模型调用
        assert state.iteration.edited_paths == [str(target)]  # 只有本轮这一次改动
        assert state.iteration.verification_runs == []
    finally:
        asyncio.run(state.job_registry.aclose())


def test_verify_reminder_builder_contract():
    """提醒正文 builder 的纯函数契约：无欠账返回 None，有欠账返回包裹在 system-reminder 里的要求。"""
    state = IterationState()
    assert build_verify_reminder_text(state) is None

    state.mark_edit("/tmp/a.py")
    text = build_verify_reminder_text(state)
    assert text is not None
    assert text.startswith("<system-reminder>") and text.endswith("</system-reminder>")
    assert "/tmp/a.py" in text
    # 记账在外层拦停点做，builder 自己不改介入计数
    assert state.interventions == 0

    # 通过的验证之后不再需要提醒
    state.mark_verification("pytest -q", 0, "1 passed")
    assert build_verify_reminder_text(state) is None


def test_run_command_records_verification_only_when_declared():
    """run_command 只在 verify=True 时记账；verify 默认 False 时闭环状态不动。"""
    from types import SimpleNamespace

    from background_jobs import JobRegistry

    async def scenario():
        registry = JobRegistry("closure-shell")
        state = IterationState()
        ctx = SimpleNamespace(deps=SimpleNamespace(job_registry=registry, iteration=state))
        try:
            await run_command(ctx, "true")
            assert state.verification_runs == []
            await run_command(ctx, "true", verify=True)
            assert len(state.verification_runs) == 1
            assert state.verification_runs[0].passed is True
        finally:
            await registry.aclose()

    asyncio.run(scenario())
