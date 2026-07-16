"""Domain tree and knowledge graph job routes."""

from fastapi import APIRouter, HTTPException

from app.agents import DomainTreeAgent
from app.schemas.api import DomainTreeGenerateRequest
from app.services.background_jobs import BackgroundJobCapacityExceeded, background_job_manager
from app.services.model_config import ModelConfigStore


router = APIRouter()


@router.post("/api/domain-tree/generate")
def generate_domain_tree(payload: DomainTreeGenerateRequest) -> dict:
    model_payload = ModelConfigStore().build_model_payload()
    if not model_payload:
        raise HTTPException(status_code=400, detail="请先配置模型参数")
    try:
        job, created = background_job_manager.submit(
            "domain_tree",
            payload.model_dump(),
            dedupe_key=f"domain-tree:{payload.project_id}",
        )
    except BackgroundJobCapacityExceeded as error:
        raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "1"}) from error
    return {"status": "accepted", "created": created, **job}


@router.get("/api/domain-tree/jobs/active/{project_id}")
def get_active_domain_tree_job(project_id: str) -> dict:
    job = background_job_manager.find_active("domain_tree", f"domain-tree:{project_id}")
    if not job:
        raise HTTPException(status_code=404, detail="当前项目没有活动的领域树任务")
    return job


@router.get("/api/domain-tree/jobs/{job_id}")
def get_domain_tree_job(job_id: str) -> dict:
    job = background_job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="领域树任务不存在")
    if job.get("type") != "domain_tree":
        raise HTTPException(status_code=404, detail="领域树任务不存在")
    if job.get("status") == "completed":
        project_id = str((job.get("request") or {}).get("project_id") or "")
        job = {**job, "result": DomainTreeAgent().get_result(project_id) if project_id else None}
    return job


@router.post("/api/domain-tree/jobs/{job_id}/cancel")
def cancel_domain_tree_job(job_id: str) -> dict:
    existing = background_job_manager.get(job_id)
    job = background_job_manager.cancel(job_id) if existing and existing.get("type") == "domain_tree" else None
    if not job:
        raise HTTPException(status_code=404, detail="领域树任务不存在")
    return job


@router.get("/api/domain-tree/{project_id}")
def get_domain_tree(project_id: str) -> dict:
    try:
        result = DomainTreeAgent().get_result(project_id)
        if not result:
            raise HTTPException(status_code=404, detail="Domain tree not found")
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


__all__ = ["router"]
