"""
system-reminder 正文构造。真正的注入在 agent/hooks.py 里挂 hook。
"""
from .file_state import ReadFileState

#添加系统提示提示是否进行了修改
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
    body = "\n".join(lines)
    return f"<system-reminder>\n{body}\n</system-reminder>"
