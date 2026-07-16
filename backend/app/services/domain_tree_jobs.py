"""SQLite-backed lifecycle manager for domain-tree background jobs."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from app.core.config import settings
from app.services.domain_tree_store import DomainTreeStore
from app.services.task_control import DomainTreeGenerationCancelled, raise_if_cancelled


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class DomainTreeJobOutcome:
    """A pointer to an already persisted domain-tree result."""

    result_path: str


JobRunner = Callable[[ProgressCallback, threading.Event], DomainTreeJobOutcome]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat()


def _parse_progress(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class DomainTreeJobRepository:
    """Owns all SQLite transactions for domain-tree job state."""

    def __init__(self, path: str | Path, *, ttl_hours: int, max_history: int) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=max(1, ttl_hours))
        self.max_history = max(1, max_history)
        self.ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS domain_tree_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    progress_json TEXT NOT NULL,
                    result_path TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT,
                    expires_at TEXT NOT NULL
                )
                """,
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_domain_tree_jobs_project_status "
                "ON domain_tree_jobs(project_id, status)",
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_domain_tree_jobs_expires ON domain_tree_jobs(expires_at)",
            )

    def create_or_get_active(self, project_id: str, action: str, *, stale_before: str) -> tuple[sqlite3.Row, bool]:
        now = _utcnow()
        with self.connect() as connection:
            # Serialize the check-and-insert across processes.
            connection.execute("BEGIN IMMEDIATE")
            self._interrupt_stale_in_connection(connection, stale_before=stale_before, now=_timestamp(now))
            active = connection.execute(
                """
                SELECT * FROM domain_tree_jobs
                WHERE project_id = ? AND status IN ('queued', 'running', 'cancelling')
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if active:
                connection.commit()
                return active, False

            job_id = uuid.uuid4().hex
            created_at = _timestamp(now)
            connection.execute(
                """
                INSERT INTO domain_tree_jobs (
                    id, project_id, action, status, stage, message, progress_json,
                    result_path, error, cancel_requested, created_at, started_at,
                    heartbeat_at, finished_at, expires_at
                ) VALUES (?, ?, ?, 'queued', 'queued', ?, '{}', NULL, '', 0, ?, NULL, ?, NULL, ?)
                """,
                (
                    job_id,
                    project_id,
                    action,
                    "任务已进入队列",
                    created_at,
                    created_at,
                    _timestamp(now + self.ttl),
                ),
            )
            row = connection.execute("SELECT * FROM domain_tree_jobs WHERE id = ?", (job_id,)).fetchone()
            connection.commit()
            assert row is not None
            return row, True

    def get(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM domain_tree_jobs WHERE id = ?", (job_id,)).fetchone()

    def get_active(self, project_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM domain_tree_jobs
                WHERE project_id = ? AND status IN ('queued', 'running', 'cancelling')
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()

    def mark_running(self, job_id: str) -> None:
        now = _timestamp()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE domain_tree_jobs
                SET status = 'running', stage = 'starting', message = ?,
                    started_at = COALESCE(started_at, ?), heartbeat_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                ("正在准备领域树生成", now, now, job_id),
            )

    def update_progress(self, job_id: str, update: dict[str, Any]) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT progress_json FROM domain_tree_jobs WHERE id = ? AND status IN ('queued', 'running', 'cancelling')",
                (job_id,),
            ).fetchone()
            if not row:
                return
            progress = _parse_progress(row["progress_json"])
            progress.update(
                {key: value for key, value in update.items() if key not in {"stage", "message", "partialResult"}}
            )
            connection.execute(
                """
                UPDATE domain_tree_jobs
                SET stage = COALESCE(?, stage), message = COALESCE(?, message),
                    progress_json = ?, heartbeat_at = ?
                WHERE id = ? AND status IN ('queued', 'running', 'cancelling')
                """,
                (
                    str(update["stage"]) if "stage" in update else None,
                    str(update["message"]) if "message" in update else None,
                    json.dumps(progress, ensure_ascii=False, separators=(",", ":")),
                    _timestamp(),
                    job_id,
                ),
            )

    def heartbeat(self, job_id: str) -> bool:
        """Refresh a heartbeat and report whether another process requested cancellation."""
        with self.connect() as connection:
            connection.execute(
                "UPDATE domain_tree_jobs SET heartbeat_at = ? WHERE id = ? AND status IN ('queued', 'running', 'cancelling')",
                (_timestamp(), job_id),
            )
            row = connection.execute(
                "SELECT cancel_requested FROM domain_tree_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancel(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM domain_tree_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                connection.commit()
                return None
            if row["status"] in {"queued", "running"}:
                connection.execute(
                    """
                    UPDATE domain_tree_jobs
                    SET status = 'cancelling', cancel_requested = 1,
                        message = '正在取消，当前模型请求结束后停止', heartbeat_at = ?
                    WHERE id = ?
                    """,
                    (_timestamp(), job_id),
                )
            row = connection.execute("SELECT * FROM domain_tree_jobs WHERE id = ?", (job_id,)).fetchone()
            connection.commit()
            return row

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        message: str,
        result_path: str | None = None,
        error: str = "",
    ) -> None:
        now = _utcnow()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE domain_tree_jobs
                SET status = ?, stage = ?, message = ?, result_path = ?, error = ?,
                    heartbeat_at = ?, finished_at = ?, expires_at = ?
                WHERE id = ? AND status IN ('queued', 'running', 'cancelling')
                """,
                (
                    status,
                    stage,
                    message,
                    result_path,
                    error,
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now + self.ttl),
                    job_id,
                ),
            )

    def interrupt_stale(self, *, stale_before: str) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = self._interrupt_stale_in_connection(connection, stale_before=stale_before, now=_timestamp())
            connection.commit()
            return count

    def interrupt_jobs(self, job_ids: list[str]) -> int:
        if not job_ids:
            return 0
        placeholders = ", ".join("?" for _ in job_ids)
        now = _utcnow()
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE domain_tree_jobs
                SET status = 'interrupted', stage = 'interrupted',
                    message = '服务停止，任务已中断', error = 'service stopped while job was active',
                    finished_at = ?, heartbeat_at = ?, expires_at = ?
                WHERE id IN ({placeholders}) AND status IN ('queued', 'running', 'cancelling')
                """,
                (_timestamp(now), _timestamp(now), _timestamp(now + self.ttl), *job_ids),
            )
            return cursor.rowcount

    def cleanup(self, *, now: str | None = None) -> int:
        deleted = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM domain_tree_jobs WHERE expires_at <= ? AND status NOT IN ('queued', 'running', 'cancelling')",
                (now or _timestamp(),),
            )
            deleted += cursor.rowcount
            overflow = connection.execute(
                """
                SELECT id FROM domain_tree_jobs
                WHERE status NOT IN ('queued', 'running', 'cancelling')
                ORDER BY COALESCE(finished_at, created_at) DESC
                LIMIT -1 OFFSET ?
                """,
                (self.max_history,),
            ).fetchall()
            if overflow:
                placeholders = ", ".join("?" for _ in overflow)
                cursor = connection.execute(
                    f"DELETE FROM domain_tree_jobs WHERE id IN ({placeholders})",
                    tuple(row["id"] for row in overflow),
                )
                deleted += cursor.rowcount
            connection.commit()
        return deleted

    def count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM domain_tree_jobs").fetchone()[0])

    def _interrupt_stale_in_connection(self, connection: sqlite3.Connection, *, stale_before: str, now: str) -> int:
        expires_at = _timestamp(datetime.fromisoformat(now) + self.ttl)
        cursor = connection.execute(
            """
            UPDATE domain_tree_jobs
            SET status = 'interrupted', stage = 'interrupted',
                message = '任务心跳已过期，已在服务恢复时标记为中断',
                error = 'job heartbeat expired', finished_at = ?, expires_at = ?
            WHERE status IN ('queued', 'running', 'cancelling')
              AND COALESCE(heartbeat_at, started_at, created_at) < ?
            """,
            (now, expires_at, stale_before),
        )
        return cursor.rowcount


@dataclass(slots=True)
class _ActiveJob:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    partial_result: dict[str, Any] | None = None


class DomainTreeJobManager:
    """Runs jobs in threads while SQLite remains the source of truth."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        max_workers: int = 2,
        ttl_hours: int = 168,
        stale_seconds: int = 300,
        cleanup_interval_seconds: int = 3600,
        max_history: int = 1000,
    ) -> None:
        self.repository = DomainTreeJobRepository(
            db_path or settings.domain_tree_job_db,
            ttl_hours=ttl_hours,
            max_history=max_history,
        )
        self.stale_seconds = max(1, stale_seconds)
        self.cleanup_interval_seconds = max(1, cleanup_interval_seconds)
        self.heartbeat_interval_seconds = max(1.0, min(30.0, self.stale_seconds / 3))
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="domain-tree")
        self._lock = threading.RLock()
        self._active: dict[str, _ActiveJob] = {}
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    def start(self) -> None:
        interrupted = self.recover_stale_jobs()
        deleted = self.cleanup()
        if interrupted or deleted:
            logger.info("领域树任务启动维护：interrupted=%s deleted=%s", interrupted, deleted)
        with self._lock:
            if self._cleanup_thread and self._cleanup_thread.is_alive():
                return
            self._stop_event.clear()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="domain-tree-job-cleanup",
                daemon=True,
            )
            self._cleanup_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            active_ids = list(self._active)
            for active in self._active.values():
                active.cancel_event.set()
        self.repository.interrupt_jobs(active_ids)
        self._executor.shutdown(wait=False, cancel_futures=True)
        thread = self._cleanup_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def submit(self, project_id: str, action: str, runner: JobRunner) -> tuple[dict[str, Any], bool]:
        stale_before = _timestamp(_utcnow() - timedelta(seconds=self.stale_seconds))
        row, created = self.repository.create_or_get_active(project_id, action, stale_before=stale_before)
        if not created:
            return self._public(row), False

        active = _ActiveJob()
        with self._lock:
            self._active[row["id"]] = active
        try:
            self._executor.submit(self._run, row["id"], runner)
        except RuntimeError:
            with self._lock:
                self._active.pop(row["id"], None)
            self.repository.finish(
                row["id"], status="interrupted", stage="interrupted",
                message="任务执行器不可用", error="job executor is shut down",
            )
            raise
        return self._public(row, active=active), True

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.repository.get(job_id)
        if not row:
            return None
        with self._lock:
            active = self._active.get(job_id)
        return self._public(row, active=active)

    def get_active(self, project_id: str) -> dict[str, Any] | None:
        row = self.repository.get_active(project_id)
        if not row:
            return None
        with self._lock:
            active = self._active.get(row["id"])
        return self._public(row, active=active)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        row = self.repository.request_cancel(job_id)
        if not row:
            return None
        with self._lock:
            active = self._active.get(job_id)
            if active:
                active.cancel_event.set()
        return self._public(row, active=active)

    def recover_stale_jobs(self) -> int:
        stale_before = _timestamp(_utcnow() - timedelta(seconds=self.stale_seconds))
        return self.repository.interrupt_stale(stale_before=stale_before)

    def cleanup(self) -> int:
        return self.repository.cleanup()

    @property
    def active_cache_size(self) -> int:
        with self._lock:
            return len(self._active)

    def _run(self, job_id: str, runner: JobRunner) -> None:
        with self._lock:
            active = self._active[job_id]
        self.repository.mark_running(job_id)
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, active.cancel_event, heartbeat_stop),
            name=f"domain-tree-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()

        def report(update: dict[str, Any]) -> None:
            partial = update.get("partialResult")
            if isinstance(partial, dict):
                with self._lock:
                    current = self._active.get(job_id)
                    if current:
                        current.partial_result = partial
            self.repository.update_progress(job_id, update)
            if self.repository.heartbeat(job_id):
                active.cancel_event.set()

        try:
            if self.repository.heartbeat(job_id):
                active.cancel_event.set()
            raise_if_cancelled(active.cancel_event)
            outcome = runner(report, active.cancel_event)
            raise_if_cancelled(active.cancel_event)
            if not isinstance(outcome, DomainTreeJobOutcome):
                raise TypeError("domain-tree job runner must return DomainTreeJobOutcome")
            self.repository.finish(
                job_id, status="completed", stage="completed",
                message="领域树和知识图谱已更新", result_path=outcome.result_path,
            )
        except DomainTreeGenerationCancelled:
            self.repository.finish(
                job_id, status="cancelled", stage="cancelled",
                message="知识图谱构建已取消，已生成的领域树仍可使用" if active.partial_result else "任务已取消",
            )
        except Exception as error:
            logger.exception("领域树后台任务失败：job_id=%s", job_id)
            self.repository.finish(
                job_id, status="failed", stage="failed",
                message="知识图谱构建失败，已生成的领域树仍可使用" if active.partial_result else "领域树生成失败",
                error=str(error),
            )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            with self._lock:
                self._active.pop(job_id, None)

    def _heartbeat_loop(self, job_id: str, cancel_event: threading.Event, stop_event: threading.Event) -> None:
        while not stop_event.wait(self.heartbeat_interval_seconds):
            try:
                if self.repository.heartbeat(job_id):
                    cancel_event.set()
            except sqlite3.Error:
                logger.exception("更新领域树任务心跳失败：job_id=%s", job_id)

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self.cleanup_interval_seconds):
            try:
                interrupted = self.recover_stale_jobs()
                deleted = self.cleanup()
                if interrupted or deleted:
                    logger.info("领域树任务定时维护：interrupted=%s deleted=%s", interrupted, deleted)
            except sqlite3.Error:
                logger.exception("领域树任务定时清理失败")

    def _public(self, row: sqlite3.Row, *, active: _ActiveJob | None = None) -> dict[str, Any]:
        result = None
        result_path = str(row["result_path"] or "")
        if row["status"] == "completed" and result_path:
            path = Path(result_path)
            result = DomainTreeStore().load_result(path.parent, str(row["project_id"]))
        return {
            "jobId": row["id"], "projectId": row["project_id"], "action": row["action"],
            "status": row["status"], "stage": row["stage"], "message": row["message"],
            "createdAt": row["created_at"], "startedAt": row["started_at"],
            "heartbeatAt": row["heartbeat_at"], "finishedAt": row["finished_at"],
            "expiresAt": row["expires_at"], "progress": _parse_progress(row["progress_json"]),
            "partialResult": active.partial_result if active else None, "result": result,
            "resultPath": result_path or None, "error": row["error"] or "",
            "cancelRequested": bool(row["cancel_requested"]),
        }


domain_tree_jobs = DomainTreeJobManager(
    max_workers=settings.domain_tree_job_max_workers,
    ttl_hours=settings.domain_tree_job_ttl_hours,
    stale_seconds=settings.domain_tree_job_stale_seconds,
    cleanup_interval_seconds=settings.domain_tree_job_cleanup_interval_seconds,
    max_history=settings.domain_tree_job_max_history,
)


__all__ = ["DomainTreeJobManager", "DomainTreeJobOutcome", "DomainTreeJobRepository", "domain_tree_jobs"]
