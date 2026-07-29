"""项目文献地图查询、构建和任务控制接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.schemas.api import LiteratureMapBuildOptions
from app.services.background_jobs import (
    BackgroundJobCapacityExceeded,
    background_job_manager,
)
from app.services.literature_map import (
    LiteratureMapProjectService,
    LiteratureMapRepository,
    VocabularyNormalizer,
)
from app.services.model_config import ModelConfigStore
from app.services.paper_repository import PaperRepository
from app.services.project_repository import ProjectNotFoundError, ProjectRepository


router = APIRouter()


def _service() -> LiteratureMapProjectService:
    return LiteratureMapProjectService(
        projects=ProjectRepository(settings.hunter_metadata_db),
        papers=PaperRepository(settings.hunter_metadata_db),
        repository=LiteratureMapRepository(settings.literature_map_db),
        extractor_version=settings.literature_map_extractor_version,
        normalizer=VocabularyNormalizer.from_file(
            settings.literature_map_normalization_config
        ),
    )


def _require_project_job(project_id: str, job_id: str) -> dict:
    job = background_job_manager.get(job_id)
    request = (job or {}).get("request") or {}
    if (
        not job
        or job.get("type") != "literature_map"
        or str(request.get("project_id") or "") != project_id
    ):
        raise HTTPException(status_code=404, detail="当前项目中不存在该文献地图任务")
    return job


@router.get("/api/projects/{project_id}/literature-map")
def get_project_literature_map(project_id: str) -> dict:
    try:
        snapshot = _service().snapshot(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    active_job = background_job_manager.find_active(
        "literature_map",
        f"literature-map:{project_id}",
    )
    if active_job:
        return {
            **snapshot,
            "status": "building",
            "jobId": active_job["jobId"],
            "jobProgress": active_job["progress"],
            "jobStage": active_job["stage"],
        }
    return snapshot


@router.post(
    "/api/projects/{project_id}/literature-map/build",
    status_code=202,
)
def build_project_literature_map(
    project_id: str,
    payload: LiteratureMapBuildOptions,
) -> dict:
    try:
        snapshot = _service().snapshot(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if int(snapshot.get("sourcePaperCount") or 0) == 0:
        raise HTTPException(
            status_code=400,
            detail="当前项目没有可用于构建文献地图的 Markdown 文献",
        )
    if not ModelConfigStore().build_model_payload():
        raise HTTPException(status_code=400, detail="请先配置模型参数")
    try:
        job, created = background_job_manager.submit(
            "literature_map",
            {"project_id": project_id, "force": payload.force},
            dedupe_key=f"literature-map:{project_id}",
        )
    except BackgroundJobCapacityExceeded as error:
        raise HTTPException(
            status_code=503,
            detail=str(error),
            headers={"Retry-After": "1"},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        **job,
        "created": created,
        "jobStatus": job.get("status"),
        "status": "building",
    }


@router.get("/api/projects/{project_id}/literature-map/jobs/active")
def get_active_literature_map_job(project_id: str) -> dict:
    try:
        _service().projects.require(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    job = background_job_manager.find_active(
        "literature_map",
        f"literature-map:{project_id}",
    )
    if not job:
        raise HTTPException(status_code=404, detail="当前项目没有活动的文献地图任务")
    return job


@router.get("/api/projects/{project_id}/literature-map/jobs/{job_id}")
def get_literature_map_job(project_id: str, job_id: str) -> dict:
    return _require_project_job(project_id, job_id)


@router.post("/api/projects/{project_id}/literature-map/jobs/{job_id}/cancel")
def cancel_literature_map_job(project_id: str, job_id: str) -> dict:
    _require_project_job(project_id, job_id)
    job = background_job_manager.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="文献地图任务不存在")
    return job


__all__ = ["router"]
