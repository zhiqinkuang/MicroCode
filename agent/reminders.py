"""
system-reminder / task-notification 正文构造。真正的注入在 agent/hooks.py 里挂 hook。
"""
from background_jobs import JobRegistry
from tasks_store import TasksStore

from .file_state import ReadFileState
from .iteration import MAX_EDIT_PATHS_SHOWN, IterationState


def _wrap(lines: list[str]) -> str:
    # 所有 reminder 正文都包在 <system-reminder>...</system-reminder> 标签里，集中一处避免分散重复
    return f"<system-reminder>\n{chr(10).join(lines)}\n</system-reminder>"


def build_reminder_text(state: ReadFileState) -> str | None:
    """
    根据当前会话状态拼出提醒正文；没什么值得提醒的就返回 None。
    """
    stale = state.stale_paths()
    if not stale:
        return None
    lines = [
        "以下文件在你读取之后被外部修改过，你 context 里的内容可能已过时，编辑前请重新用 read_file 读取：",
    ]
    lines += [f"- {path}" for path in stale]
    return _wrap(lines)


def build_task_reminder_text(store: TasksStore) -> str:
    """
    拼出 task 提醒的正文：一段温和的提醒 + 当前 task 列表。列表直接从 store 读，所以这条提醒同时承担了"把磁盘状态反向推回 prompt"的作用，模型不必主动 task_list。

    开头那句话同时被 hooks.py 用来扫历史识别"上一条已经是 reminder"，保持稳定不要改。
    """
    tasks = store.list()
    lines = [
        "task 工具最近没有被使用。如果你正在处理的工作适合用 task 跟踪进度，建议用 task_create 新建 task，用 task_update 维护状态（开工时切 in_progress，做完切 completed）；如果列表里有过时的 task，也可以顺手清理掉。只在与当前工作相关时再用这些工具。这只是一句友好的提醒——和当前工作无关的话忽略即可。",
    ]
    if tasks:
        lines.append("")
        lines.append("现存的 task 列表：")
        lines.append("")
        for t in tasks:
            lines.append(f"#{t.id}. [{t.status}] {t.subject}")
    return _wrap(lines)


def build_verify_reminder_text(state: IterationState, capped: bool = False) -> str | None:
    """
    拼出「改了文件但没验证」的强制要求正文，没有欠账就返回 None。
    它只负责拼字，不记账：介入次数由真正的拦停点 agent/hooks.py 的
    _enforce_verification 在拒收回合时累加，口径只有一处。
    """
    if not state.needs_verification():
        return None

    shown = state.edited_paths[:MAX_EDIT_PATHS_SHOWN]
    lines = [
        "系统检查：本轮你改动过文件，但没有取得一次通过的验证。",
        "",
        "已改动的文件：",
        *[f"- {path}" for path in shown],
    ]
    if len(state.edited_paths) > len(shown):
        lines.append(f"- 另有 {len(state.edited_paths) - len(shown)} 个文件未列出")
    lines.append("")

    last = state.last_verification()
    if last is None:
        lines.append("上一次验证：还没有跑过验证命令。")
    else:
        lines.append(f"上一次验证：{last.command}")
        lines.append(f"退出码：{last.exit_code}（未通过）")
        if last.output_tail:
            lines.append("输出尾部：")
            lines.append("```")
            lines.append(last.output_tail)
            lines.append("```")
    lines.append("")

    if capped:
        lines.append(
            f"系统已连续介入 {state.interventions} 次仍没拿到通过的验证，不再继续拦停。"
            "现在不要再说「已完成」：请直接向用户说明——改了哪些文件、验证命令是什么、"
            "失败输出的关键报错是什么、以及你判断修不动的原因；由用户决定下一步。"
        )
    else:
        lines.append(
            "请继续这一轮：先按上面的报错修改代码，然后用 run_command(verify=True) "
            "重新跑一次验证，直到它通过为止。如果这个项目确实没有可跑的验证命令，"
            "就如实说明你用了什么方式确认改动没问题。"
        )
    return _wrap(lines)


def build_job_reminder_text(registry: JobRegistry) -> str | None:
    """
    拼出后台 job 完成的通知正文，每条包在 <task-notification> 标签里。
    五个字段给足信息，模型拿到不用反问，直接决定下一步。
    """
    jobs = registry.pop_unnotified()
    if not jobs:
        return None
    blocks = []
    for job in jobs:
        # agent 型 job 会填 result（最终报告），直接随通知附上，主 agent 不必再读日志
        result = f"<result>{job.result}</result>\n" if job.result else ""
        blocks.append(
            "<task-notification>\n"
            f"<task-id>{job.id}</task-id>\n"
            f"<task-type>{job.kind}</task-type>\n"
            f"<output-file>{job.log_path}</output-file>\n"
            f"<status>{job.status}</status>\n"
            f"<summary>{job.summary()}，可用 read_file 读输出文件</summary>\n"
            f"{result}"
            "</task-notification>"
        )
    return "\n\n".join(blocks)
