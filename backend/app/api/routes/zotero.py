"""Zotero Local API 连接测试、分类浏览和项目数据源管理。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.document_parse_repository import DocumentParseRepository
from app.services.project_repository import ProjectNotFoundError, ProjectRepository
from app.services.zotero_connector import ZoteroConnectionError, ZoteroConnector
from app.services.zotero_source_repository import ZoteroSourceRepository


router = APIRouter()


class ZoteroConnectionRequest(BaseModel):
    api_base_url: str = Field("http://127.0.0.1:23119/api", max_length=500)
    library_type: str = Field("users", max_length=20)
    library_id: str = Field("0", max_length=50)


class ZoteroSourceCreateRequest(ZoteroConnectionRequest):
    collection_keys: list[str] = Field(default_factory=list, max_length=500)
    include_subcollections: bool = True
    include_standalone_attachments: bool = False
    create_collection_projects: bool = False


def _connector(payload: ZoteroConnectionRequest) -> ZoteroConnector:
    return ZoteroConnector(
        base_url=payload.api_base_url,
        library_type=payload.library_type,
        library_id=payload.library_id,
    )


@router.post("/api/zotero/connection")
def test_zotero_connection(payload: ZoteroConnectionRequest) -> dict:
    try:
        connector = _connector(payload)
        return connector.test_connection()
    except (ValueError, ZoteroConnectionError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/zotero/collections")
def list_zotero_collections(payload: ZoteroConnectionRequest) -> dict:
    try:
        values = _connector(payload).list_collections()
        return {"count": len(values), "collections": values}
    except (ValueError, ZoteroConnectionError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/api/zotero/sources")
def list_zotero_sources(project_id: str | None = Query(None, alias="projectId")) -> dict:
    values = ZoteroSourceRepository(settings.hunter_metadata_db).list(project_id=project_id)
    return {"count": len(values), "sources": values}


@router.post("/api/projects/{project_id}/zotero-sources", status_code=201)
def create_zotero_source(project_id: str, payload: ZoteroSourceCreateRequest) -> dict:
    try:
        projects = ProjectRepository(settings.hunter_metadata_db)
        projects.require(project_id)
        connector = _connector(payload)
        collections = connector.list_collections()
        available = {str(item.get("key") or ""): item for item in collections}
        normalized_keys = list(dict.fromkeys(key.strip().upper() for key in payload.collection_keys if key.strip()))
        missing = [key for key in normalized_keys if key not in available]
        if missing:
            raise ValueError(f"Zotero 分类不存在：{', '.join(missing[:10])}")
        sources = ZoteroSourceRepository(settings.hunter_metadata_db)

        if not payload.create_collection_projects:
            source = sources.create(
                project_id=project_id,
                api_base_url=connector.base_url,
                library_type=connector.library_type,
                library_id=connector.library_id,
                collection_keys=normalized_keys,
                include_subcollections=payload.include_subcollections,
                include_standalone_attachments=payload.include_standalone_attachments,
            )
            return {"status": "ok", "source": source, "sources": [source], "projects": []}

        scopes = [[key] for key in normalized_keys] or [[]]
        created_sources: list[dict] = []
        target_projects: list[dict] = []
        created_project_ids: list[str] = []
        for scope_keys in scopes:
            existing = sources.find_exact_scope(
                library_type=connector.library_type,
                library_id=connector.library_id,
                collection_keys=scope_keys,
            )
            if existing:
                existing_project = projects.require(existing["projectId"])
                if str(existing_project.get("description") or "").startswith("由 Zotero"):
                    created_sources.append(existing)
                    target_projects.append(existing_project)
                    continue

            collection = available.get(scope_keys[0]) if scope_keys else None
            project_name = str((collection or {}).get("name") or "Zotero 个人文库").strip()
            target_project = projects.create(
                name=project_name,
                description=(
                    f"由 Zotero 分类 {scope_keys[0]} 自动创建"
                    if scope_keys else "由 Zotero 个人文库自动创建"
                ),
            )
            if existing:
                source = sources.move_to_project(existing["id"], target_project["id"])
                created_sources.append(source)
                target_projects.append(target_project)
                created_project_ids.append(target_project["id"])
                continue
            try:
                source = sources.create(
                    project_id=target_project["id"],
                    api_base_url=connector.base_url,
                    library_type=connector.library_type,
                    library_id=connector.library_id,
                    collection_keys=scope_keys,
                    include_subcollections=payload.include_subcollections,
                    include_standalone_attachments=payload.include_standalone_attachments,
                )
            except Exception:
                # 项目创建成功但数据源落库失败时不隐藏异常；空项目保留，便于用户检查和重试。
                raise
            created_sources.append(source)
            target_projects.append(target_project)
            created_project_ids.append(target_project["id"])

        return {
            "status": "ok",
            "source": created_sources[0],
            "sources": created_sources,
            "projects": target_projects,
            "createdProjectIds": created_project_ids,
        }
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, ZoteroConnectionError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/api/zotero/sources/{source_id}")
def delete_zotero_source(source_id: str) -> dict:
    deleted = ZoteroSourceRepository(settings.hunter_metadata_db).delete(source_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Zotero 数据源不存在")
    DocumentParseRepository(settings.hunter_metadata_db).delete_source(source_id)
    return {"status": "ok", "deleted": True}


__all__ = ["router"]
