"""统一后台任务的持久化、事件游标、取消与恢复测试。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.services.background_jobs import BackgroundJobManager


class BackgroundJobManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "background.sqlite3"
        self.managers: list[BackgroundJobManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown()
        self.temp_dir.cleanup()

    def manager(self) -> BackgroundJobManager:
        manager = BackgroundJobManager(
            db_path=self.db_path,
            max_workers=1,
            max_pending_tasks=2,
            heartbeat_seconds=1,
            cleanup_interval_seconds=60,
            ttl_hours=1,
            max_history=20,
            max_events_per_job=20,
        )
        self.managers.append(manager)
        return manager

    def wait(self, manager: BackgroundJobManager, job_id: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(job_id)
            if job and job["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return job
            time.sleep(0.01)
        self.fail("后台任务未在测试时限内结束")

    def test_result_and_events_survive_restart(self) -> None:
        first = self.manager()

        def handler(context, payload):
            context.log("开始测试")
            context.progress(50, stage="working", message="处理中")
            return {"value": payload["value"]}

        first.register("test", handler)
        job, created = first.submit("test", {"value": 42})
        completed = self.wait(first, job["jobId"])
        events = first.events(job["jobId"], after=0)

        self.assertTrue(created)
        self.assertEqual(completed["result"], {"value": 42})
        self.assertEqual(completed["progress"], 100)
        self.assertIn("log", [event["type"] for event in events])
        cursor = events[-2]["sequence"]
        self.assertTrue(all(event["sequence"] > cursor for event in first.events(job["jobId"], after=cursor)))

        restarted = self.manager()
        restarted.register("test", handler)
        self.assertEqual(restarted.get(job["jobId"])["result"], {"value": 42})

    def test_explicit_cancel_is_persisted(self) -> None:
        manager = self.manager()
        started = threading.Event()

        def handler(context, payload):
            started.set()
            while not context.cancel_event.wait(0.01):
                context.check_cancelled()
            context.check_cancelled()

        manager.register("blocking", handler)
        job, _ = manager.submit("blocking", {})
        self.assertTrue(started.wait(1))
        cancelling = manager.cancel(job["jobId"])
        terminal = self.wait(manager, job["jobId"])

        self.assertEqual(cancelling["status"], "cancelling")
        self.assertEqual(terminal["status"], "cancelled")

    def test_active_dedupe_reuses_job(self) -> None:
        manager = self.manager()
        release = threading.Event()
        manager.register("dedupe", lambda context, payload: release.wait(2) or {})
        first, created = manager.submit("dedupe", {}, dedupe_key="same")
        second, second_created = manager.submit("dedupe", {}, dedupe_key="same")
        release.set()

        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertEqual(first["jobId"], second["jobId"])
        self.wait(manager, first["jobId"])


if __name__ == "__main__":
    unittest.main()
