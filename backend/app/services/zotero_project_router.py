"""将 Zotero 数据源路由到独立的同名研究项目。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.project_repository import ProjectRepository
from app.services.zotero_connector import ZoteroConnector
from app.services.zotero_source_repository import ZoteroSourceRepository


AUTO_PROJECT_PREFIX = "由 Zotero"


class ZoteroProjectRouter:
    """集中处理历史数据源迁移，避免 API 与同步任务各自猜测目标项目。"""

    def __init__(self, metadata_db_path: str | Path) -> None:
        self.projects = ProjectRepository(metadata_db_path)
        self.sources = ZoteroSourceRepository(metadata_db_path)

    def ensure_routed(
        self,
        connector: ZoteroConnector,
        source: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        current_project = self.projects.require(source["projectId"])
        if str(current_project.get("description") or "").startswith(AUTO_PROJECT_PREFIX):
            return source, current_project, False

        collection_keys = [str(key).upper() for key in source.get("collectionKeys") or []]
        collections = connector.list_collections() if collection_keys else []
        names_by_key = {
            str(collection.get("key") or "").upper(): str(collection.get("name") or "").strip()
            for collection in collections
        }
        names = [names_by_key.get(key) or key for key in collection_keys]
        if not names:
            project_name = "Zotero 个人文库"
            description = "由 Zotero 个人文库自动创建"
        elif len(names) == 1:
            project_name = names[0]
            description = f"由 Zotero 分类 {collection_keys[0]} 自动创建"
        else:
            project_name = f"{names[0]} 等 {len(names)} 个 Zotero 分类"
            description = "由 Zotero 多分类数据源自动创建"

        target_project = self.projects.create(name=project_name, description=description)
        moved_source = self.sources.move_to_project(source["id"], target_project["id"])
        return moved_source, target_project, True


__all__ = ["AUTO_PROJECT_PREFIX", "ZoteroProjectRouter"]
