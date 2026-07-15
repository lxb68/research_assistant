"""进程内领域树任务管理器，隔离耗时生成与 FastAPI 事件循环。"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.services.task_control import DomainTreeGenerationCancelled, raise_if_cancelled


ProgressCallback = Callable[[dict[str, Any]], None]
JobRunner = Callable[[ProgressCallback, threading.Event], dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class DomainTreeJob:
    """保存单个领域树任务的可观察状态。"""

    id: str
    project_id: str
    action: str
    status: str = "queued"
    stage: str = "queued"
    message: str = "任务已进入队列"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    partial_result: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        """返回不包含线程对象和模型密钥的公共状态。"""
        return {
            "jobId": self.id,
            "projectId": self.project_id,
            "action": self.action,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "progress": dict(self.progress),
            "partialResult": self.partial_result,
            "result": self.result,
            "error": self.error,
        }


class DomainTreeJobManager:
    """在线程池中执行领域树任务，并提供查询和协作式取消。"""

    def __init__(self, *, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="domain-tree")
        self._lock = threading.RLock()
        self._jobs: dict[str, DomainTreeJob] = {}
        self._active_by_project: dict[str, str] = {}

    def submit(self, project_id: str, action: str, runner: JobRunner) -> tuple[dict[str, Any], bool]:
        """提交任务；同一项目已有活动任务时直接返回该任务。"""
        with self._lock:
            active_id = self._active_by_project.get(project_id)
            if active_id:
                active = self._jobs.get(active_id)
                if active and active.status in {"queued", "running", "cancelling"}:
                    return active.public(), False

            job = DomainTreeJob(id=uuid.uuid4().hex, project_id=project_id, action=action)
            self._jobs[job.id] = job
            self._active_by_project[project_id] = job.id
            self._executor.submit(self._run, job.id, runner)
            return job.public(), True

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.public() if job else None

    def get_active(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            job_id = self._active_by_project.get(project_id)
            job = self._jobs.get(job_id or "")
            return job.public() if job else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """设置取消标记；当前同步 HTTP 调用返回后会在最近检查点退出。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.status in {"queued", "running"}:
                job.cancel_event.set()
                job.status = "cancelling"
                job.message = "正在取消，当前模型请求结束后停止"
            return job.public()

    def _run(self, job_id: str, runner: JobRunner) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.stage = "starting"
            job.message = "正在准备领域树生成"
            job.started_at = _now()

        def report(update: dict[str, Any]) -> None:
            with self._lock:
                current = self._jobs[job_id]
                if "stage" in update:
                    current.stage = str(update["stage"])
                if "message" in update:
                    current.message = str(update["message"])
                if isinstance(update.get("partialResult"), dict):
                    current.partial_result = update["partialResult"]
                current.progress.update(
                    {
                        key: value
                        for key, value in update.items()
                        if key not in {"stage", "message", "partialResult"}
                    }
                )

        try:
            raise_if_cancelled(job.cancel_event)
            result = runner(report, job.cancel_event)
            raise_if_cancelled(job.cancel_event)
            with self._lock:
                job.status = "completed"
                job.stage = "completed"
                job.message = "领域树和知识图谱已更新"
                job.result = result
        except DomainTreeGenerationCancelled:
            with self._lock:
                job.status = "cancelled"
                job.stage = "cancelled"
                job.message = (
                    "知识图谱构建已取消，已生成的领域树仍可使用"
                    if job.partial_result
                    else "任务已取消"
                )
        except Exception as error:
            with self._lock:
                job.status = "failed"
                job.stage = "failed"
                job.message = (
                    "知识图谱构建失败，已生成的领域树仍可使用"
                    if job.partial_result
                    else "领域树生成失败"
                )
                job.error = str(error)
        finally:
            with self._lock:
                job.finished_at = _now()
                if self._active_by_project.get(job.project_id) == job.id:
                    self._active_by_project.pop(job.project_id, None)


domain_tree_jobs = DomainTreeJobManager()


__all__ = ["DomainTreeJobManager", "domain_tree_jobs"]
