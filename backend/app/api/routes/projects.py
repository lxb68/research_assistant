"""项目及项目论文成员关系接口。"""

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.schemas.api import ProjectCreateRequest, ProjectPapersRequest
from app.services.paper_repository import PaperRepository
from app.services.project_repository import (
    ProjectNotFoundError,
    ProjectPaperNotFoundError,
    ProjectRepository,
)


router = APIRouter()


def _repositories() -> tuple[ProjectRepository, PaperRepository]:
    papers = PaperRepository(settings.hunter_metadata_db)
    return ProjectRepository(settings.hunter_metadata_db), papers


@router.get("/api/projects")
def list_projects() -> dict:
    projects, _ = _repositories()
    values = projects.list()
    return {"count": len(values), "projects": values}


@router.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreateRequest) -> dict:
    projects, _ = _repositories()
    try:
        project = projects.create(
            name=payload.name,
            description=payload.description,
            paper_ids=payload.paper_ids,
        )
        return {"status": "ok", "project": project}
    except ProjectPaperNotFoundError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    projects, _ = _repositories()
    try:
        return {"project": projects.require(project_id)}
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/projects/{project_id}/papers")
def list_project_papers(project_id: str) -> dict:
    projects, papers = _repositories()
    try:
        values = papers.list_by_ids(projects.list_paper_ids(project_id))
        return {"count": len(values), "papers": values}
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/api/projects/{project_id}/papers")
def replace_project_papers(project_id: str, payload: ProjectPapersRequest) -> dict:
    projects, papers = _repositories()
    try:
        project = projects.replace_papers(project_id, payload.paper_ids)
        values = papers.list_by_ids(projects.list_paper_ids(project_id))
        return {"status": "ok", "project": project, "count": len(values), "papers": values}
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProjectPaperNotFoundError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


__all__ = ["router"]
