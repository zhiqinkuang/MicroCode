"""
P0：子代理 token 计量的回归测试。

背景（已确认的缺陷）：main.py 只把主 agent 的 result.usage 累加进 SessionState，
而子代理在 run_subagent 里独立跑，它的用量既不入 job 也不入会话累计。
后果是任何用过 run_agent 的场景，token 数字都偏低——而子代理恰恰是 token 大户
（全新上下文要完整交代背景 + 最多 40 轮 + 最终报告）。P0 修掉它，本文件守住它。
"""
import asyncio
import json
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import main
import permissions
import subagents
from agent.reminders import build_job_reminder_text
from background_jobs import Job, JobRegistry
from UI.commands import SessionState

# 子代理替身模型的用量（pydantic-ai 的 RunUsage 字段名是 input_tokens / output_tokens）
SUB_INPUT_TOKENS = 50
SUB_OUTPUT_TOKENS = 5


def _subagent_model():
    """一个会正常收尾的子代理替身模型，并带固定用量。"""
    def respond(messages, info):
        return ModelResponse(
            parts=[TextPart("子代理报告：看完了")],
            usage=_usage(SUB_INPUT_TOKENS, SUB_OUTPUT_TOKENS),
        )

    return FunctionModel(respond)


def _usage(input_tokens, output_tokens):
    from pydantic_ai.usage import RunUsage

    return RunUsage(input_tokens=input_tokens, output_tokens=output_tokens)


# 主 agent 替身报一组「大且可辨识」的用量，子代理报一组小值。
#
# 为什么不用 0 或省略 usage：pydantic-ai 在模型不报用量时会**估算** token 数
# （实测一份两轮的派发记录被估成 input=117）。这个估算值会蒙过「>= 子代理用量」这类
# 断言，让测试假绿——本文件第一版就是这么错的，必须记住。
# 显式报一组远大于子代理的值之后，算术关系变得无法混淆：
#   会话累计 == 主 agent 轮数 × 主用量 + 子代理用量
MAIN_INPUT_PER_CALL = 1000
MAIN_OUTPUT_PER_CALL = 200


def _main_model_dispatching():
    """主 agent 替身：先派发一个 explore 子代理，再收尾。报 0 用量，见上方注释。"""
    def respond(messages, info):
        tool_returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return"]
        usage = _usage(MAIN_INPUT_PER_CALL, MAIN_OUTPUT_PER_CALL)
        if not tool_returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="run_agent",
                args=json.dumps({"description": "看看结构", "prompt": "读一下目录", "agent_type": "explore"}),
                tool_call_id="m1",
            )], usage=usage)
        return ModelResponse(parts=[TextPart("已派发")], usage=usage)

    return FunctionModel(respond)


def _configure_offline(monkeypatch):
    monkeypatch.setattr(main.mcp_servers, "active_toolsets", lambda: [])
    monkeypatch.setattr(main.memory_background, "schedule", lambda state, messages: None)
    monkeypatch.setattr(main, "print_part", lambda part: None)
    permissions.state.mode = permissions.BYPASS


async def _run_turn_with_subagent(state, atype, timeout=25):
    """
    跑一轮「主 agent 派发子代理」，等子代理结束。

    主循环与等待必须在同一个事件循环里：asyncio.run 返回时会关闭循环并取消
    在跑的子代理任务（日志里只剩一行头的症状就是它）。
    """
    await main.run_agent_loop("派个子代理", state, model=_main_model_dispatching())

    deadline = asyncio.get_running_loop().time() + timeout
    while state.job_registry.running() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)


def test_subagent_usage_is_recorded_on_the_job(monkeypatch, tmp_path):
    """子代理跑完必须把自己的用量写进 job——这是结算的前提。"""
    _configure_offline(monkeypatch)
    atype = subagents.get_agent_type("explore")
    original = atype.agent._model
    atype.agent._model = _subagent_model()
    state = SessionState(session_id="usage-on-job")
    try:
        asyncio.run(_run_turn_with_subagent(state, atype))
        jobs = state.job_registry.list()
        assert len(jobs) == 1, "应当派发了一个子代理 job"
        job = jobs[0]
        assert job.status == "completed", f"子代理没有正常结束：{job.status}\n{job.log_path.read_text()}"
        usage = getattr(job, "usage", None)
        assert usage is not None, "job 上没有记录子代理用量"
        assert usage.input_tokens == SUB_INPUT_TOKENS
        assert usage.output_tokens == SUB_OUTPUT_TOKENS
    finally:
        atype.agent._model = original
        asyncio.run(state.job_registry.aclose())


def test_session_tokens_include_subagent_usage(monkeypatch, tmp_path):
    """
    主断言：一轮「主 agent + 一个子代理」结束后的会话累计，必须大于主 agent 单独的用量。

    修复前这条必失败——子代理的 50/5 完全不在计数里。
    """
    _configure_offline(monkeypatch)
    atype = subagents.get_agent_type("explore")
    original = atype.agent._model
    atype.agent._model = _subagent_model()
    state = SessionState(session_id="usage-session")
    try:
        asyncio.run(_run_turn_with_subagent(state, atype))
        state.settle_job_usage()

        # 主 agent 被调用 2 次（派发 + 收尾），所以会话累计 = 2×主用量 + 子代理用量
        expected_input = 2 * MAIN_INPUT_PER_CALL + SUB_INPUT_TOKENS
        expected_output = 2 * MAIN_OUTPUT_PER_CALL + SUB_OUTPUT_TOKENS
        assert (state.input_tokens, state.output_tokens) == (expected_input, expected_output), (
            f"会话累计 {(state.input_tokens, state.output_tokens)} "
            f"与「主 agent 自己 {(2 * MAIN_INPUT_PER_CALL, 2 * MAIN_OUTPUT_PER_CALL)} "
            f"+ 子代理 {(SUB_INPUT_TOKENS, SUB_OUTPUT_TOKENS)}」不符"
        )
    finally:
        atype.agent._model = original
        asyncio.run(state.job_registry.aclose())


def test_settling_twice_does_not_double_count(monkeypatch, tmp_path):
    """结算必须幂等：同一 job 的用量只能并入一次，否则数字会被重复放大。"""
    _configure_offline(monkeypatch)
    atype = subagents.get_agent_type("explore")
    original = atype.agent._model
    atype.agent._model = _subagent_model()
    state = SessionState(session_id="usage-idempotent")
    try:
        asyncio.run(_run_turn_with_subagent(state, atype))

        state.settle_job_usage()
        first_input, first_output = state.input_tokens, state.output_tokens

        state.settle_job_usage()
        state.settle_job_usage()
        assert (state.input_tokens, state.output_tokens) == (first_input, first_output), (
            "重复结算把子代理用量计入了多次"
        )
    finally:
        atype.agent._model = original
        asyncio.run(state.job_registry.aclose())


def test_settle_only_returns_jobs_with_usage_and_marks_them(monkeypatch):
    """注册表层面的结算契约：只交出有用量且未结算过的 job，并打上已结算标记。"""
    registry = JobRegistry(session_id="settle-contract")
    try:
        with_usage = Job(
            id="a-usage", kind="agent", description="d", log_path=registry._jobs_dir / "a.log",
            status="completed", usage=_usage(7, 3),
        )
        without_usage = Job(
            id="b-nousage", kind="agent", description="d", log_path=registry._jobs_dir / "b.log",
            status="completed",
        )
        still_running = Job(
            id="c-running", kind="agent", description="d", log_path=registry._jobs_dir / "c.log",
            status="running", usage=_usage(9, 9),
        )
        registry._jobs.update({j.id: j for j in (with_usage, without_usage, still_running)})

        settled = registry.settle_usage()
        assert [j.id for j in settled] == ["a-usage"]
        assert with_usage.usage_settled is True
        # 第二次不再交出任何 job
        assert registry.settle_usage() == []
        # 没用量 / 还在跑的都不参与
        assert without_usage.usage_settled is False
        assert still_running.usage_settled is False
    finally:
        asyncio.run(registry.aclose())


def test_settle_after_job_finishes_late_is_picked_up(monkeypatch, tmp_path):
    """
    子代理在下一轮才结束时，它的用量也不能丢。

    run_agent_loop 结束时会结算一次；一个结束得更晚的 job 由下一次结算兜住。
    """
    _configure_offline(monkeypatch)
    atype = subagents.get_agent_type("explore")
    original = atype.agent._model
    atype.agent._model = _subagent_model()
    state = SessionState(session_id="usage-late")
    try:
        asyncio.run(_run_turn_with_subagent(state, atype))
        state.settle_job_usage()
        baseline = state.input_tokens

        # 模拟一个「跑完但还没结算」的 job，下一次结算必须把它算进来
        late = Job(
            id="a-late", kind="agent", description="late", log_path=state.job_registry._jobs_dir / "late.log",
            status="completed", usage=_usage(30, 4),
        )
        state.job_registry._jobs[late.id] = late

        state.settle_job_usage()
        assert state.input_tokens == baseline + 30
        assert state.output_tokens == 2 * MAIN_OUTPUT_PER_CALL + SUB_OUTPUT_TOKENS + 4
    finally:
        atype.agent._model = original
        asyncio.run(state.job_registry.aclose())


def test_late_finishing_subagent_is_settled_on_the_next_turn(monkeypatch, tmp_path):
    """
    晚跑完的子代理必须在下一轮开始时被计入。

    子代理在 run_agent_loop 返回时通常还在 running（异步派发本就如此），那一轮内没有
    可结算的东西。真正保证「终会被计入」的是下一轮开始时的结算（或空闲轮询里的那次）。
    这条用例覆盖前者，且刻意模拟「通知已经取走、但还没结算」的状态——
    实现过程中曾把结算放在只有 registry、拿不到 state 的通知函数里，
    结果是 job 被标记为已结算却没真正累加，用量永久丢失。
    """
    _configure_offline(monkeypatch)
    atype = subagents.get_agent_type("explore")
    original = atype.agent._model
    atype.agent._model = _subagent_model()
    state = SessionState(session_id="usage-late-turn")
    try:
        asyncio.run(_run_turn_with_subagent(state, atype))
        before = state.input_tokens

        # 先取走通知（生产里 watch_jobs 的顺序是「先结算、再取通知」，这里刻意反过来，
        # 确认取通知这个动作本身不会把 job 的用量吞掉）
        assert build_job_reminder_text(state.job_registry) is not None

        # 下一轮开始 → 结算兜住
        asyncio.run(main.run_agent_loop("再说一句", state, model=FunctionModel(
            lambda m, i: ModelResponse(parts=[TextPart("好")], usage=_usage(MAIN_INPUT_PER_CALL, MAIN_OUTPUT_PER_CALL))
        )))

        expected = before + MAIN_INPUT_PER_CALL + SUB_INPUT_TOKENS
        assert state.input_tokens == expected, (
            f"下一轮没有结算上一轮跑完的子代理（{state.input_tokens} != {expected}）"
        )
    finally:
        atype.agent._model = original
        asyncio.run(state.job_registry.aclose())


def test_run_agent_loop_settles_jobs_finished_before_it_returns(monkeypatch, tmp_path):
    """
    收尾结算的兜底：本轮返回时已经结束的 job，它的用量必须在同一轮内并入累计，
    不能等到下一次通知（否则 /status 看到的数字会持续偏小）。
    """
    _configure_offline(monkeypatch)
    state = SessionState(session_id="usage-loop-backstop")
    try:
        # 预置一个「已结束但未结算」的 job，模拟主循环期间就跑完的子代理
        done = Job(
            id="a-done", kind="agent", description="早跑完的", log_path=state.job_registry._jobs_dir / "d.log",
            status="completed", usage=_usage(SUB_INPUT_TOKENS, SUB_OUTPUT_TOKENS),
        )
        state.job_registry._jobs[done.id] = done

        asyncio.run(main.run_agent_loop("随便说句", state, model=FunctionModel(
            lambda m, i: ModelResponse(parts=[TextPart("好")], usage=_usage(MAIN_INPUT_PER_CALL, MAIN_OUTPUT_PER_CALL))
        )))

        expected = MAIN_INPUT_PER_CALL + SUB_INPUT_TOKENS
        assert state.input_tokens == expected, (
            f"run_agent_loop 收尾没有结算已完成 job（{state.input_tokens} != {expected}）"
        )
        assert done.usage_settled is True
    finally:
        asyncio.run(state.job_registry.aclose())
