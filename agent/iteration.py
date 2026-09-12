"""
编辑—验证—纠错闭环的会话级状态：记录本轮改过哪些文件、最近一次验证命令的结果。

它只在进程内、只在当前会话有效，明确不参与持久化：
- 每轮用户输入开始时 clear()，所以「本轮」的语义是「这一次输入触发的这一轮 run」；
- 每轮 run 内部跨多轮模型调用累积，闭环判定正需要这个粒度；
- 不写进会话 jsonl——闭环是运行时概念，重放历史时不该复活「未验证」的判定；
- /new、/resume 切换到别的会话时重建，旧会话的未验证状态不跟过来。

闭环的两个判定信号由工具层写入：file 工具 mark_edit、shell 工具 run_verification；
真正的强制介入在 agent/hooks.py 的 _build_verify_reminder（走 system-reminder 注入）。
"""
from dataclasses import dataclass, field

MAX_OUTPUT_TAIL = 2000
MAX_EDIT_PATHS_SHOWN = 20

# 系统最多强制介入几轮（agent/hooks.py 的 builder 与这里共用同一个常量，口径只有一处）
MAX_INTERVENTIONS = 3


@dataclass
class VerificationRun:
    """模型声明为验证的一次命令执行及其结果。"""

    command: str
    exit_code: int | None
    output_tail: str
    passed: bool


@dataclass
class IterationState:
    """
    一轮 run 内的编辑与验证记录。校验都由系统记账，不依赖模型自觉上报。
    """

    # 本轮被 edit_file / write_file 改过的文件，按首次改动顺序保留
    edited_paths: list[str] = field(default_factory=list)
    # 已结束的验证命令，按先后顺序；判定只看最后一次（模型可能先后跑了几个验证命令）
    verification_runs: list[VerificationRun] = field(default_factory=list)
    # 系统已经强制介入过几轮，达到 MAX_INTERVENTIONS 后不再介入、只要求交代
    interventions: int = 0

    def _remember_path(self, path: str) -> None:
        """登记一个被改动的路径，重复改动只记一次、保持首次顺序，避免提醒刷屏。"""
        if path and path not in self.edited_paths:
            self.edited_paths.append(path)

    def mark_edit(self, path: str) -> None:
        # file 工具写盘成功后调用：写盘失败不该记成「改过」，所以调用点在写盘之后
        self._remember_path(path)

    def mark_edited_files(self, paths) -> None:
        """批量登记（subagent 等没有工具级钩子的场景可用）。"""
        for path in paths:
            self._remember_path(path)

    def mark_verification(self, command: str, exit_code: int | None, output: str) -> None:
        """
        记录一次验证结果。exit_code 为 None 表示命令被人为终止（kill 或转后台），
        这时没有可用结论，passed 记为 False——拿不到证据就不算验证通过。
        """
        self.verification_runs.append(
            VerificationRun(
                command=command,
                exit_code=exit_code,
                output_tail=_tail(output),
                passed=exit_code == 0,
            )
        )

    def last_verification(self) -> VerificationRun | None:
        return self.verification_runs[-1] if self.verification_runs else None

    def needs_verification(self) -> bool:
        """
        判定「这一轮改了东西但还没拿到通过的验证」。
        只看最后一次验证是不是通过：先失败后通过是闭环的正常路径，后通过就不再追。
        """
        if not self.edited_paths:
            return False
        last = self.last_verification()
        return last is None or not last.passed

    def budget_exhausted(self) -> bool:
        """强制介入预算已用光：不再继续拦停，让它收尾（最后一次拦停已要求它向用户交代）。"""
        return self.interventions >= MAX_INTERVENTIONS

    def clear(self) -> None:
        """
        每轮用户输入开始时重置。介入计数一并归零：这是新的一轮用户意图，
        上一轮的欠账不该继续压在这一轮头上（未验证的文件仍会由下一条提醒重新识别）。
        """
        self.edited_paths.clear()
        self.verification_runs.clear()
        self.interventions = 0


def _tail(output: str, limit: int = MAX_OUTPUT_TAIL) -> str:
    """
    只留输出尾部：报错和失败汇总几乎总在末尾，头部往往是无关的进度噪音。
    超长时显式标注被截断的长度，模型才知道自己看的不是全文。
    """
    text = output or ""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"...（前面省略 {omitted} 个字符）\n" + text[-limit:]
