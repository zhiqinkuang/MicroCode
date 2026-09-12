"""
工具子包：按职责拆成 file（读写文件）/ shell（执行命令）/ ask_user（向用户提问）/ agents（委托 sub agent）。

外部代码只需要：
- TOOLS: 注册给 Agent 的工具列表
- read_and_register: mentions.py 复用，把 @ 引用伪装成 read_file 调用塞进对话历史
"""
import os

from pydantic_ai import Tool

# 显式触发 shell.py 顶层 permissions.register_self_check 副作用：必须在 import 时就把
# run_command 的高危特征自检挂上去，否则后续 default 模式下的危险命令直接放行
from . import shell as _shell  # noqa: F401
from .agents import run_agent
from .ask_user import ask_user_question
from .file import read_and_register, read_file, edit_file, write_file
from .shell import job_kill, run_command
from .task import task_create, task_get, task_list, task_update
from skills import load_skill

# 消融实验开关：关掉后主 agent 拿不到 run_agent，子任务只能自己做。
# 只在 eval 里用（见 scripts/eval/run_task.py），默认行为一个字不变。
# 位置放在所有 import 之后：E402 会因为「import 之前有代码」而报错，常量定义也算代码。
SUBAGENTS_DISABLED = os.environ.get("CODING_AGENT_DISABLE_SUBAGENTS", "").strip().lower() not in ("", "0", "false")

# edit_file 和 write_file 标记 sequential=True：同一轮里的多个改文件调用必须串行执行，
# 否则它们会基于同一份旧快照并发写盘、互相覆盖（这正是 readFileState + mtime 想防住的并发问题）
TOOLS = [
    read_file,
    load_skill,
    Tool(edit_file, sequential=True),
    Tool(write_file, sequential=True),
    run_command,
    job_kill,
    # 消融实验关闭时把 run_agent 摘掉：没有派发能力，等价于「没有 SubAgent 隔离」
    *([] if SUBAGENTS_DISABLED else [run_agent]),
    ask_user_question,
    task_create,
    task_list,
    task_get,
    task_update,
]

__all__ = ["TOOLS", "read_and_register"]
