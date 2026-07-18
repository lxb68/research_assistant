"""Domain tree and knowledge graph job routes."""

from fastapi import APIRouter, HTTPException

from app.agents import DomainTreeAgent
from app.core.config import settings
from app.schemas.api import DomainTreeGenerateOptions, DomainTreeGenerateRequest
from app.services.background_jobs import BackgroundJobCapacityExceeded, background_job_manager
from app.services.model_config import ModelConfigStore
from app.services.project_repository import ProjectNotFoundError, ProjectRepository


router = APIRouter()


def _require_project(project_id: str) -> dict:
    try:
        return ProjectRepository(settings.hunter_metadata_db).require(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _submit(project_id: str, request_payload: dict) -> dict:
    _require_project(project_id)
    model_payload = ModelConfigStore().build_model_payload()
    if not model_payload:
        raise HTTPException(status_code=400, detail="请先配置模型参数")
    payload = {**request_payload, "project_id": project_id}
    try:
        job, created = background_job_manager.submit(
            "domain_tree",
            payload,
            dedupe_key=f"domain-tree:{project_id}",
        )
    except BackgroundJobCapacityExceeded as error:
        raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "1"}) from error
    return {"status": "accepted", "created": created, **job}


def _require_project_job(project_id: str, job_id: str) -> dict:
    _require_project(project_id)
    job = background_job_manager.get(job_id)
    request = (job or {}).get("request") or {}
    if not job or job.get("type") != "domain_tree" or str(request.get("project_id") or "") != project_id:
        raise HTTPException(status_code=404, detail="当前项目中不存在该领域树任务")
    return job


@router.post("/api/domain-tree/generate")
def generate_domain_tree(payload: DomainTreeGenerateRequest) -> dict:
    """兼容旧客户端；仍在服务端验证项目是否真实存在。"""
    return _submit(payload.project_id, payload.model_dump(exclude={"project_id"}))


@router.post("/api/projects/{project_id}/domain-tree/generate")
def generate_project_domain_tree(project_id: str, payload: DomainTreeGenerateOptions) -> dict:
    return _submit(project_id, payload.model_dump())


@router.get("/api/domain-tree/jobs/active/{project_id}")
def get_active_domain_tree_job(project_id: str) -> dict:
    _require_project(project_id)
    job = background_job_manager.find_active("domain_tree", f"domain-tree:{project_id}")
    if not job:
        raise HTTPException(status_code=404, detail="当前项目没有活动的领域树任务")
    return job


@router.get("/api/projects/{project_id}/domain-tree/jobs/active")
def get_active_project_domain_tree_job(project_id: str) -> dict:
    return get_active_domain_tree_job(project_id)


@router.get("/api/projects/{project_id}/domain-tree/jobs/{job_id}")
def get_project_domain_tree_job(project_id: str, job_id: str) -> dict:
    job = _require_project_job(project_id, job_id)
    if job.get("status") == "completed":
        job = {**job, "result": DomainTreeAgent().get_result(project_id)}
    return job


@router.post("/api/projects/{project_id}/domain-tree/jobs/{job_id}/cancel")
def cancel_project_domain_tree_job(project_id: str, job_id: str) -> dict:
    _require_project_job(project_id, job_id)
    job = background_job_manager.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="领域树任务不存在")
    return job


@router.get("/api/domain-tree/{project_id}")
def get_domain_tree(project_id: str) -> dict:
    try:
        _require_project(project_id)
        result = DomainTreeAgent().get_result(project_id)
        if not result:
            raise HTTPException(status_code=404, detail="Domain tree not found")
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/projects/{project_id}/domain-tree")
def get_project_domain_tree(project_id: str) -> dict:
    return get_domain_tree(project_id)


__all__ = ["router"]
