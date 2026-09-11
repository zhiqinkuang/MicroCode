"""
Agent 复合依赖：把各工具需要的状态打包成一个对象，统一通过 RunContext 注入。
"""
from dataclasses import dataclass

from background_jobs import JobRegistry
from tasks_store import TasksStore

from .file_state import ReadFileState
from file_history import FileHistory

@dataclass
class AgentDeps:
    read_file_state: ReadFileState
    tasks_store: TasksStore
    file_history: FileHistory | None = None
    # 后台任务注册表：run_command / run_subagent / job_kill 共用；subagent 也拿同一份，能起自己的后台任务
    job_registry: JobRegistry | None = None
