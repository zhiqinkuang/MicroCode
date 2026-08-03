"""
Agent 复合依赖：现在 agent 同时需要 ReadFileState（给文件工具用）和 TasksStore（给 task 工具用），把它们包成一个对象，统一通过 RunContext 注入。
"""
from dataclasses import dataclass

from tasks_store import TasksStore

from .file_state import ReadFileState


@dataclass
class AgentDeps:
    read_file_state: ReadFileState
    tasks_store: TasksStore
