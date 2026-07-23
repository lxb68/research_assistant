"""Zotero 数据源及增量同步游标的 SQLite 持久化。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ZoteroSourceNotFoundError(LookupError):
    """指定的 Zotero 数据源不存在。"""


class ZoteroSourceRepository:
    """维护项目 Zotero 数据源和逐条目同步状态。"""

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
                CREATE TABLE IF NOT EXISTS zotero_sources (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    api_base_url TEXT NOT NULL,
                    library_type TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    collection_keys_json TEXT NOT NULL DEFAULT '[]',
                    include_subcollections INTEGER NOT NULL DEFAULT 1,
                    include_standalone_attachments INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_synced_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_zotero_sources_project
                    ON zotero_sources(project_id);
                CREATE TABLE IF NOT EXISTS zotero_source_items (
                    source_id TEXT NOT NULL,
                    zotero_item_key TEXT NOT NULL,
                    item_version INTEGER NOT NULL DEFAULT 0,
                    attachment_key TEXT NOT NULL DEFAULT '',
                    attachment_version INTEGER NOT NULL DEFAULT 0,
                    file_path TEXT NOT NULL DEFAULT '',
                    file_hash TEXT NOT NULL DEFAULT '',
                    paper_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, zotero_item_key),
                    FOREIGN KEY (source_id) REFERENCES zotero_sources(id) ON DELETE CASCADE
                );
                """,
            )
            connection.commit()

    def create(
        self,
        *,
        project_id: str,
        api_base_url: str,
        library_type: str,
        library_id: str,
        collection_keys: list[str],
        include_subcollections: bool = True,
        include_standalone_attachments: bool = False,
    ) -> dict[str, Any]:
        source_id = f"zotero-{uuid.uuid4().hex}"
        timestamp = _now()
        normalized_keys = list(dict.fromkeys(str(key).strip().upper() for key in collection_keys if str(key).strip()))
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO zotero_sources (
                    id, project_id, api_base_url, library_type, library_id,
                    collection_keys_json, include_subcollections,
                    include_standalone_attachments, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)
                """,
                (
                    source_id, project_id, api_base_url, library_type, library_id,
                    json.dumps(normalized_keys), int(include_subcollections),
                    int(include_standalone_attachments), timestamp, timestamp,
                ),
            )
            connection.commit()
        return self.require(source_id)

    def list(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM zotero_sources"
        params: list[str] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " ORDER BY created_at DESC"
        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._source_row(row) for row in rows]

    def get(self, source_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM zotero_sources WHERE id = ?", (source_id,)).fetchone()
        return self._source_row(row) if row else None

    def find_exact_scope(
        self,
        *,
        library_type: str,
        library_id: str,
        collection_keys: list[str],
    ) -> dict[str, Any] | None:
        """查找同一 Zotero 文库和完全相同分类范围的数据源，避免重复创建项目。"""
        normalized_keys = list(
            dict.fromkeys(str(key).strip().upper() for key in collection_keys if str(key).strip())
        )
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM zotero_sources
                WHERE library_type = ? AND library_id = ? AND collection_keys_json = ?
                ORDER BY created_at LIMIT 1
                """,
                (library_type, library_id, json.dumps(normalized_keys)),
            ).fetchone()
        return self._source_row(row) if row else None

    def require(self, source_id: str) -> dict[str, Any]:
        source = self.get(str(source_id or "").strip())
        if not source:
            raise ZoteroSourceNotFoundError("Zotero 数据源不存在")
        return source

    def delete(self, source_id: str) -> bool:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM zotero_source_items WHERE source_id = ?", (source_id,))
            cursor = connection.execute("DELETE FROM zotero_sources WHERE id = ?", (source_id,))
            deleted = cursor.rowcount > 0
            connection.commit()
        return deleted

    def set_source_status(self, source_id: str, *, status: str, error: str = "", synced: bool = False) -> None:
        timestamp = _now()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE zotero_sources SET status = ?, last_error = ?,
                    last_synced_at = CASE WHEN ? THEN ? ELSE last_synced_at END,
                    updated_at = ? WHERE id = ?
                """,
                (status, error[:2000], int(synced), timestamp, timestamp, source_id),
            )
            connection.commit()

    def move_to_project(self, source_id: str, project_id: str) -> dict[str, Any]:
        """将已有数据源迁移到新的目标项目，保留增量游标和解析状态。"""
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "UPDATE zotero_sources SET project_id = ?, updated_at = ? WHERE id = ?",
                (project_id, _now(), source_id),
            )
            connection.commit()
        if cursor.rowcount <= 0:
            raise ZoteroSourceNotFoundError("Zotero 数据源不存在")
        return self.require(source_id)

    def get_item(self, source_id: str, item_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM zotero_source_items WHERE source_id = ? AND zotero_item_key = ?",
                (source_id, item_key),
            ).fetchone()
        return dict(row) if row else None

    def upsert_item(self, source_id: str, item_key: str, **values: Any) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO zotero_source_items (
                    source_id, zotero_item_key, item_version, attachment_key,
                    attachment_version, file_path, file_hash, paper_id,
                    status, error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, zotero_item_key) DO UPDATE SET
                    item_version=excluded.item_version,
                    attachment_key=excluded.attachment_key,
                    attachment_version=excluded.attachment_version,
                    file_path=excluded.file_path,
                    file_hash=excluded.file_hash,
                    paper_id=excluded.paper_id,
                    status=excluded.status,
                    error_message=excluded.error_message,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id, item_key, int(values.get("item_version") or 0),
                    str(values.get("attachment_key") or ""), int(values.get("attachment_version") or 0),
                    str(values.get("file_path") or ""), str(values.get("file_hash") or ""),
                    str(values.get("paper_id") or ""), str(values.get("status") or "pending"),
                    str(values.get("error_message") or "")[:2000], _now(),
                ),
            )
            connection.commit()

    def mark_missing_except(self, source_id: str, item_keys: list[str]) -> int:
        with closing(self.connect()) as connection:
            if item_keys:
                placeholders = ",".join("?" for _ in item_keys)
                cursor = connection.execute(
                    f"UPDATE zotero_source_items SET status = 'missing', updated_at = ? "
                    f"WHERE source_id = ? AND zotero_item_key NOT IN ({placeholders}) AND status != 'missing'",
                    [_now(), source_id, *item_keys],
                )
            else:
                cursor = connection.execute(
                    "UPDATE zotero_source_items SET status = 'missing', updated_at = ? "
                    "WHERE source_id = ? AND status != 'missing'",
                    (_now(), source_id),
                )
            connection.commit()
        return max(0, int(cursor.rowcount))

    @staticmethod
    def _source_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            collection_keys = json.loads(row["collection_keys_json"] or "[]")
        except json.JSONDecodeError:
            collection_keys = []
        return {
            "id": str(row["id"]),
            "projectId": str(row["project_id"]),
            "apiBaseUrl": str(row["api_base_url"]),
            "libraryType": str(row["library_type"]),
            "libraryId": str(row["library_id"]),
            "collectionKeys": collection_keys if isinstance(collection_keys, list) else [],
            "includeSubcollections": bool(row["include_subcollections"]),
            "includeStandaloneAttachments": bool(row["include_standalone_attachments"]),
            "status": str(row["status"]),
            "lastError": str(row["last_error"]),
            "lastSyncedAt": str(row["last_synced_at"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }


__all__ = ["ZoteroSourceNotFoundError", "ZoteroSourceRepository"]
