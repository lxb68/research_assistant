"""Domain tree and knowledge graph job routes."""

import threading

from fastapi import APIRouter, HTTPException

from app.agents import DomainTreeAgent
from app.schemas.api import DomainTreeGenerateRequest
from app.services.domain_tree_jobs import DomainTreeJobOutcome, domain_tree_jobs
from app.services.model_config import ModelConfigStore


router = APIRouter()


def _run_domain_tree_job(
    payload: DomainTreeGenerateRequest,
    model_payload: dict[str, object],
    progress_callback,
    cancel_event: threading.Event,
) -> DomainTreeJobOutcome:
    agent = DomainTreeAgent()
    tags = agent.handle_domain_tree_sync(
        payload.project_id,
        action=payload.action,
        all_toc=payload.all_toc,
        new_toc=payload.new_toc,
        model=payload.model or model_payload,
        language=payload.language,
        delete_toc=payload.delete_toc,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
    if not tags:
        raise ValueError("未找到可用于生成领域树的 Markdown 或目录数据")
    result_path = agent.get_result_path(payload.project_id)
    if not result_path.exists():
        raise RuntimeError("领域树已生成，但读取结果失败")
    return DomainTreeJobOutcome(result_path=str(result_path))


@router.post("/api/domain-tree/generate")
def generate_domain_tree(payload: DomainTreeGenerateRequest) -> dict:
    model_payload = ModelConfigStore().build_model_payload()
    if not model_payload:
        raise HTTPException(status_code=400, detail="请先配置模型参数")
    job, created = domain_tree_jobs.submit(
        payload.project_id,
        payload.action,
        lambda report, cancel_event: _run_domain_tree_job(payload, model_payload, report, cancel_event),
    )
    return {"status": "accepted", "created": created, **job}


@router.get("/api/domain-tree/jobs/active/{project_id}")
def get_active_domain_tree_job(project_id: str) -> dict:
    job = domain_tree_jobs.get_active(project_id)
    if not job:
        raise HTTPException(status_code=404, detail="当前项目没有活动的领域树任务")
    return job


@router.get("/api/domain-tree/jobs/{job_id}")
def get_domain_tree_job(job_id: str) -> dict:
    job = domain_tree_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="领域树任务不存在")
    return job


@router.post("/api/domain-tree/jobs/{job_id}/cancel")
def cancel_domain_tree_job(job_id: str) -> dict:
    job = domain_tree_jobs.cancel(job_id)
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
