"""Research chat and orchestration routes."""

import asyncio

from fastapi import APIRouter, HTTPException

from app.agents import OrchestratorAgent
from app.api.streaming import ndjson_worker_response
from app.schemas.api import OrchestratorRequest, ResearchChatRequest


router = APIRouter()


def _research_arguments(payload: ResearchChatRequest) -> dict:
    return {
        "history": [message.model_dump() for message in payload.history],
        "paper_ids": payload.paper_ids,
        "allow_external_search": not bool(payload.paper_ids),
    }


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
def research_chat_stream(payload: ResearchChatRequest):
    def produce(emit) -> None:
        def push_log(message: str) -> None:
            emit({"type": "log", "message": message})

        result = asyncio.run(
            OrchestratorAgent(log_callback=push_log).run(
                payload.question,
                action="auto",
                arguments=_research_arguments(payload),
            ),
        )
        emit({"type": "result", "result": result})

    return ndjson_worker_response(produce)


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
