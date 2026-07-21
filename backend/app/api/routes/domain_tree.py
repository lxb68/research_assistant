"""Domain tree and knowledge graph job routes."""

from fastapi import APIRouter, HTTPException

from app.agents import DomainTreeAgent
from app.core.config import settings
from app.schemas.api import (
    DomainTreeGenerateOptions,
    DomainTreeGenerateRequest,
    DomainTreeNodeUpdateRequest,
    KnowledgeEntityUpdateRequest,
    KnowledgeRelationUpdateRequest,
    KnowledgeRevisionRequest,
)
from app.services.background_jobs import BackgroundJobCapacityExceeded, background_job_manager
from app.services.model_config import ModelConfigStore
from app.services.project_repository import ProjectNotFoundError, ProjectRepository
from app.services.project_knowledge import (
    KnowledgeCurationError,
    KnowledgeNotFoundError,
    KnowledgeRevisionConflict,
    ProjectKnowledgeService,
)


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


def _knowledge_service(project_id: str) -> ProjectKnowledgeService:
    _require_project(project_id)
    active_job = background_job_manager.find_active("domain_tree", f"domain-tree:{project_id}")
    if active_job:
        raise HTTPException(status_code=423, detail="领域树或知识图谱正在生成，完成后才能编辑")
    agent = DomainTreeAgent()
    return ProjectKnowledgeService(agent.get_result_path(project_id).parent, project_id)


def _run_curation(operation) -> dict:
    try:
        return operation()
    except KnowledgeRevisionConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except KnowledgeNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except KnowledgeCurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


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


@router.patch("/api/projects/{project_id}/domain-tree/nodes/{node_id}")
def update_project_domain_tree_node(
    project_id: str,
    node_id: str,
    payload: DomainTreeNodeUpdateRequest,
) -> dict:
    service = _knowledge_service(project_id)
    return _run_curation(lambda: service.update_tree_node(node_id, {"label": payload.label}, payload.revision))


@router.delete("/api/projects/{project_id}/domain-tree/nodes/{node_id}")
def delete_project_domain_tree_node(
    project_id: str,
    node_id: str,
    payload: KnowledgeRevisionRequest,
    dry_run: bool = False,
) -> dict:
    service = _knowledge_service(project_id)
    return _run_curation(lambda: service.delete_tree_node(node_id, payload.revision, dry_run=dry_run))


@router.post("/api/projects/{project_id}/domain-tree/nodes/{node_id}/restore")
def restore_project_domain_tree_node(
    project_id: str,
    node_id: str,
    payload: KnowledgeRevisionRequest,
) -> dict:
    service = _knowledge_service(project_id)
    return _run_curation(lambda: service.restore_tree_node(node_id, payload.revision))


@router.patch("/api/projects/{project_id}/knowledge-graph/entities/{entity_id}")
def update_project_knowledge_entity(
    project_id: str,
    entity_id: str,
    payload: KnowledgeEntityUpdateRequest,
) -> dict:
    service = _knowledge_service(project_id)
    patch = payload.model_dump(exclude={"revision"}, exclude_unset=True)
    return _run_curation(lambda: service.update_entity(entity_id, patch, payload.revision))


@router.delete("/api/projects/{project_id}/knowledge-graph/entities/{entity_id}")
def delete_project_knowledge_entity(
    project_id: str,
    entity_id: str,
    payload: KnowledgeRevisionRequest,
    dry_run: bool = False,
) -> dict:
    service = _knowledge_service(project_id)
    return _run_curation(lambda: service.delete_entity(entity_id, payload.revision, dry_run=dry_run))


@router.post("/api/projects/{project_id}/knowledge-graph/entities/{entity_id}/restore")
def restore_project_knowledge_entity(
    project_id: str,
    entity_id: str,
    payload: KnowledgeRevisionRequest,
) -> dict:
    service = _knowledge_service(project_id)
    return _run_curation(lambda: service.restore_entity(entity_id, payload.revision))


@router.patch("/api/projects/{project_id}/knowledge-graph/relations/{relation_id}")
def update_project_knowledge_relation(
    project_id: str,
    relation_id: str,
    payload: KnowledgeRelationUpdateRequest,
) -> dict:
    service = _knowledge_service(project_id)
    patch = payload.model_dump(exclude={"revision"}, exclude_unset=True)
    return _run_curation(lambda: service.update_relation(relation_id, patch, payload.revision))


@router.delete("/api/projects/{project_id}/knowledge-graph/relations/{relation_id}")
def delete_project_knowledge_relation(
    project_id: str,
    relation_id: str,
    payload: KnowledgeRevisionRequest,
    dry_run: bool = False,
) -> dict:
    service = _knowledge_service(project_id)
    return _run_curation(lambda: service.delete_relation(relation_id, payload.revision, dry_run=dry_run))


@router.post("/api/projects/{project_id}/knowledge-graph/relations/{relation_id}/restore")
def restore_project_knowledge_relation(
    project_id: str,
    relation_id: str,
    payload: KnowledgeRevisionRequest,
) -> dict:
    service = _knowledge_service(project_id)
    return _run_curation(lambda: service.restore_relation(relation_id, payload.revision))


__all__ = ["router"]
