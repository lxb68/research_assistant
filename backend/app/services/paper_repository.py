"""SQLite persistence boundary for paper metadata."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


PAPER_COLUMNS = (
    "id, source, title, doi, external_id, url, pdf_url, pdf_path, keyword, "
    "relevance_score, metadata_json, saved_at"
)


class PaperRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def ensure_schema(self) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '', doi TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                    pdf_url TEXT NOT NULL DEFAULT '', pdf_path TEXT NOT NULL DEFAULT '',
                    keyword TEXT NOT NULL DEFAULT '', relevance_score REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL, saved_at TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            connection.commit()

    def save(self, record: dict[str, Any]) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO papers (
                    id, source, title, doi, external_id, url, pdf_url, pdf_path,
                    keyword, relevance_score, metadata_json, saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source=excluded.source, title=excluded.title, doi=excluded.doi,
                    external_id=excluded.external_id, url=excluded.url,
                    pdf_url=excluded.pdf_url, pdf_path=excluded.pdf_path,
                    keyword=excluded.keyword, relevance_score=excluded.relevance_score,
                    metadata_json=excluded.metadata_json, saved_at=excluded.saved_at
                """,
                (
                    record["id"], record.get("source", ""), record.get("title", ""), record.get("doi", ""),
                    record.get("externalId") or record.get("external_id") or "", record.get("url", ""),
                    record.get("pdfUrl") or record.get("pdf_url") or "", record.get("pdfPath", ""),
                    record.get("keyword", ""), record.get("relevanceScore", 0),
                    json.dumps(record, ensure_ascii=False), record.get("savedAt", ""),
                ),
            )
            connection.commit()

    def list(self, *, limit: int, keyword: str | None = None) -> list[dict[str, Any]]:
        query = f"SELECT {PAPER_COLUMNS} FROM papers"
        params: list[object] = []
        if keyword:
            query += " WHERE keyword LIKE ? OR title LIKE ?"
            pattern = f"%{keyword}%"
            params.extend([pattern, pattern])
        query += " ORDER BY saved_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with closing(self.connect()) as connection:
            return [self._row_to_record(row) for row in connection.execute(query, params).fetchall()]

    def count(self, *, keyword: str | None = None) -> int:
        """统计文献数量，可使用与列表一致的关键词过滤。"""
        query = "SELECT COUNT(*) FROM papers"
        params: list[object] = []
        if keyword:
            query += " WHERE keyword LIKE ? OR title LIKE ?"
            pattern = f"%{keyword}%"
            params.extend([pattern, pattern])
        with closing(self.connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return int(row[0] if row else 0)

    def find(self, *, record_id: str | None = None, doi: str | None = None, title: str | None = None) -> dict[str, Any] | None:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (("id", record_id), ("doi", doi), ("title", title)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if not clauses:
            return None
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"SELECT {PAPER_COLUMNS} FROM papers WHERE {' OR '.join(clauses)} LIMIT 1",
                params,
            ).fetchone()
        return self._row_to_record(row) if row else None

    def delete_rows(self, ids: list[str]) -> None:
        normalized = [value.strip() for value in ids if value.strip()]
        if not normalized:
            return
        placeholders = ", ".join("?" for _ in normalized)
        with closing(self.connect()) as connection:
            connection.execute(f"DELETE FROM papers WHERE id IN ({placeholders})", normalized)
            connection.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return {
            **metadata, "id": row["id"], "source": row["source"], "title": row["title"],
            "doi": row["doi"], "externalId": row["external_id"], "url": row["url"],
            "pdfUrl": row["pdf_url"], "pdfPath": row["pdf_path"], "keyword": row["keyword"],
            "relevanceScore": row["relevance_score"], "savedAt": row["saved_at"],
        }


__all__ = ["PaperRepository"]
