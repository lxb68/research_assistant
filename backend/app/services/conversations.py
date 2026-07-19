"""研究对话的 SQLite 持久化存储。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.core.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


class ConversationStore:
    """只负责会话与消息持久化，不参与任务调度。"""

    def __init__(self, path: str | Path = settings.conversation_db) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_session_updated
                    ON conversations(session_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    context_sources_json TEXT NOT NULL DEFAULT '[]',
                    response_mode TEXT,
                    job_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, id),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_messages_order
                    ON conversation_messages(conversation_id, created_at);
                """,
            )

    def ensure_conversation(self, conversation_id: str, *, title: str, session_id: str = "local") -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations (id, session_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = CASE WHEN conversations.title = '' THEN excluded.title ELSE conversations.title END,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, session_id, title, timestamp, timestamp),
            )

    def upsert_message(
        self,
        conversation_id: str,
        message_id: str,
        *,
        role: str,
        content: str,
        sources: list[dict[str, Any]] | None = None,
        context_sources: list[dict[str, Any]] | None = None,
        response_mode: str | None = None,
        job_id: str | None = None,
    ) -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_messages (
                    id, conversation_id, role, content, sources_json,
                    context_sources_json, response_mode, job_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, id) DO UPDATE SET
                    role = excluded.role,
                    content = excluded.content,
                    sources_json = excluded.sources_json,
                    context_sources_json = excluded.context_sources_json,
                    response_mode = excluded.response_mode,
                    job_id = excluded.job_id
                """,
                (
                    message_id,
                    conversation_id,
                    role,
                    content,
                    json.dumps(sources or [], ensure_ascii=False),
                    json.dumps(context_sources or [], ensure_ascii=False),
                    response_mode,
                    job_id,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (timestamp, conversation_id),
            )

    def list(self, *, session_id: str = "local", limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?",
                (session_id, max(1, min(limit, 500))),
            ).fetchall()
            return [self._public(connection, row) for row in rows]

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            return self._public(connection, row) if row else None

    def rename(self, conversation_id: str, title: str) -> bool:
        """更新会话标题；返回会话是否存在。"""
        timestamp = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, timestamp, conversation_id),
            )
            return cursor.rowcount > 0

    def _public(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        messages = connection.execute(
            "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "title": row["title"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "messages": [
                {
                    "id": message["id"],
                    "role": message["role"],
                    "content": message["content"],
                    "sources": _loads(message["sources_json"], []),
                    "contextSources": _loads(message["context_sources_json"], []),
                    "responseMode": message["response_mode"],
                    "jobId": message["job_id"],
                    "createdAt": message["created_at"],
                }
                for message in messages
            ],
        }


conversation_store = ConversationStore()


__all__ = ["ConversationStore", "conversation_store"]
