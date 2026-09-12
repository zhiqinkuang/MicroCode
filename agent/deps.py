"""
Agent 复合依赖：把各工具需要的状态打包成一个对象，统一通过 RunContext 注入。
"""
from dataclasses import dataclass

from background_jobs import Job, JobRegistry
from tasks_store import TasksStore

from .file_state import ReadFileState
from .iteration import IterationState
from file_history import FileHistory

@dataclass
class AgentDeps:
    read_file_state: ReadFileState
    tasks_store: TasksStore | None
    file_history: FileHistory | None = None
    # 后台任务注册表：run_command / run_agent / job_kill 共用
    job_registry: JobRegistry | None = None
    # sub agent 运行时指向它自己对应的 job：审批冒泡时要告诉用户是哪个 sub agent 在请求
    subagent_job: Job | None = None
    # 编辑—验证—纠错闭环状态：file 工具标记改动、shell 工具记录验证结果、hooks 判定是否强制介入。
    # 刻意放在字段表最后：新字段不插队，既有的位置参数构造方（如子代理测试）不受影响
    iteration: IterationState | None = None
