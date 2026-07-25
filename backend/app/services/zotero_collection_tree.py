"""Zotero Collection 树的作用域计算与持久化。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIBRARY_ROOT_KEY = "__library_root__"
UNFILED_KEY = "__unfiled__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ZoteroCollectionTreeService:
    """只负责 Collection 层级计算，不读取网络或写入数据库。"""

    def build_scope(
        self,
        collections: list[dict[str, Any]],
        *,
        root_keys: list[str],
        include_subcollections: bool,
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        normalized: dict[str, dict[str, Any]] = {}
        for raw in collections:
            key = str(raw.get("key") or "").strip().upper()
            if not key:
                continue
            normalized[key] = {
                "key": key,
                "name": str(raw.get("name") or key).strip() or key,
                "parentCollection": str(raw.get("parentCollection") or "").strip().upper(),
                "version": int(raw.get("version") or 0),
            }
        self._validate_acyclic(normalized)
        selected_roots = list(dict.fromkeys(
            str(key).strip().upper() for key in root_keys if str(key).strip()
        ))
        whole_library = not selected_roots
        if whole_library:
            included = set(normalized)
        else:
            missing = [key for key in selected_roots if key not in normalized]
            if missing:
                raise ValueError(f"Zotero 分类不存在：{', '.join(missing[:10])}")
            included = set(selected_roots)
            if include_subcollections:
                children_by_parent: dict[str, list[str]] = {}
                for item in normalized.values():
                    children_by_parent.setdefault(item["parentCollection"], []).append(item["key"])
                pending = list(selected_roots)
                while pending:
                    parent = pending.pop()
                    for child in children_by_parent.get(parent, []):
                        if child not in included:
                            included.add(child)
                            pending.append(child)

        absolute_paths = {
            key: self._absolute_path(key, normalized)
            for key in included
        }
        nodes: list[dict[str, Any]] = []
        if whole_library:
            nodes.append({
                "key": LIBRARY_ROOT_KEY,
                "parentKey": "",
                "name": "Zotero 个人文库",
                "path": "Zotero 个人文库",
                "depth": 0,
                "version": 0,
                "isVirtual": True,
            })
        relative_depths: dict[str, int] = {}

        def relative_depth(key: str) -> int:
            if key in relative_depths:
                return relative_depths[key]
            parent = normalized[key]["parentCollection"]
            if whole_library:
                value = relative_depth(parent) + 1 if parent in included else 1
            else:
                value = relative_depth(parent) + 1 if parent in included else 0
            relative_depths[key] = value
            return value

        for key in sorted(included, key=lambda value: (relative_depth(value), absolute_paths[value], value)):
            item = normalized[key]
            parent = item["parentCollection"]
            nodes.append({
                "key": key,
                "parentKey": parent if parent in included else (LIBRARY_ROOT_KEY if whole_library else ""),
                "name": item["name"],
                "path": absolute_paths[key],
                "depth": relative_depth(key),
                "version": item["version"],
                "isVirtual": False,
            })
        if whole_library:
            nodes.append({
                "key": UNFILED_KEY,
                "parentKey": LIBRARY_ROOT_KEY,
                "name": "未分类文献",
                "path": "Zotero 个人文库 / 未分类文献",
                "depth": 1,
                "version": 0,
                "isVirtual": True,
            })
        return nodes, [
            node["key"] for node in nodes if not node["isVirtual"]
        ], whole_library

    @staticmethod
    def _absolute_path(key: str, collections: dict[str, dict[str, Any]]) -> str:
        names: list[str] = []
        current = key
        seen: set[str] = set()
        while current in collections and current not in seen:
            seen.add(current)
            item = collections[current]
            names.append(item["name"])
            current = item["parentCollection"]
        return " / ".join(reversed(names))

    @staticmethod
    def _validate_acyclic(collections: dict[str, dict[str, Any]]) -> None:
        for key in collections:
            current = key
            seen: set[str] = set()
            while current in collections:
                if current in seen:
                    raise ValueError(f"Zotero 分类层级存在循环：{key}")
                seen.add(current)
                current = collections[current]["parentCollection"]


class ZoteroCollectionRepository:
    """原子保存 Collection 树和文献多对多成员关系。"""

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
                CREATE TABLE IF NOT EXISTS zotero_collections (
                    source_id TEXT NOT NULL,
                    collection_key TEXT NOT NULL,
                    parent_collection_key TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    depth INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 0,
                    is_virtual INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, collection_key)
                );
                CREATE INDEX IF NOT EXISTS idx_zotero_collections_parent
                    ON zotero_collections(source_id, parent_collection_key, status);
                CREATE TABLE IF NOT EXISTS zotero_item_collections (
                    source_id TEXT NOT NULL,
                    zotero_item_key TEXT NOT NULL,
                    paper_id TEXT NOT NULL,
                    collection_key TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, zotero_item_key, collection_key)
                );
                CREATE INDEX IF NOT EXISTS idx_zotero_item_collections_node
                    ON zotero_item_collections(source_id, collection_key, paper_id);
                """,
            )
            connection.commit()

    def replace_snapshot(
        self,
        source_id: str,
        *,
        nodes: list[dict[str, Any]],
        memberships: dict[str, set[str]],
        paper_ids: dict[str, str],
    ) -> None:
        """在单个事务中替换完整作用域，失败时保留上一版可用树。"""
        timestamp = _now()
        active_keys = [str(node["key"]) for node in nodes]
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE zotero_collections SET status = 'missing', updated_at = ? WHERE source_id = ?",
                (timestamp, source_id),
            )
            connection.executemany(
                """
                INSERT INTO zotero_collections (
                    source_id, collection_key, parent_collection_key, name,
                    path, depth, version, is_virtual, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                ON CONFLICT(source_id, collection_key) DO UPDATE SET
                    parent_collection_key = excluded.parent_collection_key,
                    name = excluded.name,
                    path = excluded.path,
                    depth = excluded.depth,
                    version = excluded.version,
                    is_virtual = excluded.is_virtual,
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        source_id,
                        str(node["key"]),
                        str(node.get("parentKey") or ""),
                        str(node.get("name") or node["key"]),
                        str(node.get("path") or node["key"]),
                        int(node.get("depth") or 0),
                        int(node.get("version") or 0),
                        int(bool(node.get("isVirtual"))),
                        timestamp,
                    )
                    for node in nodes
                ],
            )
            connection.execute(
                "DELETE FROM zotero_item_collections WHERE source_id = ?",
                (source_id,),
            )
            connection.executemany(
                """
                INSERT INTO zotero_item_collections (
                    source_id, zotero_item_key, paper_id, collection_key, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (source_id, item_key, paper_ids[item_key], collection_key, timestamp)
                    for item_key, collection_keys in memberships.items()
                    for collection_key in sorted(collection_keys)
                    if collection_key in active_keys and item_key in paper_ids
                ],
            )
            connection.commit()

    def delete_source(self, source_id: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM zotero_item_collections WHERE source_id = ?", (source_id,))
            connection.execute("DELETE FROM zotero_collections WHERE source_id = ?", (source_id,))
            connection.commit()

    def list_project_trees(self, project_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            source_rows = connection.execute(
                """
                SELECT id FROM zotero_sources
                WHERE project_id = ? ORDER BY created_at, id
                """,
                (project_id,),
            ).fetchall()
        return [
            {"sourceId": str(row["id"]), "roots": self.list_tree(str(row["id"]))}
            for row in source_rows
        ]

    def list_tree(self, source_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM zotero_collections
                WHERE source_id = ? AND status = 'active'
                ORDER BY depth, path, collection_key
                """,
                (source_id,),
            ).fetchall()
            existing_papers = self._existing_paper_ids(connection)
            membership_rows = connection.execute(
                """
                SELECT collection_key, paper_id FROM zotero_item_collections
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchall()
        direct: dict[str, set[str]] = {}
        for row in membership_rows:
            paper_id = str(row["paper_id"])
            if paper_id in existing_papers:
                direct.setdefault(str(row["collection_key"]), set()).add(paper_id)
        nodes = {
            str(row["collection_key"]): {
                "sourceId": source_id,
                "key": str(row["collection_key"]),
                "parentKey": str(row["parent_collection_key"]),
                "name": str(row["name"]),
                "path": str(row["path"]),
                "depth": int(row["depth"]),
                "version": int(row["version"]),
                "isVirtual": bool(row["is_virtual"]),
                "directPaperCount": len(direct.get(str(row["collection_key"]), set())),
                "paperCount": 0,
                "children": [],
            }
            for row in rows
        }
        roots: list[dict[str, Any]] = []
        for node in nodes.values():
            parent = nodes.get(node["parentKey"])
            if parent:
                parent["children"].append(node)
            else:
                roots.append(node)

        def fill(node: dict[str, Any]) -> set[str]:
            paper_ids = set(direct.get(node["key"], set()))
            node["children"].sort(key=lambda item: (item["name"], item["key"]))
            for child in node["children"]:
                paper_ids.update(fill(child))
            node["paperCount"] = len(paper_ids)
            return paper_ids

        roots.sort(key=lambda item: (item["name"], item["key"]))
        for root in roots:
            fill(root)
        return roots

    def list_paper_ids(
        self,
        source_id: str,
        collection_key: str,
        *,
        include_descendants: bool = True,
    ) -> list[str]:
        keys = {collection_key}
        if include_descendants:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT collection_key, parent_collection_key
                    FROM zotero_collections
                    WHERE source_id = ? AND status = 'active'
                    """,
                    (source_id,),
                ).fetchall()
            children: dict[str, list[str]] = {}
            for row in rows:
                children.setdefault(str(row["parent_collection_key"]), []).append(str(row["collection_key"]))
            pending = [collection_key]
            while pending:
                parent = pending.pop()
                for child in children.get(parent, []):
                    if child not in keys:
                        keys.add(child)
                        pending.append(child)
        placeholders = ",".join("?" for _ in keys)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT paper_id FROM zotero_item_collections
                WHERE source_id = ? AND collection_key IN ({placeholders})
                ORDER BY paper_id
                """,
                [source_id, *sorted(keys)],
            ).fetchall()
        return [str(row["paper_id"]) for row in rows]

    @staticmethod
    def _existing_paper_ids(connection: sqlite3.Connection) -> set[str]:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'papers'",
        ).fetchone()
        if not table:
            return set()
        return {str(row["id"]) for row in connection.execute("SELECT id FROM papers").fetchall()}


__all__ = [
    "LIBRARY_ROOT_KEY",
    "UNFILED_KEY",
    "ZoteroCollectionRepository",
    "ZoteroCollectionTreeService",
]
