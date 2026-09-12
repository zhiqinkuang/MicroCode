"""
Eval 管线的离线测试：判定器自测 + 单任务运行器的接口与端到端。

上半部分（判定器）是整个 eval 里最关键的测试：判定器是完成率的唯一来源——
一个会把作弊判成通过的判定器，产出的完成率比没有完成率更有害。
所以每一类作弊手法都有独立用例，且断言失败理由命中对应类别。

下半部分（运行器）覆盖夹具加载、文件快照与 diff、pytest 汇总行解析，
以及一条用 FunctionModel 替身跑完整条链路的端到端用例（运行器 → 记录 → 判定器）。

**全部离线**：不跑真实模型、不碰真实剪贴板预设，也不进真实主目录。
"""
import copy
import json
import os
import shutil
import subprocess
import sys

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from scripts.eval.judge import collect_reasons, judge
from scripts.eval.run_task import (
    REPO_ROOT,
    diff_since,
    load_task,
    parse_pytest_summary,
    resolve_command,
    run_one,
    snapshot,
)


def make_task(**overrides):
    """一个「正确修复」的任务声明：只允许改实现文件，测试与判定配置只读且不得改。"""
    task = {
        "id": "sample-task",
        "writable_paths": ["src/**/*.py"],
        "readonly_paths": ["tests/**/*.py", "conftest.py", "pytest.ini", "pyproject.toml"],
        "baseline_failed": True,
        "baseline_collected": 5,
    }
    task.update(overrides)
    return task


class Replace:
    """
    标记「整段替换」而不是「部分覆盖」。

    默认的部分覆盖用 dict.update，改不了「故意缺字段」这种场景——
    缺字段本来就该是记录的完整形态，而不是在完整记录上删掉一个键。
    """

    def __init__(self, value):
        self.value = value


def make_record(**overrides):
    """
    一条「正确修复」的记录：基线 5 个测试失败，最终全通过，只改了实现文件，无人碰测试。
    各用例在此之上只改一处，保证失败理由能归因到那一处。
    传 Replace({...}) 可整段替换某个子字典（用于构造「缺字段」这类记录）。
    """
    record = {
        "task": make_task(),
        "run": {
            "changed_paths": ["src/solution.py"],
            "diff": "--- a/src/solution.py\n+++ b/src/solution.py\n-return a - b\n+return a + b\n",
            "error": None,
        },
        "tests": {"returncode": 0, "collected": 5, "failed": 0, "passed": 5},
    }
    for key, value in overrides.items():
        if key not in ("task", "run", "tests"):
            record[key] = value
        elif isinstance(value, Replace):
            record[key] = value.value
        else:
            record[key].update(value)
    return record


# ---------- 正向：正确修复必须判 PASS ----------

def test_correct_fix_passes():
    verdict = judge(make_record())
    assert verdict["passed"] is True, verdict["reasons"]
    assert verdict["reasons"] == []


# ---------- 反向：各类作弊必须判 FAIL，且理由命中对应类别 ----------

def test_failing_test_is_rejected():
    """最终测试没通过——这是最朴素的 FAIL，不能因为别处看起来正常就放行。"""
    record = make_record(tests={"returncode": 1, "collected": 5, "failed": 2, "passed": 3})
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("未通过" in r for r in verdict["reasons"])
    assert any("2 个失败" in r for r in verdict["reasons"])


def test_modifying_the_test_instead_of_the_implementation_is_rejected():
    """改测试而不是改实现：最经典的一种，靠只读白名单拦。"""
    record = make_record(run={"changed_paths": ["src/solution.py", "tests/test_solution.py"]})
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("只读路径" in r and "tests/test_solution.py" in r for r in verdict["reasons"])


def test_deleting_tests_is_rejected_by_collection_count():
    """删掉失败的测试：收集数会减少，必须抓到。"""
    record = make_record(tests={"returncode": 0, "collected": 3, "failed": 0, "passed": 3})
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("收集数从 5 降到 3" in r for r in verdict["reasons"])


def test_skipping_tests_in_the_diff_is_rejected():
    """用 pytest.skip / mark.skip 让失败消失：diff 里必须能看出来。"""
    for pattern in ("pytest.skip(", "pytest.mark.skip", "pytest.mark.xfail"):
        record = make_record(run={
            "changed_paths": ["src/solution.py"],
            "diff": f"+    {pattern}\n",
            "error": None,
        })
        verdict = judge(record)
        assert verdict["passed"] is False, pattern
        assert any("失去意义" in r for r in verdict["reasons"]), pattern


def test_hardcoding_against_visible_cases_fails_hidden_tests():
    """只对可见用例成立的改动：隐藏用例必须让它失败。"""
    record = make_record(run={
        "changed_paths": ["src/solution.py"],
        "diff": "+if x == 3: return 6\n",
        "hidden_tests": {"returncode": 1, "failed": 4, "passed": 0},
        "error": None,
    })
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("隐藏用例未通过" in r for r in verdict["reasons"])


def test_writing_outside_the_whitelist_is_rejected():
    """白名单之外的任何路径都不许动——包括新建文件。"""
    record = make_record(run={
        "changed_paths": ["src/solution.py", "src/helper_new.py", "notes.md"],
        "diff": "",
        "error": None,
    })
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("白名单之外" in r and "notes.md" in r for r in verdict["reasons"])


def test_changing_judging_config_is_rejected():
    """改 conftest / pytest.ini / pyproject：等于换了判据。"""
    for path in ("conftest.py", "pytest.ini", "pyproject.toml"):
        record = make_record(run={"changed_paths": ["src/solution.py", path], "diff": "", "error": None})
        verdict = judge(record)
        assert verdict["passed"] is False, path
        assert any("只读路径" in r and path in r for r in verdict["reasons"]), path


def test_task_without_a_failing_baseline_is_rejected():
    """
    基线测试在改动前就没失败：这个任务没有可验证的起点。
    这属于夹具问题，但绝不能算成 agent 的成功——否则一个坏夹具会凭空贡献完成率。
    """
    record = make_record(task={"baseline_failed": False})
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("没有可验证的起点" in r for r in verdict["reasons"])


def test_zero_passing_tests_is_rejected_even_with_returncode_zero():
    """returncode=0 但一个测试都没通过：多半是测试没被收集或全被删了。"""
    record = make_record(tests={"returncode": 0, "collected": 0, "failed": 0, "passed": 0})
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("没有真正验证到东西" in r for r in verdict["reasons"])


def test_run_error_is_surfaced():
    """运行器自身报错（超时/环境坏）不能被当成 agent 的失败或成功，必须显式暴露。"""
    record = make_record(run={"changed_paths": [], "diff": "", "error": "TimeoutError: 任务超时"})
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("运行过程报错" in r for r in verdict["reasons"])


def test_missing_test_fields_are_reported_instead_of_guessed():
    """记录不完整时必须报出来，而不是默认通过或默认失败。"""
    record = make_record(tests=Replace({"returncode": 0, "passed": 5}))
    verdict = judge(record)
    assert verdict["passed"] is False
    assert any("缺少 tests.collected" in r for r in verdict["reasons"]), verdict["reasons"]


# ---------- 判定器自身的性质 ----------

def test_judge_is_deterministic_and_pure():
    """
    同一份记录重复判定必须完全一致，且判定不得修改记录。
    不确定性会直接变成完成率的噪声；修改记录会让复判不可信。
    """
    record = make_record()
    snapshot = copy.deepcopy(record)
    verdicts = [judge(record) for _ in range(5)]
    assert all(v == verdicts[0] for v in verdicts)
    assert record == snapshot, "判定器修改了输入记录"


def test_judge_does_not_depend_on_the_filesystem(tmp_path, monkeypatch):
    """
    判定只读记录：即使当前工作目录里根本没有记录里提到的文件，判定结果也不变。
    这条保证「复判旧记录」永远有效——判定器改了可以对历史记录重判，不用重跑烧钱。
    """
    record = make_record()
    baseline = judge(record)
    monkeypatch.chdir(tmp_path)          # 换到一个空目录，记录里的路径都不存在
    assert judge(record) == baseline


# ---------- 判定器自测的门禁：标注样本上的误判率 ----------

# (标注标签, 记录构造器, 期望判定)
LABELED_CASES = [
    ("correct", lambda: make_record(), True),
    ("correct-minimal-diff", lambda: make_record(run={"changed_paths": ["src/solution.py"], "diff": "+x=1", "error": None}), True),
    ("cheat-edit-test", lambda: make_record(run={"changed_paths": ["tests/test_solution.py"], "diff": "", "error": None}), False),
    ("cheat-delete-test", lambda: make_record(tests={"returncode": 0, "collected": 2, "failed": 0, "passed": 2}), False),
    ("cheat-skip", lambda: make_record(run={"changed_paths": ["src/solution.py"], "diff": "+    pytest.skip('later')", "error": None}), False),
    ("cheat-hardcode", lambda: make_record(run={"changed_paths": ["src/solution.py"], "diff": "+return 42", "hidden_tests": {"returncode": 1}, "error": None}), False),
    ("cheat-config", lambda: make_record(run={"changed_paths": ["conftest.py"], "diff": "", "error": None}), False),
    ("incomplete", lambda: make_record(tests={"returncode": 1, "collected": 5, "failed": 1, "passed": 4}), False),
    ("no-baseline", lambda: make_record(task={"baseline_failed": False}), False),
    ("run-error", lambda: make_record(run={"changed_paths": [], "diff": "", "error": "boom"}), False),
]


def test_judge_agrees_with_human_labels():
    """
    标注样本一致性门禁：判定器必须与人工标注完全一致。

    计划里要求误判率 < 5%。在这样一份小样本上，5% 的容错没有意义——
    所以这里卡的是**零误判**：任何一条不一致都会让本用例失败并点名是哪一类。
    """
    mismatches = [
        f"{label}: 人工={expected} 判定={judge(build())['passed']}"
        for label, build, expected in LABELED_CASES
        if judge(build())["passed"] != expected
    ]
    assert mismatches == [], "判定器与人工标注不一致：\n  " + "\n  ".join(mismatches)


def test_labeled_cases_cover_both_directions():
    """样本集本身要两个方向都有，否则「零误判」可能只是因为全是 FAIL。"""
    labels = {expected for _label, _build, expected in LABELED_CASES}
    assert labels == {True, False}


def test_collect_reasons_returns_all_problems_not_just_the_first():
    """一次给出全部问题：调试时不用反复重跑（每次重跑都要烧 token）。"""
    record = make_record(
        tests={"returncode": 1, "collected": 2, "failed": 1, "passed": 1},
        run={"changed_paths": ["tests/test_solution.py", "notes.md"], "diff": "", "error": None},
    )
    reasons = collect_reasons(record)
    assert len(reasons) >= 3, reasons
    joined = " ".join(reasons)
    assert "未通过" in joined and "只读路径" in joined and "白名单之外" in joined


def test_eval_judge_cli_returns_exit_code_by_verdict(tmp_path):
    """命令行入口：PASS 返回 0、FAIL 返回 1，方便脚本串联。"""
    from scripts.eval import judge as judge_module

    good = tmp_path / "good.json"
    good.write_text(json.dumps(make_record(), ensure_ascii=False), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(make_record(task={"baseline_failed": False}), ensure_ascii=False), encoding="utf-8")

    import sys
    argv_backup = sys.argv
    try:
        sys.argv = ["judge.py", str(good)]
        assert judge_module.main() == 0
        sys.argv = ["judge.py", str(bad)]
        assert judge_module.main() == 1
    finally:
        sys.argv = argv_backup

# ============================ 运行器（scripts/eval/run_task.py）============================

FIXTURE = REPO_ROOT / "scripts" / "eval" / "tasks" / "example-fix-addition"
FIXED_IMPLEMENTATION = '''"""修好后的实现。"""


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b
'''


# ---------- 纯函数层：快、无副作用 ----------

def test_load_task_reads_the_fixture():
    config = load_task(FIXTURE)
    assert config["id"] == "example-fix-addition"
    assert "{python}" in config["test_command"][0]
    assert config["writable_paths"] == ["src/**/*.py"]
    assert "tests/**/*.py" in config["readonly_paths"]


def test_load_task_rejects_incomplete_fixture(tmp_path):
    """缺必填字段的夹具必须当场报错，而不是跑出一堆无意义的记录。"""
    (tmp_path / "start").mkdir()
    (tmp_path / "task.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_task(tmp_path)
    assert "description" in str(excinfo.value) and "test_command" in str(excinfo.value)


def test_load_task_rejects_missing_start_dir(tmp_path):
    (tmp_path / "task.json").write_text(
        json.dumps({"id": "x", "description": "d", "test_command": ["true"]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="start"):
        load_task(tmp_path)


def test_resolve_command_substitutes_placeholders(tmp_path):
    command = resolve_command(["{python}", "-m", "pytest", "{workspace}/tests"], tmp_path)
    assert command[0] != "{python}"
    assert command[-1] == f"{tmp_path}/tests"


def test_snapshot_and_diff_detect_added_modified_and_deleted(tmp_path):
    """删文件也必须进入 changed_paths——「把测试删掉」正是要抓的作弊手法。"""
    (tmp_path / "keep.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "edit.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "delete.py").write_text("bye\n", encoding="utf-8")
    before = snapshot(tmp_path)

    (tmp_path / "edit.py").write_text("new\n", encoding="utf-8")
    (tmp_path / "delete.py").unlink()
    (tmp_path / "added.py").write_text("hi\n", encoding="utf-8")
    after = snapshot(tmp_path)

    changed, diff_text = diff_since(before, after)
    assert set(changed) == {"added.py", "delete.py", "edit.py"}
    assert "keep.py" not in changed
    assert "+new" in diff_text and "-old" in diff_text


def test_snapshot_ignores_pycache_and_pyc(tmp_path):
    """缓存文件不算改动，否则每次运行都会产生噪声。"""
    (tmp_path / "mod.py").write_text("x\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
    assert list(snapshot(tmp_path)) == ["mod.py"]


def test_parse_pytest_summary_reads_counts():
    output = "tests/test_calc.py::test_a PASSED\n\n2 failed, 1 passed, 1 skipped in 0.02s\n"
    assert parse_pytest_summary(output) == {"failed": 2, "passed": 1, "skipped": 1, "collected": 4}


def test_parse_pytest_summary_returns_empty_when_unparsable():
    """
    解析不出汇总行时返回空字典，让判定器因缺字段而拒绝判定——
    绝不能编一个「看起来通过」的数字出来。
    """
    assert parse_pytest_summary("完全不是 pytest 的输出") == {}
    assert parse_pytest_summary("") == {}


# ---------- 端到端层：运行器 → 记录 → 判定器 ----------

def test_runner_end_to_end_with_a_scripted_fix(tmp_path):
    """
    走完整条链路：运行器建隔离工作区 → 基线测试（必须失败）→ 驱动 agent 修好 →
    跑最终测试与隐藏用例 → 写记录 → 判定器判 PASS。

    顺带守住两个容易被忽略的点：隐藏用例跑完必须被清掉（否则会出现在 changed_paths
    里，被判定器当成越权改动而误判）、记录里必须有事件流与用量。
    """
    def respond(messages, info):
        tool_returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return"]
        if not tool_returns:
            # 先读文件再改：读不到内容就说明工作区没建对，这一步顺带验证了起始代码真的被拷进去
            return ModelResponse(parts=[ToolCallPart(
                tool_name="read_file", args=json.dumps({"path": "src/calc.py"}), tool_call_id="c1",
            )])
        if len(tool_returns) == 1:
            assert "a - b" in str(tool_returns[0].content), "agent 应当读到有 bug 的实现"
            return ModelResponse(parts=[ToolCallPart(
                tool_name="write_file",
                args=json.dumps({"path": "src/calc.py", "content": FIXED_IMPLEMENTATION}),
                tool_call_id="c2",
            )])
        if len(tool_returns) == 2:
            # 声明一次通过的验证：闭环闸门是产品行为，不该干扰本用例
            return ModelResponse(parts=[ToolCallPart(
                tool_name="run_command", args=json.dumps({"command": "true", "verify": True}), tool_call_id="c3",
            )])
        return ModelResponse(parts=[TextPart("已修好 add 的实现")])

    out_dir = tmp_path / "runs"
    record_path = run_one(FIXTURE, version="scripted", repeat=1, out_dir=out_dir, model=FunctionModel(respond))
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert record["task"]["baseline_failed"] is True, "基线必须先失败，否则任务没有可验证起点"
    assert record["task"]["baseline_collected"] == 3

    assert record["tests"]["failed"] == 0
    assert record["tests"]["passed"] == 3
    assert record["run"]["hidden_tests"]["failed"] == 0
    assert record["run"]["hidden_tests"]["passed"] == 3

    # 只改了实现文件；隐藏用例文件不得出现在改动清单里
    assert record["run"]["changed_paths"] == ["src/calc.py"]
    assert record["run"]["error"] is None

    kinds = {event["kind"] for event in record["run"]["events"]}
    assert {"tool-call", "tool-return"} <= kinds
    assert record["run"]["usage"]["input_tokens"] >= 0

    assert record_path.parent.name == "scripted"
    assert record["meta"]["version"] == "scripted"
    assert judge(record)["passed"] is True, judge(record)["reasons"]


def test_runner_records_a_noop_agent_as_fail(tmp_path):
    """agent 什么都没改 → 记录如实反映失败，判定器判 FAIL。"""
    def respond(messages, info):
        return ModelResponse(parts=[TextPart("我觉得没问题，不用改")])

    record_path = run_one(
        FIXTURE, version="noop", repeat=1, out_dir=tmp_path / "runs", model=FunctionModel(respond)
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert record["run"]["changed_paths"] == []
    verdict = judge(record)
    assert verdict["passed"] is False, "什么都没改却被判通过"
    assert verdict["reasons"], "FAIL 必须给出理由"

# ============================ 夹具自证（防止白烧 token）============================

ORDER_REPORT = REPO_ROOT / "scripts" / "eval" / "tasks" / "order-report"

# 这个夹具的 bug 是「税基取错了变量」：折扣算对了，但税按未打折金额算。
# 税基从 gross 改成 net 即为正解。
GOLDEN_PATCH = (
    "    tax = tax_cents(gross, tax_rate)",
    "    tax = tax_cents(net, tax_rate)",
)


def _run_fixture_tests(workspace, target: str, extra_env=None):
    """在夹具工作区里跑一次 pytest，返回 (returncode, 输出)。"""
    env = os.environ.copy()
    env.pop("CODING_AGENT_DISABLE_SKILLS", None)
    env.pop("CODING_AGENT_DISABLE_SUBAGENTS", None)
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q"],
        cwd=workspace, capture_output=True, text=True, env=env, timeout=180,
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_order_report_fixture_baseline_fails(tmp_path):
    """
    新夹具的基线必须失败。

    如果哪天有人把这个夹具里的 bug「顺手修好」，基线就不再失败，
    评测会把任务判成「没有可验证起点」——**静默烧掉整轮 token 却什么也没测到**。
    这条用例就是防这个的。
    """
    workspace = tmp_path / "ws"
    shutil.copytree(ORDER_REPORT / "start", workspace)
    returncode, output = _run_fixture_tests(workspace, "tests/")
    assert returncode != 0, f"夹具基线不该通过：\n{output[-800:]}"


def test_order_report_fixture_is_solvable_with_the_golden_patch(tmp_path):
    """
    夹具必须有解，且解了之后可见测试与隐藏用例都通过。

    这同时验证了两件事：夹具本身可解（不是无解题），以及隐藏用例的期望值
    与正解一致（否则会出现「改对了却被隐藏用例判失败」的假失败）。
    """
    workspace = tmp_path / "ws"
    shutil.copytree(ORDER_REPORT / "start", workspace)
    # 隐藏用例放在工作区之外，靠 PYTHONPATH 指回工作区的 src/
    hidden = tmp_path / "hidden"
    shutil.copytree(ORDER_REPORT / "hidden", hidden)

    pricing = workspace / "src" / "report" / "pricing.py"
    source = pricing.read_text(encoding="utf-8")
    assert GOLDEN_PATCH[0] in source, "夹具的 pricing.py 变了，请同步更新正解补丁"
    pricing.write_text(source.replace(*GOLDEN_PATCH), encoding="utf-8")

    visible_code, visible_out = _run_fixture_tests(workspace, "tests/")
    assert visible_code == 0, f"应用正解后可见测试仍失败：\n{visible_out[-800:]}"

    hidden_code, hidden_out = _run_fixture_tests(hidden, ".", {"PYTHONPATH": str(workspace)})
    assert hidden_code == 0, f"应用正解后隐藏用例仍失败：\n{hidden_out[-800:]}"


def test_order_report_fixture_requires_cross_module_understanding():
    """
    夹具必须保持「单文件看不出问题」的形状：pricing.py 里折扣算对了、只有税基错了。

    这条守住夹具的设计意图——如果它退化成一眼可见的单行 bug，
    就再也测不出「探索」与「上下文隔离」这类机制（P2-a 第一版夹具的教训）。
    """
    pricing = (ORDER_REPORT / "start" / "src" / "report" / "pricing.py").read_text(encoding="utf-8")
    assert "apply_discount(gross, discount_rate)" in pricing, "折扣必须算对，错处只在税基"
    assert "tax_cents(gross, tax_rate)" in pricing, "税基应当（错误地）取未打折金额"
    assert "tax_cents(net, tax_rate)" not in pricing, "夹具里不该出现正解"

    # 业务口径写在 README 里：模型必须读它才能判断税基该用哪一个
    readme = (ORDER_REPORT / "README.md").read_text(encoding="utf-8")
    assert "折扣先于税" in readme
    # 项目自带 skill 讲金额约定：这是「按需加载 Skill」这条机制的用武之地
    skill = (ORDER_REPORT / "start" / ".my-claude-code" / "skills" / "money-rules" / "SKILL.md").read_text(encoding="utf-8")
    assert "折扣先于税" in skill


def test_order_report_fixture_declares_readonly_paths_that_cover_tests():
    """测试与隐藏用例必须在只读白名单里，否则「改测试通过」这条作弊路径就是敞开的。"""
    config = load_task(ORDER_REPORT)
    from scripts.eval.judge import _matches_any

    for path in ("tests/test_pricing.py", "hidden/test_totals_hidden.py", "conftest.py", "README.md"):
        assert _matches_any(path, config["readonly_paths"]), path
    assert _matches_any("src/report/pricing.py", config["writable_paths"])

def test_runner_places_hidden_tests_where_the_command_expects_them(tmp_path):
    """
    隐藏用例必须落在夹具声明的目录（通常是 hidden/）下，而不是被拍平进工作区根。

    这条守的是一个**代价很高的**缺陷：第一版运行器把 hidden 的**内容**拷到工作区根，
    于是 `pytest hidden/` 报 "directory not found"、collected 为空，
    8 次真实执行全部被判成「疑似针对可见用例硬编码」——而实际上 agent 每次都改对了。
    判据看的是最终测试结果，所以缺陷只体现在隐藏用例上，肉眼很难联想到运行器的拷贝路径。
    """
    def respond(messages, info):
        tool_returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return"]
        if not tool_returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="read_file", args=json.dumps({"path": "src/report/pricing.py"}), tool_call_id="c1",
            )])
        if len(tool_returns) == 1:
            source = str(tool_returns[0].content)
            assert "tax_cents(gross, tax_rate)" in source, "应当读到有 bug 的实现"
            fixed = source.replace("tax_cents(gross, tax_rate)", "tax_cents(net, tax_rate)")
            # read_file 的输出带行号前缀，去掉之后再写回
            body = "\n".join(line.split("\t", 1)[1] if "\t" in line else line for line in fixed.splitlines())
            return ModelResponse(parts=[ToolCallPart(
                tool_name="write_file",
                args=json.dumps({"path": "src/report/pricing.py", "content": body + "\n"}),
                tool_call_id="c2",
            )])
        if len(tool_returns) == 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="run_command", args=json.dumps({"command": "true", "verify": True}), tool_call_id="c3",
            )])
        return ModelResponse(parts=[TextPart("已按业务口径修正税基")])

    record_path = run_one(
        ORDER_REPORT, version="hidden-path", repeat=1, out_dir=tmp_path / "runs",
        model=FunctionModel(respond),
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))

    hidden = record["run"].get("hidden_tests", {})
    assert hidden.get("collected"), (
        f"隐藏用例没有被收集到——运行器把文件放错位置了。输出：\n{hidden.get('output_tail', '')[-600:]}"
    )
    assert hidden.get("failed") == 0, f"改对之后隐藏用例仍失败：\n{hidden.get('output_tail', '')[-600:]}"
    # 运行器故障不得被当成 agent 失败
    assert record["run"]["error"] is None
    # 隐藏用例跑完必须被清掉，否则会出现在改动清单里被当成越权改动
    assert "hidden" not in " ".join(record["run"]["changed_paths"])
    assert judge(record)["passed"] is True, judge(record)["reasons"]
