"""Research chat and orchestration routes."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from app.agents import OrchestratorAgent
from app.api.streaming import ndjson_worker_response
from app.schemas.api import OrchestratorRequest, ResearchChatRequest
from app.core.config import settings
from app.services.project_repository import ProjectNotFoundError
from app.services.project_scope import ProjectScopeService


router = APIRouter()


def _research_arguments(payload: ResearchChatRequest) -> dict:
    try:
        return ProjectScopeService(settings.hunter_metadata_db).build_research_arguments(
            project_id=payload.project_id,
            requested_paper_ids=payload.paper_ids,
            history=[message.model_dump() for message in payload.history],
        )
    except ProjectNotFoundError as error:
        raise ValueError(str(error)) from error


@router.post("/api/research/chat")
async def research_chat(payload: ResearchChatRequest) -> dict:
    try:
        result = await OrchestratorAgent().run(
            payload.question,
            action="auto",
            arguments=_research_arguments(payload),
        )
        return {"status": "ok", **result}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/research/chat/stream")
async def research_chat_stream(payload: ResearchChatRequest, request: Request):
    def produce(emit, cancel_event) -> None:
        def push_log(message: str) -> None:
            emit({"type": "log", "message": message})

        result = asyncio.run(
            OrchestratorAgent(log_callback=push_log).run(
                payload.question,
                action="auto",
                arguments=_research_arguments(payload),
                cancel_event=cancel_event,
            ),
        )
        emit({"type": "result", "result": result})

    return ndjson_worker_response(request, produce)


@router.post("/api/orchestrator/run")
async def orchestrator_run(payload: OrchestratorRequest) -> dict:
    try:
        result = await OrchestratorAgent().run(
            payload.task,
            action=payload.action,
            arguments=payload.arguments,
        )
        return {"status": "ok", **result}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


__all__ = ["router"]
