"""验证流式任务容量、后台存续、显式取消与有界事件缓冲。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import streaming
from app.services.stream_tasks import (
    BoundedEventBuffer,
    StreamCapacityExceeded,
    StreamTaskManager,
    raise_if_stream_cancelled,
)


def wait_for_status(manager: StreamTaskManager, job_id: str, statuses: set[str]) -> str:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = manager.get(job_id)
        if task and task.status in statuses:
            return task.status
        time.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 未在时限内进入状态 {statuses}")


class BoundedEventBufferTest(unittest.TestCase):
    def test_slow_consumer_does_not_grow_buffer_or_drop_terminal_events(self) -> None:
        buffer = BoundedEventBuffer(16)
        buffer.emit({"type": "job", "jobId": "job"})
        for index in range(1000):
            buffer.emit({"type": "log", "message": f"日志 {index}"})
            buffer.emit({"type": "progress", "value": index})
        buffer.emit({"type": "result", "value": "ok"})
        buffer.emit({"type": "done", "status": "completed"})

        events, _ = buffer.read_after(0)
        event_types = [event.payload["type"] for event in events]
        progress = [event.payload for event in events if event.payload["type"] == "progress"]

        self.assertLessEqual(buffer.size, 16)
        self.assertGreater(buffer.dropped_log_count, 0)
        self.assertIn("result", event_types)
        self.assertIn("done", event_types)
        self.assertEqual(progress[-1]["value"], 999)
        self.assertGreater(events[-1].payload["droppedLogCount"], 0)


class StreamTaskManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.managers: list[StreamTaskManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown()

    def manager(
        self,
        *,
        workers: int = 4,
        pending: int = 20,
        queue_size: int = 32,
        retention_seconds: float = 86400,
        max_retained_tasks: int = 200,
    ) -> StreamTaskManager:
        manager = StreamTaskManager(
            max_workers=workers,
            max_pending_tasks=pending,
            event_queue_size=queue_size,
            retention_seconds=retention_seconds,
            max_retained_tasks=max_retained_tasks,
        )
        self.managers.append(manager)
        return manager

    def test_one_hundred_submissions_keep_worker_threads_bounded(self) -> None:
        manager = self.manager(workers=4, pending=20)
        release = threading.Event()

        def producer(emit, cancel_event) -> None:
            while not release.wait(0.01):
                raise_if_stream_cancelled(cancel_event)

        accepted = []
        rejected = 0
        for _ in range(100):
            try:
                accepted.append(manager.submit(producer))
            except StreamCapacityExceeded:
                rejected += 1

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            workers = [thread for thread in threading.enumerate() if thread.name.startswith("stream-task")]
            if len(workers) == 4:
                break
            time.sleep(0.01)

        self.assertEqual(len(accepted), 24)
        self.assertEqual(rejected, 76)
        self.assertLessEqual(len(workers), 4)
        release.set()
        for task in accepted:
            wait_for_status(manager, task.id, {"completed"})

    def test_overload_is_rejected_without_waiting(self) -> None:
        manager = self.manager(workers=1, pending=1)
        release = threading.Event()
        producer = lambda emit, cancel: release.wait(2)
        manager.submit(producer)
        manager.submit(producer)

        started_at = time.monotonic()
        with self.assertRaises(StreamCapacityExceeded):
            manager.submit(producer)
        self.assertLess(time.monotonic() - started_at, 0.1)
        release.set()

    def test_http_adapter_returns_503_when_capacity_is_full(self) -> None:
        manager = self.manager(workers=1, pending=0)
        release = threading.Event()
        manager.submit(lambda emit, cancel: release.wait(2))

        with patch.object(streaming, "stream_task_manager", manager):
            with self.assertRaises(HTTPException) as raised:
                streaming.ndjson_worker_response(object(), lambda emit, cancel: None)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.headers["Retry-After"], "1")
        release.set()

    def test_cancelled_http_step_cannot_emit_result_after_return(self) -> None:
        manager = self.manager(workers=1, pending=0)
        http_returned = threading.Event()

        def producer(emit, cancel_event) -> None:
            http_returned.wait(2)
            emit({"type": "result", "value": "不应发送"})

        task = manager.submit(producer)
        wait_for_status(manager, task.id, {"running"})
        self.assertTrue(manager.cancel(task.id))
        http_returned.set()
        self.assertEqual(wait_for_status(manager, task.id, {"cancelled"}), "cancelled")

        events, _ = task.buffer.read_after(0)
        self.assertNotIn("result", [event.payload["type"] for event in events])
        done = [event.payload for event in events if event.payload["type"] == "done"]
        self.assertEqual(done[-1]["status"], "cancelled")

    def test_completed_task_is_retained_until_ttl_expires(self) -> None:
        manager = self.manager(workers=1, pending=0, retention_seconds=0.05)
        task = manager.submit(lambda emit, cancel: emit({"type": "result", "value": "ok"}))
        self.assertEqual(wait_for_status(manager, task.id, {"completed"}), "completed")
        self.assertIs(manager.get(task.id), task)

        task.finished_at = time.monotonic() - 1

        self.assertIsNone(manager.get(task.id))

    def test_retained_task_history_is_bounded(self) -> None:
        manager = self.manager(workers=1, pending=0, max_retained_tasks=2)
        tasks = []
        for index in range(3):
            task = manager.submit(lambda emit, cancel, value=index: emit({"type": "result", "value": value}))
            wait_for_status(manager, task.id, {"completed"})
            tasks.append(task)

        self.assertEqual(manager.task_count, 2)
        self.assertIsNone(manager.get(tasks[0].id))
        self.assertIsNotNone(manager.get(tasks[-1].id))


class StreamDisconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_only_ends_subscription_and_task_continues(self) -> None:
        manager = StreamTaskManager(max_workers=1, max_pending_tasks=0, event_queue_size=16)
        release = threading.Event()

        def producer(emit, cancel_event) -> None:
            release.wait(2)
            raise_if_stream_cancelled(cancel_event)
            emit({"type": "result", "value": "后台完成"})

        task = manager.submit(producer)

        class DisconnectedRequest:
            async def is_disconnected(self) -> bool:
                return True

        try:
            with patch.object(streaming, "stream_task_manager", manager):
                events = [event async for event in streaming._event_stream(DisconnectedRequest(), task.id)]
            self.assertEqual(events, [])
            self.assertFalse(task.cancel_event.is_set())
            release.set()
            self.assertEqual(wait_for_status(manager, task.id, {"completed"}), "completed")
            buffered, _ = task.buffer.read_after(0)
            self.assertIn("result", [event.payload["type"] for event in buffered])
        finally:
            release.set()
            manager.shutdown()

    async def test_completed_task_can_be_subscribed_again(self) -> None:
        manager = StreamTaskManager(max_workers=1, max_pending_tasks=0, event_queue_size=16)
        task = manager.submit(lambda emit, cancel: emit({"type": "result", "value": "ok"}))
        wait_for_status(manager, task.id, {"completed"})

        class ConnectedRequest:
            async def is_disconnected(self) -> bool:
                return False

        try:
            with patch.object(streaming, "stream_task_manager", manager):
                payloads = [
                    json.loads(event)
                    async for event in streaming._event_stream(ConnectedRequest(), task.id)
                ]
            self.assertIn("result", [payload["type"] for payload in payloads])
            self.assertEqual(payloads[-1]["type"], "done")
            self.assertIs(manager.get(task.id), task)
        finally:
            manager.shutdown()

    async def test_explicit_cancel_endpoint_stops_task(self) -> None:
        manager = StreamTaskManager(max_workers=1, max_pending_tasks=0, event_queue_size=16)

        def producer(emit, cancel_event) -> None:
            while not cancel_event.wait(0.01):
                pass
            raise_if_stream_cancelled(cancel_event)

        task = manager.submit(producer)
        wait_for_status(manager, task.id, {"running"})
        try:
            with patch.object(streaming, "stream_task_manager", manager):
                response = await streaming.cancel_stream_job(task.id)
            self.assertEqual(response["status"], "cancelling")
            self.assertEqual(wait_for_status(manager, task.id, {"cancelled"}), "cancelled")
        finally:
            manager.shutdown()

    async def test_idle_subscription_emits_heartbeat(self) -> None:
        manager = StreamTaskManager(max_workers=1, max_pending_tasks=0, event_queue_size=16)

        def producer(emit, cancel_event) -> None:
            while not cancel_event.wait(0.01):
                pass
            raise_if_stream_cancelled(cancel_event)

        task = manager.submit(producer)

        class ConnectedRequest:
            async def is_disconnected(self) -> bool:
                return False

        generator = None
        try:
            with (
                patch.object(streaming, "stream_task_manager", manager),
                patch.object(streaming.settings, "stream_heartbeat_seconds", 0.05),
            ):
                generator = streaming._event_stream(ConnectedRequest(), task.id)
                event_types = []
                deadline = asyncio.get_running_loop().time() + 1
                while "heartbeat" not in event_types and asyncio.get_running_loop().time() < deadline:
                    payload = json.loads(await anext(generator))
                    event_types.append(payload["type"])
                self.assertIn("heartbeat", event_types)
        finally:
            if generator is not None:
                await generator.aclose()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
