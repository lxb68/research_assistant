"""持久化外部文档解析任务，支持幂等、重试和服务重启恢复。"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentParseRepository:
    """维护与具体文献来源解耦的解析任务状态。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def ensure_schema(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_parse_tasks (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_item_key TEXT NOT NULL,
                    attachment_key TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    parse_key TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL DEFAULT 'mineru',
                    provider_batch_id TEXT NOT NULL DEFAULT '',
                    provider_data_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'discovered',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_url TEXT NOT NULL DEFAULT '',
                    output_dir TEXT NOT NULL DEFAULT '',
                    markdown_path TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_document_parse_tasks_source
                    ON document_parse_tasks(source_id, source_item_key);
                CREATE INDEX IF NOT EXISTS idx_document_parse_tasks_batch
                    ON document_parse_tasks(provider, provider_batch_id, status);
                """,
            )
            connection.commit()

    def get_by_parse_key(self, parse_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM document_parse_tasks WHERE parse_key = ?",
                (parse_key,),
            ).fetchone()
        return dict(row) if row else None

    def create_or_get(
        self,
        *,
        source_id: str,
        source_item_key: str,
        attachment_key: str,
        file_hash: str,
        parse_key: str,
        data_id: str,
    ) -> dict[str, Any]:
        timestamp = _now()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO document_parse_tasks (
                    id, source_id, source_item_key, attachment_key, file_hash,
                    parse_key, provider_data_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                """,
                (
                    f"parse-{uuid.uuid4().hex}", source_id, source_item_key,
                    attachment_key, file_hash, parse_key, data_id, timestamp, timestamp,
                ),
            )
            connection.commit()
        task = self.get_by_parse_key(parse_key)
        if not task:
            raise RuntimeError("创建文档解析任务失败")
        return task

    def update(self, parse_key: str, *, status: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "provider_batch_id", "provider_data_id", "attempts", "result_url",
            "output_dir", "markdown_path", "error_message",
        }
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, _now()]
        for key, value in values.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            params.append(value)
        params.append(parse_key)
        with closing(self.connect()) as connection:
            connection.execute(
                f"UPDATE document_parse_tasks SET {', '.join(assignments)} WHERE parse_key = ?",
                params,
            )
            connection.commit()
        task = self.get_by_parse_key(parse_key)
        if not task:
            raise LookupError(f"解析任务不存在：{parse_key}")
        return task

    def delete_source(self, source_id: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM document_parse_tasks WHERE source_id = ?", (source_id,))
            connection.commit()


__all__ = ["DocumentParseRepository"]
