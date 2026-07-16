"""统一后台任务的持久化生命周期、事件和受限线程池。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from app.core.config import settings
from app.services.task_control import TaskCancelled, raise_if_task_cancelled


logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"queued", "running", "cancelling"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
JobHandler = Callable[["BackgroundJobContext", dict[str, Any]], dict[str, Any] | None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


class BackgroundJobCapacityExceeded(RuntimeError):
    """运行与等待队列都已达到上限。"""


class BackgroundJobRepository:
    """集中管理后台任务和事件的 SQLite 事务。"""

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_hours: int,
        max_history: int,
        max_events_per_job: int,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=max(1, ttl_hours))
        self.max_history = max(1, max_history)
        self.max_events_per_job = max(10, max_events_per_job)
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS background_jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    session_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    dedupe_key TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    result_payload TEXT,
                    error TEXT,
                    retry_of TEXT,
                    retryable INTEGER NOT NULL DEFAULT 1,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_background_jobs_session_created
                    ON background_jobs(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_background_jobs_status_heartbeat
                    ON background_jobs(status, heartbeat_at);
                CREATE INDEX IF NOT EXISTS idx_background_jobs_dedupe
                    ON background_jobs(type, dedupe_key, status);
                CREATE TABLE IF NOT EXISTS background_job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_background_job_events_cursor
                    ON background_job_events(job_id, sequence);
                """,
            )

    def create(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        session_id: str,
        user_id: str | None,
        dedupe_key: str | None,
        retry_of: str | None = None,
        retryable: bool = True,
    ) -> tuple[sqlite3.Row, bool]:
        now = _utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if dedupe_key:
                active = connection.execute(
                    """
                    SELECT * FROM background_jobs
                    WHERE type = ? AND dedupe_key = ?
                      AND status IN ('queued', 'running', 'cancelling')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (job_type, dedupe_key),
                ).fetchone()
                if active:
                    return active, False
            job_id = uuid.uuid4().hex
            created_at = _timestamp(now)
            connection.execute(
                """
                INSERT INTO background_jobs (
                    id, user_id, session_id, type, dedupe_key, status, stage,
                    progress, message, request_payload, result_payload, error,
                    retry_of, retryable, cancel_requested, created_at, started_at,
                    finished_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, NULL, '',
                          ?, ?, 0, ?, NULL, NULL, ?, ?)
                """,
                (
                    job_id,
                    user_id,
                    session_id,
                    job_type,
                    dedupe_key,
                    "任务已进入队列",
                    _dumps(payload),
                    retry_of,
                    int(retryable),
                    created_at,
                    created_at,
                    _timestamp(now + self.ttl),
                ),
            )
            self._append_event(connection, job_id, "status", {"status": "queued", "message": "任务已进入队列"})
            row = connection.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
            return row, True

    def get(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()

    def list(self, *, session_id: str, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM background_jobs WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, max(1, min(limit, 500))),
            ).fetchall()

    def list_queued(self, limit: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM background_jobs WHERE status = 'queued' ORDER BY created_at LIMIT ?",
                (max(1, limit),),
            ).fetchall()

    def find_active(self, job_type: str, dedupe_key: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM background_jobs
                WHERE type = ? AND dedupe_key = ?
                  AND status IN ('queued', 'running', 'cancelling')
                ORDER BY created_at DESC LIMIT 1
                """,
                (job_type, dedupe_key),
            ).fetchone()

    def mark_running(self, job_id: str) -> bool:
        now = _timestamp()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE background_jobs
                SET status = 'running', stage = 'running', message = ?,
                    started_at = COALESCE(started_at, ?), heartbeat_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                ("任务正在后台执行", now, now, job_id),
            )
            if cursor.rowcount:
                self._append_event(connection, job_id, "status", {"status": "running", "message": "任务正在后台执行"})
            return bool(cursor.rowcount)

    def heartbeat(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM background_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return True
            connection.execute(
                "UPDATE background_jobs SET heartbeat_at = ? WHERE id = ? AND status IN ('running', 'cancelling')",
                (_timestamp(), job_id),
            )
            return bool(row["cancel_requested"])

    def update_progress(
        self,
        job_id: str,
        *,
        progress: int | None = None,
        stage: str | None = None,
        message: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        assignments: list[str] = ["heartbeat_at = ?"]
        values: list[Any] = [_timestamp()]
        if progress is not None:
            assignments.append("progress = ?")
            values.append(max(0, min(int(progress), 100)))
        if stage:
            assignments.append("stage = ?")
            values.append(stage)
        if message is not None:
            assignments.append("message = ?")
            values.append(message)
        values.append(job_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE background_jobs SET {', '.join(assignments)} WHERE id = ? AND status IN ('running', 'cancelling')",
                values,
            )
            payload = dict(event_payload or {})
            if progress is not None:
                payload.setdefault("progress", max(0, min(int(progress), 100)))
            if stage:
                payload.setdefault("stage", stage)
            if message is not None:
                payload.setdefault("message", message)
            self._append_event(connection, job_id, "progress", payload)

    def append_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            self._append_event(connection, job_id, event_type, payload)

    def _append_event(self, connection: sqlite3.Connection, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO background_job_events (job_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, _dumps(payload), _timestamp()),
        )
        connection.execute(
            """
            DELETE FROM background_job_events
            WHERE job_id = ? AND sequence NOT IN (
                SELECT sequence FROM background_job_events
                WHERE job_id = ? ORDER BY sequence DESC LIMIT ?
            )
            """,
            (job_id, job_id, self.max_events_per_job),
        )

    def events(self, job_id: str, *, after: int, limit: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM background_job_events
                WHERE job_id = ? AND sequence > ? ORDER BY sequence LIMIT ?
                """,
                (job_id, max(0, after), max(1, min(limit, 500))),
            ).fetchall()

    def request_cancel(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] in ACTIVE_STATUSES:
                connection.execute(
                    "UPDATE background_jobs SET status = 'cancelling', stage = 'cancelling', cancel_requested = 1, message = ? WHERE id = ?",
                    ("正在取消任务", job_id),
                )
                self._append_event(connection, job_id, "status", {"status": "cancelling", "message": "正在取消任务"})
            return connection.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        message: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        now = _utcnow()
        progress = 100 if status == "completed" else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE background_jobs
                SET status = ?, stage = ?, progress = COALESCE(?, progress), message = ?,
                    result_payload = ?, error = ?, finished_at = ?, heartbeat_at = ?, expires_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    status,
                    progress,
                    message,
                    _dumps(result) if result is not None else None,
                    error,
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now + self.ttl),
                    job_id,
                ),
            )
            if result is not None:
                self._append_event(connection, job_id, "result", {"result": result})
            if error:
                self._append_event(connection, job_id, "error", {"message": error})
            self._append_event(connection, job_id, "status", {"status": status, "message": message})

    def interrupt_stale(self, *, stale_before: str, all_active: bool = False) -> int:
        with self.connect() as connection:
            if all_active:
                rows = connection.execute(
                    "SELECT id FROM background_jobs WHERE status IN ('running', 'cancelling')",
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id FROM background_jobs
                    WHERE status IN ('running', 'cancelling')
                      AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                    """,
                    (stale_before,),
                ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE background_jobs SET status = 'interrupted', stage = 'interrupted',
                        message = ?, error = ?, finished_at = ?, heartbeat_at = ? WHERE id = ?
                    """,
                    ("服务中断，任务未完成", "background worker heartbeat expired", _timestamp(), _timestamp(), row["id"]),
                )
                self._append_event(connection, row["id"], "status", {"status": "interrupted", "message": "服务中断，任务未完成"})
            return len(rows)

    def interrupt_jobs(self, job_ids: list[str]) -> int:
        """仅中断当前进程实际持有的运行任务，避免影响其他进程。"""
        if not job_ids:
            return 0
        interrupted = 0
        with self.connect() as connection:
            for job_id in job_ids:
                row = connection.execute(
                    "SELECT status FROM background_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if not row or row["status"] not in {"running", "cancelling"}:
                    continue
                connection.execute(
                    """
                    UPDATE background_jobs SET status = 'interrupted', stage = 'interrupted',
                        message = ?, error = ?, finished_at = ?, heartbeat_at = ? WHERE id = ?
                    """,
                    ("服务中断，任务未完成", "background worker stopped", _timestamp(), _timestamp(), job_id),
                )
                self._append_event(connection, job_id, "status", {"status": "interrupted", "message": "服务中断，任务未完成"})
                interrupted += 1
        return interrupted

    def cleanup(self) -> int:
        now = _timestamp()
        with self.connect() as connection:
            expired = connection.execute(
                "SELECT id FROM background_jobs WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted') AND expires_at < ?",
                (now,),
            ).fetchall()
            overflow = connection.execute(
                """
                SELECT id FROM background_jobs
                WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')
                ORDER BY created_at DESC LIMIT -1 OFFSET ?
                """,
                (self.max_history,),
            ).fetchall()
            ids = {row["id"] for row in [*expired, *overflow]}
            for job_id in ids:
                connection.execute("DELETE FROM background_job_events WHERE job_id = ?", (job_id,))
                connection.execute("DELETE FROM background_jobs WHERE id = ?", (job_id,))
            return len(ids)


class BackgroundJobContext:
    """向业务处理器暴露最小化的进度、日志和取消接口。"""

    def __init__(self, manager: "BackgroundJobManager", job_id: str, cancel_event: threading.Event) -> None:
        self.manager = manager
        self.job_id = job_id
        self.cancel_event = cancel_event

    def check_cancelled(self) -> None:
        raise_if_task_cancelled(self.cancel_event)

    def log(self, message: str, **payload: Any) -> None:
        self.check_cancelled()
        self.manager.repository.append_event(self.job_id, "log", {"message": message, **payload})

    def progress(self, value: int, *, stage: str = "running", message: str = "", **payload: Any) -> None:
        self.check_cancelled()
        self.manager.repository.update_progress(
            self.job_id,
            progress=value,
            stage=stage,
            message=message or None,
            event_payload=payload,
        )


class BackgroundJobManager:
    """持久化状态为权威来源，内存仅保存运行信号和处理器。"""

    def __init__(
        self,
        *,
        db_path: str | Path = settings.background_job_db,
        max_workers: int = settings.background_job_max_workers,
        max_pending_tasks: int = settings.background_job_max_pending_tasks,
        stale_seconds: int = settings.background_job_stale_seconds,
        heartbeat_seconds: int = settings.background_job_heartbeat_seconds,
        cleanup_interval_seconds: int = settings.background_job_cleanup_interval_seconds,
        ttl_hours: int = settings.background_job_ttl_hours,
        max_history: int = settings.background_job_max_history,
        max_events_per_job: int = settings.background_job_max_events_per_job,
    ) -> None:
        self.max_workers = max(1, max_workers)
        self.max_pending_tasks = max(0, max_pending_tasks)
        self.stale_seconds = max(5, stale_seconds)
        self.heartbeat_seconds = max(1, heartbeat_seconds)
        self.cleanup_interval_seconds = max(1, cleanup_interval_seconds)
        self.repository = BackgroundJobRepository(
            db_path,
            ttl_hours=ttl_hours,
            max_history=max_history,
            max_events_per_job=max_events_per_job,
        )
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="background-job")
        self._capacity = threading.BoundedSemaphore(self.max_workers + self.max_pending_tasks)
        self._handlers: dict[str, JobHandler] = {}
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._shutdown = False

    def register(self, job_type: str, handler: JobHandler) -> None:
        self._handlers[job_type] = handler

    @property
    def supported_types(self) -> set[str]:
        return set(self._handlers)

    def start(self) -> None:
        stale_before = _timestamp(_utcnow() - timedelta(seconds=self.stale_seconds))
        self.repository.interrupt_stale(stale_before=stale_before)
        self._recover_queued()
        if self._maintenance_thread and self._maintenance_thread.is_alive():
            return
        self._stop_event.clear()
        self._maintenance_thread = threading.Thread(target=self._maintenance_loop, name="background-job-maintenance", daemon=True)
        self._maintenance_thread.start()

    def submit(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        session_id: str = "local",
        user_id: str | None = None,
        dedupe_key: str | None = None,
        retryable: bool = True,
        retry_of: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if job_type not in self._handlers:
            raise ValueError(f"不支持的后台任务类型：{job_type}")
        if not self._capacity.acquire(blocking=False):
            raise BackgroundJobCapacityExceeded(
                f"后台任务已达到容量上限：运行 {self.max_workers}，排队 {self.max_pending_tasks}",
            )
        row, created = self.repository.create(
            job_type,
            payload,
            session_id=session_id,
            user_id=user_id,
            dedupe_key=dedupe_key,
            retry_of=retry_of,
            retryable=retryable,
        )
        if not created:
            self._capacity.release()
            return self._public(row), False
        try:
            self._schedule(row["id"])
        except Exception:
            self._capacity.release()
            self.repository.finish(row["id"], status="interrupted", message="任务执行器不可用", error="background executor unavailable")
            raise
        return self._public(self.repository.get(row["id"])), True

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.repository.get(job_id)
        return self._public(row) if row else None

    def list(self, *, session_id: str = "local", limit: int = 100) -> list[dict[str, Any]]:
        return [self._public(row) for row in self.repository.list(session_id=session_id, limit=limit)]

    def find_active(self, job_type: str, dedupe_key: str) -> dict[str, Any] | None:
        row = self.repository.find_active(job_type, dedupe_key)
        return self._public(row) if row else None

    def events(self, job_id: str, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {
                "sequence": row["sequence"],
                "jobId": row["job_id"],
                "type": row["type"],
                "payload": _loads(row["payload"], {}),
                "createdAt": row["created_at"],
            }
            for row in self.repository.events(job_id, after=after, limit=limit)
        ]

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        row = self.repository.request_cancel(job_id)
        if not row:
            return None
        with self._lock:
            cancel_event = self._active.get(job_id)
        if cancel_event:
            cancel_event.set()
        return self.get(job_id)

    def retry(self, job_id: str) -> tuple[dict[str, Any], bool] | None:
        row = self.repository.get(job_id)
        if not row:
            return None
        if row["status"] not in TERMINAL_STATUSES:
            raise ValueError("任务尚未结束，不能重试")
        if not row["retryable"]:
            raise ValueError("该任务不支持自动重试")
        return self.submit(
            row["type"],
            _loads(row["request_payload"], {}),
            session_id=row["session_id"],
            user_id=row["user_id"],
            dedupe_key=row["dedupe_key"],
            retryable=bool(row["retryable"]),
            retry_of=row["id"],
        )

    def shutdown(self) -> None:
        self._shutdown = True
        self._stop_event.set()
        with self._lock:
            active = list(self._active.items())
        self.repository.interrupt_jobs([job_id for job_id, _cancel_event in active])
        for _job_id, cancel_event in active:
            cancel_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._maintenance_thread and self._maintenance_thread.is_alive():
            self._maintenance_thread.join(timeout=2)

    def _schedule(self, job_id: str) -> None:
        cancel_event = threading.Event()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("后台任务执行器正在关闭")
            self._active[job_id] = cancel_event
        self._executor.submit(self._run, job_id, cancel_event)

    def _run(self, job_id: str, cancel_event: threading.Event) -> None:
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            row = self.repository.get(job_id)
            if not row:
                return
            if row["cancel_requested"] or row["status"] == "cancelling":
                raise TaskCancelled("任务已取消")
            if not self.repository.mark_running(job_id):
                return
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(job_id, cancel_event, heartbeat_stop),
                name=f"background-job-heartbeat-{job_id[:8]}",
                daemon=True,
            )
            heartbeat_thread.start()
            handler = self._handlers.get(row["type"])
            if not handler:
                raise RuntimeError(f"未注册任务处理器：{row['type']}")
            context = BackgroundJobContext(self, job_id, cancel_event)
            context.check_cancelled()
            result = handler(context, _loads(row["request_payload"], {})) or {}
            context.check_cancelled()
            self.repository.finish(job_id, status="completed", message="任务已完成", result=result)
        except TaskCancelled:
            current = self.repository.get(job_id)
            if current and current["status"] != "interrupted":
                self.repository.finish(job_id, status="cancelled", message="任务已取消")
        except Exception as error:
            logger.exception("后台任务执行失败：job_id=%s", job_id)
            current = self.repository.get(job_id)
            if current and current["status"] != "interrupted":
                self.repository.finish(job_id, status="failed", message="任务执行失败", error=str(error))
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=2)
            with self._lock:
                self._active.pop(job_id, None)
            self._capacity.release()

    def _heartbeat_loop(self, job_id: str, cancel_event: threading.Event, stop_event: threading.Event) -> None:
        while not stop_event.wait(self.heartbeat_seconds):
            try:
                if self.repository.heartbeat(job_id):
                    cancel_event.set()
            except sqlite3.Error:
                logger.exception("后台任务心跳写入失败：job_id=%s", job_id)

    def _recover_queued(self) -> None:
        for row in self.repository.list_queued(self.max_workers + self.max_pending_tasks):
            if row["type"] not in self._handlers or not self._capacity.acquire(blocking=False):
                continue
            try:
                self._schedule(row["id"])
            except RuntimeError:
                self._capacity.release()
                break

    def _maintenance_loop(self) -> None:
        while not self._stop_event.wait(self.cleanup_interval_seconds):
            try:
                stale_before = _timestamp(_utcnow() - timedelta(seconds=self.stale_seconds))
                self.repository.interrupt_stale(stale_before=stale_before)
                self.repository.cleanup()
                self._recover_queued()
            except sqlite3.Error:
                logger.exception("后台任务定时维护失败")

    def _public(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        return {
            "jobId": row["id"],
            "userId": row["user_id"],
            "sessionId": row["session_id"],
            "type": row["type"],
            "status": row["status"],
            "stage": row["stage"],
            "progress": row["progress"],
            "message": row["message"],
            "request": _loads(row["request_payload"], {}),
            "result": _loads(row["result_payload"], None),
            "error": row["error"] or "",
            "retryOf": row["retry_of"],
            "retryable": bool(row["retryable"]),
            "cancelRequested": bool(row["cancel_requested"]),
            "createdAt": row["created_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "heartbeatAt": row["heartbeat_at"],
        }


background_job_manager = BackgroundJobManager()


__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "BackgroundJobCapacityExceeded",
    "BackgroundJobContext",
    "BackgroundJobManager",
    "BackgroundJobRepository",
    "background_job_manager",
]
