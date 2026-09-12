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
    # shell 忽略 SIGTERM 时，收尾阶段使用强制终止回调。
    force_kill_func: Callable[[], None] | None = None
    # agent 型 job 的最终报告，shell 型 job 不用（输出都在日志文件里）
    result: str | None = None
    # agent 型 job 自己消耗的用量（pydantic-ai 的 RunUsage），由 run_subagent 填入。
    # 子代理在独立上下文里跑，它的用量不在主 agent 的 result.usage 里，
    # 不单独记下来就会从会话累计里整块消失（子代理恰恰是 token 大户）。
    usage: object | None = None
    # 该 job 的用量是否已并入会话累计：结算必须幂等，否则重复统计会把数字放大
    usage_settled: bool = False

    def summary(self) -> str:
        """
        通知模型时用的一句话总结。
        """
        desc = " ".join(self.description.split())
        if len(desc) > 60:
            desc = desc[:60] + "..."
        if self.kind == "agent":
            if self.status == "completed":
                return f"sub agent「{desc}」执行成功"
            if self.status == "failed":
                return f"sub agent「{desc}」执行失败"
            if self.status == "killed":
                return f"sub agent「{desc}」被终止"
            return f"sub agent「{desc}」运行中"
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


def _kill_process_tree(pid: int, signal_number: int = signal.SIGTERM) -> None:
    try:
        # 对整个进程组发终止信号：只杀 shell 本身的话，挂它底下的子进程会变孤儿继续跑
        os.killpg(os.getpgid(pid), signal_number)
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
            force_kill_func=lambda: _kill_process_tree(proc.pid, signal.SIGKILL) if proc.returncode is None else None,
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
        try:
            returncode = await proc.wait()
        finally:
            log_file.close()
        # 已被人为终止的 job 保持 killed 状态，不覆盖
        if job.status == "killed":
            return
        job.returncode = returncode
        job.status = "completed" if returncode == 0 else "failed"

    # ---------- agent job（sub agent） ----------

    async def spawn_agent(self, description: str, run: Callable[[Job], Awaitable]) -> Job:
        """
        把一个协程包装成 kind="agent" 的 job：注册表、日志、通知、/jobs 面板与 shell job 全部复用。

        和 spawn_shell 结构一致，区别只在「创建的东西」变了：shell job 起的是操作系统进程，
        agent job 起的是一个 asyncio 协程；kill_func 由终止进程树换成 task.cancel，
        其余代码一行不用改——这正是 Job 按 kill_func 回调抽象的好处。
        """
        job_id = _new_job_id("agent")
        log_path = self._jobs_dir / f"{job_id}.log"
        log_path.touch()
        job = Job(id=job_id, kind="agent", description=description, log_path=log_path)
        task = asyncio.create_task(run(job))
        # agent job 的终止就是取消协程
        job.kill_func = task.cancel
        self._jobs[job_id] = job
        # watcher 盯着协程结束，负责更新 job 状态
        watcher = asyncio.create_task(self._watch_agent(job, task))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return job

    async def _watch_agent(self, job: Job, task: asyncio.Task) -> None:
        """
        盯着 sub agent 协程结束，按结果把 job 标成 completed / failed / killed。
        """
        try:
            await task
            if job.status != "killed":
                job.status = "completed"
        except asyncio.CancelledError:
            job.status = "killed"
        except Exception as e:
            if job.status != "killed":
                job.status = "failed"
            with open(job.log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[job 出错] {type(e).__name__}: {e}\n")

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

    def settle_usage(self) -> list[Job]:
        """
        交出「已经结束、记了用量、但还没并入会话累计」的 job，并打上已结算标记。

        用独立的 settle_usage 而不是复用 pop_unnotified：后者是**通知**语义
        （取走即标记已通知），而结算时机与通知时机并不总是一致——例如本轮结束时
        就地结算，此时通知可能还没被取走。两件事混在一个标记上会漏计或重复计。
        """
        settled = [
            job for job in self._jobs.values()
            if job.usage is not None and not job.usage_settled and job.status != "running"
        ]
        for job in settled:
            job.usage_settled = True
        return settled

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

    async def aclose(self) -> int:
        """终止任务并等待其 finally 清理完成，再交还会话控制权。"""
        killed = self.shutdown()
        if self._watchers:
            watchers = tuple(self._watchers)
            _, pending = await asyncio.wait(watchers, timeout=2)
            if pending:
                for job in self._jobs.values():
                    if job.status == "killed" and job.force_kill_func:
                        job.force_kill_func()
                await asyncio.gather(*pending, return_exceptions=True)
        return killed
