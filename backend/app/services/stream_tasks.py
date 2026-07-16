"""受限流式任务执行器与有界事件缓冲。"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.config import settings
from app.services.task_control import TaskCancelled, raise_if_task_cancelled


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
CRITICAL_EVENT_TYPES = {"job", "result", "error", "done"}
StreamEmitter = Callable[[dict[str, Any]], None]
StreamProducer = Callable[[StreamEmitter, threading.Event], None]


class StreamCapacityExceeded(RuntimeError):
    """流式执行池和等待队列均已满。"""


StreamTaskCancelled = TaskCancelled
raise_if_stream_cancelled = raise_if_task_cancelled


@dataclass(frozen=True, slots=True)
class BufferedEvent:
    sequence: int
    payload: dict[str, Any]


class BoundedEventBuffer:
    """为关键事件预留空间，并对高频非关键事件执行有界降载。"""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(8, capacity)
        self._regular_capacity = self.capacity - 4
        self._events: deque[BufferedEvent] = deque()
        self._next_sequence = 1
        self._dropped_log_count = 0
        self._lock = threading.RLock()

    def emit(self, event: dict[str, Any]) -> bool:
        payload = dict(event)
        event_type = str(payload.get("type") or "log")
        with self._lock:
            if event_type == "log" and self._regular_count() >= self._regular_capacity:
                self._dropped_log_count += 1
                return False

            if event_type == "progress" and self._regular_count() >= self._regular_capacity:
                # 只保留最新进度，避免慢客户端积压大量过时状态。
                for index in range(len(self._events) - 1, -1, -1):
                    if self._events[index].payload.get("type") == "progress":
                        del self._events[index]
                        break
                else:
                    return False

            if event_type not in CRITICAL_EVENT_TYPES and self._regular_count() >= self._regular_capacity:
                return False

            if self._dropped_log_count:
                payload["droppedLogCount"] = self._dropped_log_count

            if len(self._events) >= self.capacity:
                self._evict_noncritical()
            if len(self._events) >= self.capacity:
                # 预留空间保证正常任务生命周期最多四个关键事件不会走到这里。
                raise RuntimeError("流式关键事件缓冲区容量不足")

            self._events.append(BufferedEvent(self._next_sequence, payload))
            self._next_sequence += 1
            return True

    def read_after(self, sequence: int) -> tuple[list[BufferedEvent], int]:
        with self._lock:
            events = [event for event in self._events if event.sequence > sequence]
        next_sequence = events[-1].sequence if events else sequence
        return events, next_sequence

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def dropped_log_count(self) -> int:
        with self._lock:
            return self._dropped_log_count

    def _regular_count(self) -> int:
        return sum(event.payload.get("type") not in CRITICAL_EVENT_TYPES for event in self._events)

    def _evict_noncritical(self) -> None:
        for index, event in enumerate(self._events):
            if event.payload.get("type") not in CRITICAL_EVENT_TYPES:
                del self._events[index]
                return


@dataclass(slots=True)
class StreamTask:
    id: str
    producer: StreamProducer
    buffer: BoundedEventBuffer
    status: str = "queued"
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    subscriber_count: int = 0


class StreamTaskManager:
    """用固定线程池执行任务，并在提交前限制运行与排队总量。"""

    def __init__(
        self,
        *,
        max_workers: int,
        max_pending_tasks: int,
        event_queue_size: int,
        retention_seconds: float = 86400,
        max_retained_tasks: int = 200,
    ) -> None:
        self.max_workers = max(1, max_workers)
        self.max_pending_tasks = max(0, max_pending_tasks)
        self.event_queue_size = max(8, event_queue_size)
        self.retention_seconds = max(0.0, retention_seconds)
        self.max_retained_tasks = max(1, max_retained_tasks)
        self._capacity = threading.BoundedSemaphore(self.max_workers + self.max_pending_tasks)
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="stream-task",
        )
        self._tasks: dict[str, StreamTask] = {}
        self._lock = threading.RLock()
        self._shutdown = False

    def submit(self, producer: StreamProducer) -> StreamTask:
        if not self._capacity.acquire(blocking=False):
            raise StreamCapacityExceeded(
                f"流式任务已达到容量上限：运行 {self.max_workers}，排队 {self.max_pending_tasks}",
            )
        with self._lock:
            if self._shutdown:
                self._capacity.release()
                raise StreamCapacityExceeded("流式任务执行器正在关闭")
            self._prune_finished_locked()
            job_id = uuid.uuid4().hex
            task = StreamTask(
                id=job_id,
                producer=producer,
                buffer=BoundedEventBuffer(self.event_queue_size),
            )
            task.buffer.emit({"type": "job", "jobId": job_id, "status": "queued"})
            self._tasks[job_id] = task
        try:
            self._executor.submit(self._run, task)
        except RuntimeError:
            with self._lock:
                self._tasks.pop(job_id, None)
            self._capacity.release()
            raise StreamCapacityExceeded("流式任务执行器不可用")
        return task

    def get(self, job_id: str) -> StreamTask | None:
        with self._lock:
            self._prune_finished_locked()
            return self._tasks.get(job_id)

    def begin_subscription(self, job_id: str) -> StreamTask | None:
        with self._lock:
            self._prune_finished_locked()
            task = self._tasks.get(job_id)
            if task:
                task.subscriber_count += 1
            return task

    def end_subscription(self, job_id: str) -> None:
        with self._lock:
            task = self._tasks.get(job_id)
            if not task:
                return
            task.subscriber_count = max(0, task.subscriber_count - 1)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(job_id)
            if not task or task.status in TERMINAL_STATUSES:
                return False
            task.cancel_event.set()
            if task.status == "queued":
                task.status = "cancelling"
            elif task.status == "running":
                task.status = "cancelling"
            return True

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            for task in self._tasks.values():
                if task.status not in TERMINAL_STATUSES:
                    task.cancel_event.set()
                    task.status = "cancelling"
        self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def task_count(self) -> int:
        with self._lock:
            self._prune_finished_locked()
            return len(self._tasks)

    def _run(self, task: StreamTask) -> None:
        with self._lock:
            should_run = not task.cancel_event.is_set()
            task.status = "running" if should_run else "cancelled"
        if should_run:
            task.buffer.emit({"type": "progress", "jobId": task.id, "status": "running"})

        def emit(event: dict[str, Any]) -> None:
            raise_if_stream_cancelled(task.cancel_event)
            task.buffer.emit(event)

        try:
            raise_if_stream_cancelled(task.cancel_event)
            task.producer(emit, task.cancel_event)
            with self._lock:
                if task.cancel_event.is_set():
                    raise StreamTaskCancelled("任务已取消")
                task.status = "completed"
        except StreamTaskCancelled:
            with self._lock:
                task.status = "cancelled"
        except Exception as error:
            with self._lock:
                cancelled = task.cancel_event.is_set()
                task.status = "cancelled" if cancelled else "failed"
            if not cancelled:
                task.buffer.emit({"type": "error", "jobId": task.id, "message": str(error)})
        finally:
            with self._lock:
                task.finished_at = time.monotonic()
            task.buffer.emit(
                {
                    "type": "done",
                    "jobId": task.id,
                    "status": task.status,
                    "droppedLogCount": task.buffer.dropped_log_count,
                },
            )
            self._capacity.release()

    def _prune_finished_locked(self) -> None:
        now = time.monotonic()
        finished = sorted(
            (
            task
            for task in self._tasks.values()
            if task.status in TERMINAL_STATUSES and task.subscriber_count == 0
            ),
            key=lambda item: item.finished_at or item.created_at,
        )
        expired = [
            task
            for task in finished
            if task.finished_at is not None and now - task.finished_at >= self.retention_seconds
        ]
        for task in expired:
            self._tasks.pop(task.id, None)
        retained = [task for task in finished if task.id in self._tasks]
        overflow = max(0, len(retained) - self.max_retained_tasks)
        for task in retained[:overflow]:
            self._tasks.pop(task.id, None)


stream_task_manager = StreamTaskManager(
    max_workers=settings.stream_max_workers,
    max_pending_tasks=settings.stream_max_pending_tasks,
    event_queue_size=settings.stream_event_queue_size,
    retention_seconds=settings.stream_task_retention_seconds,
    max_retained_tasks=settings.stream_max_retained_tasks,
)


__all__ = [
    "BoundedEventBuffer",
    "StreamCapacityExceeded",
    "StreamProducer",
    "StreamTask",
    "StreamTaskCancelled",
    "StreamTaskManager",
    "raise_if_stream_cancelled",
    "stream_task_manager",
]
