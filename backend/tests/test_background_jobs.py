"""统一后台任务的持久化、事件游标、取消与恢复测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.services.background_jobs import BackgroundJobManager, BackgroundJobRepository


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

    def test_existing_metric_table_is_migrated_for_empty_response_diagnostics(self) -> None:
        """已有数据库启动时应补齐停止原因和推理 Token 列，不要求人工删库。"""
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """
                CREATE TABLE model_call_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    document_id TEXT,
                    chunk_index INTEGER,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_category TEXT,
                    http_status INTEGER,
                    request_accepted INTEGER,
                    request_id TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cached_tokens INTEGER,
                    elapsed_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        BackgroundJobRepository(
            self.db_path,
            ttl_hours=1,
            max_history=20,
            max_events_per_job=20,
        )

        connection = sqlite3.connect(self.db_path)
        try:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(model_call_metrics)")
            }
        finally:
            connection.close()
        self.assertIn("finish_reason", columns)
        self.assertIn("reasoning_tokens", columns)

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
            context.progress(
                50,
                stage="working",
                message="处理中",
                details={
                    "completedChunks": 6,
                    "totalChunks": 10,
                    "processedChunks": 5,
                    "failedChunks": 1,
                },
            )
            return {"value": payload["value"]}

        first.register("test", handler)
        job, created = first.submit("test", {"value": 42})
        completed = self.wait(first, job["jobId"])
        events = first.events(job["jobId"], after=0)

        self.assertTrue(created)
        self.assertEqual(completed["result"], {"value": 42})
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(
            completed["progressDetails"],
            {
                "completedChunks": 6,
                "totalChunks": 10,
                "processedChunks": 5,
                "failedChunks": 1,
            },
        )
        self.assertIn("log", [event["type"] for event in events])
        cursor = events[-2]["sequence"]
        self.assertTrue(all(event["sequence"] > cursor for event in first.events(job["jobId"], after=cursor)))

        restarted = self.manager()
        restarted.register("test", handler)
        self.assertEqual(restarted.get(job["jobId"])["result"], {"value": 42})
        self.assertEqual(restarted.get(job["jobId"])["progressDetails"]["completedChunks"], 6)

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

    def test_model_metrics_are_persisted_outside_event_retention(self) -> None:
        """模型调用计量必须独立持久化并可按任务聚合。"""
        manager = self.manager()

        def handler(context, payload):
            context.record_model_call(
                {
                    "stage": "semantic_extraction",
                    "documentId": "paper-1",
                    "chunkIndex": 1,
                    "attempt": 1,
                    "status": "success",
                    "promptTokens": 100,
                    "completionTokens": 20,
                    "totalTokens": 120,
                    "cachedTokens": 10,
                    "reasoningTokens": 8,
                    "finishReason": "stop",
                    "elapsedMs": 25,
                }
            )
            context.record_model_call(
                {
                    "stage": "semantic_extraction",
                    "documentId": "paper-1",
                    "chunkIndex": 2,
                    "attempt": 1,
                    "status": "failed",
                    "errorCategory": "quota_exhausted",
                    "httpStatus": 402,
                    "requestAccepted": False,
                    "elapsedMs": 5,
                }
            )
            return {"usage": context.model_usage_summary()}

        manager.register("metrics", handler)
        job, _ = manager.submit("metrics", {})
        completed = self.wait(manager, job["jobId"])
        usage = completed["result"]["usage"]

        self.assertEqual(usage["callCount"], 2)
        self.assertEqual(usage["totalTokens"], 120)
        self.assertEqual(usage["cachedTokens"], 10)
        self.assertEqual(usage["reasoningTokens"], 8)
        self.assertEqual(usage["errors"], {"quota_exhausted": 1})
        self.assertEqual(completed["modelUsage"]["callCount"], 2)


if __name__ == "__main__":
    unittest.main()
