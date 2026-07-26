"""验证运行时配置原子切换、任务固定版本和分级生效。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import runtime_config_manager
from app.services.env_config import EnvConfigStore
from app.services.runtime_settings import (
    RuntimeConfigManager,
    RuntimeSettingsProxy,
    bind_runtime_settings,
)


def isolated_runtime_manager() -> RuntimeConfigManager:
    """复制启动快照，避免测试修改进程级运行配置。"""

    snapshot = runtime_config_manager.snapshot()
    return RuntimeConfigManager(snapshot.values, revision=snapshot.revision)


class RuntimeSettingsRefreshTest(unittest.TestCase):
    def test_bound_task_keeps_original_snapshot_after_atomic_commit(self) -> None:
        manager = RuntimeConfigManager({"request_timeout": 10})
        settings = RuntimeSettingsProxy(manager)
        original = manager.snapshot()

        with bind_runtime_settings(original):
            candidate = manager.build_candidate({"request_timeout": 30})
            manager.commit(candidate)
            self.assertEqual(settings.request_timeout, 10)
            self.assertEqual(settings.revision, original.revision)

        self.assertEqual(settings.request_timeout, 30)
        self.assertEqual(settings.revision, original.revision + 1)

    def test_hot_restart_and_reindex_fields_are_classified_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = isolated_runtime_manager()
            store = EnvConfigStore(Path(temporary) / ".env", runtime_manager=manager)
            initial = manager.snapshot()

            hot = store.update({"REQUEST_TIMEOUT": 27})
            self.assertEqual(manager.snapshot().get("request_timeout"), 27)
            self.assertEqual(hot["appliedKeys"], ["REQUEST_TIMEOUT"])
            self.assertEqual(hot["restartRequiredKeys"], [])
            self.assertEqual(hot["reindexRequiredKeys"], [])

            restart = store.update({"PORT": 4321})
            self.assertEqual(manager.snapshot().get("port"), initial.get("port"))
            self.assertEqual(restart["restartRequiredKeys"], ["PORT"])

            reindex = store.update({"RAG_EMBEDDING_MODEL": "next-embedding-model"})
            self.assertEqual(
                manager.snapshot().get("rag_embedding_model"),
                "next-embedding-model",
            )
            self.assertEqual(reindex["reindexRequiredKeys"], ["RAG_EMBEDDING_MODEL"])

    def test_persistence_failure_does_not_commit_runtime_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = isolated_runtime_manager()
            store = EnvConfigStore(Path(temporary) / ".env", runtime_manager=manager)
            before = manager.snapshot()

            with patch.object(store, "_atomic_update", side_effect=OSError("磁盘不可写")):
                with self.assertRaisesRegex(OSError, "磁盘不可写"):
                    store.update({"REQUEST_TIMEOUT": 31})

            after = manager.snapshot()
            self.assertEqual(after.revision, before.revision)
            self.assertEqual(after.get("request_timeout"), before.get("request_timeout"))

    def test_runtime_commit_failure_restores_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / ".env"
            env_path.write_text("# 原始配置\nREQUEST_TIMEOUT=15\n", encoding="utf-8")
            manager = isolated_runtime_manager()
            store = EnvConfigStore(env_path, runtime_manager=manager)
            before = manager.snapshot()

            with patch.object(manager, "commit", side_effect=RuntimeError("版本冲突")):
                with self.assertRaisesRegex(RuntimeError, "版本冲突"):
                    store.update({"REQUEST_TIMEOUT": 32})

            self.assertEqual(env_path.read_text(encoding="utf-8"), "# 原始配置\nREQUEST_TIMEOUT=15\n")
            self.assertEqual(manager.snapshot().revision, before.revision)

    def test_declared_multi_worker_process_disables_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = isolated_runtime_manager()
            before = manager.snapshot()
            with patch.dict(os.environ, {"WEB_CONCURRENCY": "2"}):
                store = EnvConfigStore(Path(temporary) / ".env", runtime_manager=manager)
                result = store.update({"REQUEST_TIMEOUT": 44})

            self.assertFalse(result["hotReloadAvailable"])
            self.assertIn("REQUEST_TIMEOUT", result["restartRequiredKeys"])
            self.assertEqual(manager.snapshot().revision, before.revision)
            self.assertEqual(manager.snapshot().get("request_timeout"), before.get("request_timeout"))

    def test_cross_field_validation_happens_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / ".env"
            manager = isolated_runtime_manager()
            store = EnvConfigStore(env_path, runtime_manager=manager)

            with self.assertRaisesRegex(ValueError, "最小分块字符数"):
                store.update({"SPLIT_MIN_LENGTH": 3000, "SPLIT_MAX_LENGTH": 2000})

            self.assertFalse(env_path.exists())


if __name__ == "__main__":
    unittest.main()
