"""验证领域树后台任务、取消和有限重试。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

import requests


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.domain_tree_jobs import DomainTreeJobManager
from app.services.task_control import (
    DomainTreeGenerationCancelled,
    call_with_retry,
)


class ModelRetryTest(unittest.TestCase):
    """只对可恢复网络错误进行有限重试。"""

    def test_retries_timeout_then_returns_result(self) -> None:
        calls = 0
        retries: list[int] = []

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise requests.ReadTimeout("temporary timeout")
            return "ok"

        result = call_with_retry(
            operation,
            max_attempts=3,
            base_delay_seconds=0,
            on_retry=lambda attempt, error, delay: retries.append(attempt),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 3)
        self.assertEqual(retries, [1, 2])

    def test_cancel_interrupts_retry_backoff(self) -> None:
        cancel_event = threading.Event()

        def on_retry(attempt: int, error: Exception, delay: float) -> None:
            cancel_event.set()

        with self.assertRaises(DomainTreeGenerationCancelled):
            call_with_retry(
                lambda: (_ for _ in ()).throw(requests.ReadTimeout("timeout")),
                max_attempts=3,
                base_delay_seconds=10,
                cancel_event=cancel_event,
                on_retry=on_retry,
            )


class DomainTreeJobManagerTest(unittest.TestCase):
    """后台任务必须可观察、可取消且同项目去重。"""

    def wait_for_terminal(self, manager: DomainTreeJobManager, job_id: str) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = manager.get(job_id)
            if job and job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        self.fail("领域树任务未在测试时限内结束")

    def test_reports_progress_and_result(self) -> None:
        manager = DomainTreeJobManager(max_workers=1)

        def runner(report, cancel_event) -> dict:
            report({"stage": "semantic_extraction", "completedChunks": 1, "totalChunks": 2})
            return {"domainTree": [{"label": "测试"}]}

        job, created = manager.submit("workspace", "rebuild", runner)
        terminal = self.wait_for_terminal(manager, job["jobId"])

        self.assertTrue(created)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["progress"]["completedChunks"], 1)
        self.assertEqual(terminal["result"]["domainTree"][0]["label"], "测试")

    def test_reuses_active_job_and_cancels_it(self) -> None:
        manager = DomainTreeJobManager(max_workers=1)
        started = threading.Event()

        def runner(report, cancel_event) -> dict:
            started.set()
            while not cancel_event.wait(0.01):
                pass
            raise DomainTreeGenerationCancelled("cancelled")

        first, first_created = manager.submit("workspace", "rebuild", runner)
        self.assertTrue(started.wait(1))
        duplicate, duplicate_created = manager.submit("workspace", "revise", runner)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["jobId"], first["jobId"])

        cancelling = manager.cancel(first["jobId"])
        terminal = self.wait_for_terminal(manager, first["jobId"])

        self.assertEqual(cancelling["status"], "cancelling")
        self.assertEqual(terminal["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
