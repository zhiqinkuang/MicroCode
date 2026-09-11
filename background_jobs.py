"""
后台任务注册表：起任务、盯状态、终止任务。

每个会话一份 JobRegistry，管理本会话起过的所有 job（shell 命令、subagent……）。
job 的输出统一落盘到 ~/.my-claude-code/jobs/<session_id>/<id>.log：
- read_file 工具直接可读，不用为「查看 job 输出」单造一个工具
- 边跑边写，每读一次拿到的都是最新进度
- 天然持久，命令结束甚至程序退出后输出都还在

注册表按 job 抽象而不是按进程抽象：Job 有 kind 字段，终止动作是可注入的回调，
新增长时间任务类型（如 subagent）时，注册表、日志、通知、/jobs 面板全部直接复用。
"""
from __future__ import annotations

import asyncio
import os
import random
import signal
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal

# 落盘状态：运行中 / 成功 / 失败 / 被终止
JobStatus = Literal["running", "completed", "failed", "killed"]


@dataclass
class Job:
    id: str
    # job 类型："shell" 是后台命令，"agent" 是 subagent；id 前缀随之区分
    kind: str
    # 一句话描述：shell job 是命令本身，agent job 是任务描述
    description: str
    # job 的日志输出存储到文件
    log_path: Path
    status: JobStatus = "running"
    returncode: int | None = None
    # 是否已经通知过模型，防止重复通知
    notified: bool = False
    # False 表示前台任务，用户可以按 ctrl+b 把它转化为后台任务
    background: bool = True
    # 终止这个 job 用的回调函数，由创建方注入（shell 杀进程组，agent cancel 协程）
    kill_func: Callable[[], None] | None = None

    def summary(self) -> str:
        """
        通知模型时用的一句话总结。
        """
        desc = " ".join(self.description.split())
        if len(desc) > 60:
            desc = desc[:60] + "..."
        if self.kind == "agent":
            return f"subagent {self.status}：{desc}"
        if self.status == "completed":
            return f"命令执行成功：{desc}"
        if self.status == "failed":
            return f"命令失败（exit {self.returncode}）：{desc}"
        if self.status == "killed":
            return f"命令被终止：{desc}"
        return f"命令运行中：{desc}"


# id 用小写字母数字组合：日志文件按 id 命名，用户敲 /jobs、模型拼 job id 都方便
_ID_ALPHABET = string.digits + string.ascii_lowercase

# id 前缀标 job 类型：b 取 bash 首字母，a 取 agent 首字母，光看 id 就能区分
_JOB_ID_PREFIXES = {"shell": "b", "agent": "a"}


def _new_job_id(kind: str) -> str:
    # 随机字符而不是 1、2、3 自增计数：自增是内存变量，重启程序后会从头数起，
    # 新 job 的日志就可能覆盖上次运行留下的同名旧日志，随机 id 天然没这个隐患
    return _JOB_ID_PREFIXES.get(kind, "j") + "".join(random.choices(_ID_ALPHABET, k=8))


def _kill_process_tree(pid: int) -> None:
    try:
        # 对整个进程组发终止信号：只杀 shell 本身的话，挂它底下的子进程会变孤儿继续跑
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # 进程已退出 / 权限不足：都视作已终止，不报错
        pass


class JobRegistry:
    """
    按 session_id 隔离的 job 注册表。对外暴露 spawn_shell / spawn_agent 起任务，
    kill 终止，pop_unnotified 取走「已结束但还没通知模型」的 job。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._jobs_dir = Path.home() / ".my-claude-code" / "jobs" / session_id
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._watchers: set[asyncio.Task] = set()

    # ---------- shell job ----------

    async def spawn_shell(self, command: str, background: bool = True) -> Job:
        """
        起一个独立进程组的 shell 命令，输出重定向到日志文件，创建后立即返回不等它结束。
        """
        job_id = _new_job_id("shell")
        log_path = self._jobs_dir / f"{job_id}.log"
        log_file = open(log_path, "wb")
        proc = await asyncio.create_subprocess_shell(
            # stdout/stderr 合并写进日志文件持久化，方便模型和用户随时查看
            command, stdout=log_file, stderr=subprocess.STDOUT,
            # 独立进程组：npm run dev 这类会派生子进程的命令，终止时整组一并退出
            start_new_session=True,
        )
        job = Job(
            id=job_id, kind="shell", description=command,
            log_path=log_path, background=background,
            kill_func=lambda: _kill_process_tree(proc.pid),
        )
        self._jobs[job_id] = job
        # watcher 协程盯着进程结束，负责更新 job 状态
        watcher = asyncio.create_task(self._watch(job, proc, log_file))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return job

    async def _watch(self, job: Job, proc: asyncio.subprocess.Process, log_file) -> None:
        """
        等进程退出，按 exit code 把 job 标成 completed 或 failed。
        """
        returncode = await proc.wait()
        log_file.close()
        # 已被人为终止的 job 保持 killed 状态，不覆盖
        if job.status == "killed":
            return
        job.returncode = returncode
        job.status = "completed" if returncode == 0 else "failed"

    # ---------- agent job（subagent） ----------

    def spawn_agent(
        self,
        description: str,
        make_run: Callable[[Path], Awaitable],
        background: bool = True,
    ) -> Job:
        """
        把一个协程包装成 kind="agent" 的 job：注册表、日志、通知、/jobs 面板与 shell job 全部复用。

        make_run 接收日志路径、返回待跑的协程——日志路径由注册表按 id 分配，
        所以协程只能在拿到 job id 之后构造。终止用 asyncio.Task.cancel。
        """
        job_id = _new_job_id("agent")
        log_path = self._jobs_dir / f"{job_id}.log"
        job = Job(
            id=job_id, kind="agent", description=description,
            log_path=log_path, background=background,
        )
        self._jobs[job_id] = job

        async def _runner() -> None:
            try:
                await make_run(log_path)
                job.status = "completed"
            except asyncio.CancelledError:
                job.status = "killed"
            except Exception as e:
                job.status = "failed"
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n[job 出错] {type(e).__name__}: {e}\n")

        task = asyncio.create_task(_runner())
        job.kill_func = task.cancel
        return job

    # ---------- 通用 API ----------

    def list(self) -> list[Job]:
        # 按创建顺序返回本会话所有 job（/jobs 命令用）
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def kill(self, job_id: str) -> bool:
        """
        终止一个还在运行的 job。job 不存在或已结束返回 False。
        """
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        if job.kill_func:
            job.kill_func()
        job.status = "killed"
        return True

    def running(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status == "running"]

    def to_background(self) -> list[Job]:
        """
        把所有运行中的前台 job 转成后台（ctrl+b 用）。
        run_command / run_subagent 的前台等待循环发现 background 标志变了，
        就会退出等待并告诉模型「任务转后台了」。
        """
        moved = [j for j in self.running() if not j.background]
        for job in moved:
            job.background = True
        return moved

    def pop_unnotified(self) -> list[Job]:
        """
        取出所有已结束但还没通知模型的后台 job，取的同时标记为已通知。
        """
        done = [j for j in self._jobs.values()
                if j.background and j.status != "running" and not j.notified]
        for job in done:
            job.notified = True
        return done

    def shutdown(self) -> int:
        """
        会话收尾（/new、/resume、/exit）：终止所有还在跑的 job，避免进程/协程泄露。返回终止数。
        """
        running = self.running()
        for job in running:
            self.kill(job.id)
        return len(running)
