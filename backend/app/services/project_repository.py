"""项目及项目论文成员关系的 SQLite 持久化边界。"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PROJECT_ID = "workspace-domain-tree"
DEFAULT_PROJECT_NAME = "默认研究项目"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectNotFoundError(LookupError):
    """请求的项目不存在。"""


class ProjectPaperNotFoundError(ValueError):
    """项目成员列表包含不存在的论文。"""


class ProjectRepository:
    """集中维护项目元数据和多对多论文成员关系。"""

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
        now = _timestamp()
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_papers (
                    project_id TEXT NOT NULL,
                    paper_id TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, paper_id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_project_papers_paper
                    ON project_papers(paper_id);
                """,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO projects (
                    id, name, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (
                    DEFAULT_PROJECT_ID,
                    DEFAULT_PROJECT_NAME,
                    "兼容升级前全局工作区的默认项目",
                    now,
                    now,
                ),
            )
            connection.commit()
        self._sync_default_project_members()

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        self._sync_default_project_members()
        condition = "" if include_archived else "WHERE p.status = 'active'"
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT p.*, COUNT(pp.paper_id) AS paper_count
                FROM projects p
                LEFT JOIN project_papers pp ON pp.project_id = p.id
                {condition}
                GROUP BY p.id
                ORDER BY CASE WHEN p.id = ? THEN 0 ELSE 1 END, p.created_at
                """,
                (DEFAULT_PROJECT_ID,),
            ).fetchall()
        return [self._row_to_project(row) for row in rows]

    def get(self, project_id: str) -> dict[str, Any] | None:
        normalized = str(project_id or "").strip()
        if not normalized:
            return None
        if normalized == DEFAULT_PROJECT_ID:
            self._sync_default_project_members()
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT p.*, COUNT(pp.paper_id) AS paper_count
                FROM projects p
                LEFT JOIN project_papers pp ON pp.project_id = p.id
                WHERE p.id = ?
                GROUP BY p.id
                """,
                (normalized,),
            ).fetchone()
        return self._row_to_project(row) if row else None

    def require(self, project_id: str) -> dict[str, Any]:
        project = self.get(project_id)
        if not project or project["status"] != "active":
            raise ProjectNotFoundError("项目不存在或已归档")
        return project

    def create(
        self,
        *,
        name: str,
        description: str = "",
        paper_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("项目名称不能为空")
        project_id = f"project-{uuid.uuid4().hex}"
        now = _timestamp()
        normalized_paper_ids = list(
            dict.fromkeys(str(value).strip() for value in paper_ids or [] if str(value).strip())
        )
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            available = self._existing_paper_ids(connection, normalized_paper_ids)
            missing = [paper_id for paper_id in normalized_paper_ids if paper_id not in available]
            if missing:
                connection.rollback()
                raise ProjectPaperNotFoundError(f"以下论文不存在：{', '.join(missing[:10])}")
            connection.execute(
                """
                INSERT INTO projects (id, name, description, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (project_id, normalized_name[:200], str(description or "").strip()[:2000], now, now),
            )
            connection.executemany(
                "INSERT INTO project_papers (project_id, paper_id, added_at) VALUES (?, ?, ?)",
                [(project_id, paper_id, now) for paper_id in normalized_paper_ids],
            )
            connection.commit()
        return self.require(project_id)

    def list_paper_ids(self, project_id: str) -> list[str]:
        self.require(project_id)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT paper_id FROM project_papers
                WHERE project_id = ? ORDER BY added_at, paper_id
                """,
                (project_id,),
            ).fetchall()
        return [str(row["paper_id"]) for row in rows]

    def replace_papers(self, project_id: str, paper_ids: list[str]) -> dict[str, Any]:
        self.require(project_id)
        normalized = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        now = _timestamp()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            available = self._existing_paper_ids(connection, normalized)
            missing = [paper_id for paper_id in normalized if paper_id not in available]
            if missing:
                connection.rollback()
                raise ProjectPaperNotFoundError(f"以下论文不存在：{', '.join(missing[:10])}")
            connection.execute("DELETE FROM project_papers WHERE project_id = ?", (project_id,))
            connection.executemany(
                "INSERT INTO project_papers (project_id, paper_id, added_at) VALUES (?, ?, ?)",
                [(project_id, paper_id, now) for paper_id in normalized],
            )
            connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            connection.commit()
        return self.require(project_id)

    def add_papers(self, project_id: str, paper_ids: list[str]) -> dict[str, Any]:
        """增量加入论文，避免同步任务用全量替换覆盖用户正在编辑的成员关系。"""
        self.require(project_id)
        normalized = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not normalized:
            return self.require(project_id)
        now = _timestamp()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            available = self._existing_paper_ids(connection, normalized)
            missing = [paper_id for paper_id in normalized if paper_id not in available]
            if missing:
                connection.rollback()
                raise ProjectPaperNotFoundError(f"以下论文不存在：{', '.join(missing[:10])}")
            connection.executemany(
                "INSERT OR IGNORE INTO project_papers (project_id, paper_id, added_at) VALUES (?, ?, ?)",
                [(project_id, paper_id, now) for paper_id in normalized],
            )
            connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            connection.commit()
        return self.require(project_id)

    def remove_paper_references(self, paper_ids: list[str]) -> int:
        """移除已删除论文的全部项目成员关系，并刷新受影响项目的更新时间。"""
        normalized = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not normalized:
            return 0
        placeholders = ", ".join("?" for _ in normalized)
        now = _timestamp()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            project_rows = connection.execute(
                f"SELECT DISTINCT project_id FROM project_papers WHERE paper_id IN ({placeholders})",
                normalized,
            ).fetchall()
            cursor = connection.execute(
                f"DELETE FROM project_papers WHERE paper_id IN ({placeholders})",
                normalized,
            )
            removed_count = max(0, int(cursor.rowcount))
            cursor.close()
            project_ids = [str(row["project_id"]) for row in project_rows]
            if project_ids:
                project_placeholders = ", ".join("?" for _ in project_ids)
                connection.execute(
                    f"UPDATE projects SET updated_at = ? WHERE id IN ({project_placeholders})",
                    [now, *project_ids],
                )
            connection.commit()
        return removed_count

    def _sync_default_project_members(self) -> None:
        """默认项目承接普通全局论文，但排除已由独立项目管理的 Zotero 文献。"""
        now = _timestamp()
        with closing(self.connect()) as connection:
            has_papers = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'papers'",
            ).fetchone()
            if not has_papers:
                return
            connection.execute(
                """
                DELETE FROM project_papers
                WHERE project_id = ? AND paper_id IN (
                    SELECT id FROM papers WHERE LOWER(source) = 'zotero'
                )
                """,
                (DEFAULT_PROJECT_ID,),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO project_papers (project_id, paper_id, added_at)
                SELECT ?, id, ? FROM papers WHERE LOWER(source) != 'zotero'
                """,
                (DEFAULT_PROJECT_ID, now),
            )
            connection.commit()

    @staticmethod
    def _existing_paper_ids(connection: sqlite3.Connection, paper_ids: list[str]) -> set[str]:
        if not paper_ids:
            return set()
        has_papers = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'papers'",
        ).fetchone()
        if not has_papers:
            return set()
        placeholders = ", ".join("?" for _ in paper_ids)
        rows = connection.execute(
            f"SELECT id FROM papers WHERE id IN ({placeholders})",
            paper_ids,
        ).fetchall()
        return {str(row["id"]) for row in rows}

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "description": str(row["description"]),
            "status": str(row["status"]),
            "paperCount": int(row["paper_count"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }


__all__ = [
    "DEFAULT_PROJECT_ID",
    "DEFAULT_PROJECT_NAME",
    "ProjectNotFoundError",
    "ProjectPaperNotFoundError",
    "ProjectRepository",
]
