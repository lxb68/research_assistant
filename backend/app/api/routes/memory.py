"""研究记忆候选、已确认记忆和全局用户偏好 API。"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.services.research_memory import research_memory_extractor, research_memory_store


router = APIRouter()


class MemoryExtractRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=20000)
    answer: str = Field(..., min_length=1, max_length=30000)
    sources: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    project_id: str = Field(..., alias="projectId", min_length=1, max_length=200)
    project_name: str = Field("", alias="projectName", max_length=200)
    source_conversation_id: str = Field("", alias="sourceConversationId", max_length=200)
    source_message_id: str = Field("", alias="sourceMessageId", max_length=200)


class MemoryWriteRequest(BaseModel):
    project_id: str = Field(..., alias="projectId", min_length=1, max_length=200)
    project_name: str = Field("", alias="projectName", max_length=200)
    type: Literal["conclusion", "fact", "decision", "limitation", "hypothesis", "task"]
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(..., min_length=1, max_length=1200)
    tags: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(0.5, ge=0, le=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    source_conversation_id: str = Field("", alias="sourceConversationId", max_length=200)
    source_message_id: str = Field("", alias="sourceMessageId", max_length=200)
    source_question: str = Field("", alias="sourceQuestion", max_length=2000)


class MemoryPatchRequest(BaseModel):
    type: Literal["conclusion", "fact", "decision", "limitation", "hypothesis", "task"] | None = None
    title: str | None = Field(None, min_length=1, max_length=200)
    summary: str | None = Field(None, min_length=1, max_length=1200)
    tags: list[str] | None = Field(None, max_length=8)
    confidence: float | None = Field(None, ge=0, le=1)


class PreferencesRequest(BaseModel):
    preferred_name: str | None = Field(None, alias="preferredName", max_length=80)
    language: str | None = Field(None, max_length=30)
    answer_style: str | None = Field(None, alias="answerStyle", max_length=200)


@router.get("/api/research-memories")
def list_memories(
    project_id: list[str] | None = Query(None, alias="projectId"),
    session_id: str = Query("local", alias="sessionId"),
) -> dict[str, Any]:
    memories = research_memory_store.list(session_id=session_id, project_ids=project_id)
    return {"count": len(memories), "memories": memories}


@router.post("/api/research-memories/extract")
def extract_memory(payload: MemoryExtractRequest) -> dict[str, Any]:
    candidate = research_memory_extractor.extract(payload.question, payload.answer, payload.sources)
    return {
        "candidate": {
            **candidate,
            "projectId": payload.project_id,
            "projectName": payload.project_name,
            "evidence": payload.sources,
            "sourceConversationId": payload.source_conversation_id,
            "sourceMessageId": payload.source_message_id,
            "sourceQuestion": payload.question,
        }
    }


@router.post("/api/research-memories", status_code=201)
def create_memory(payload: MemoryWriteRequest) -> dict[str, Any]:
    try:
        return {"memory": research_memory_store.create(payload.model_dump(by_alias=True))}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.patch("/api/research-memories/{memory_id}")
def update_memory(memory_id: str, payload: MemoryPatchRequest) -> dict[str, Any]:
    try:
        memory = research_memory_store.update(memory_id, payload.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not memory:
        raise HTTPException(status_code=404, detail="研究记忆不存在")
    return {"memory": memory}


@router.delete("/api/research-memories/{memory_id}", status_code=204)
def delete_memory(memory_id: str) -> Response:
    if not research_memory_store.delete(memory_id):
        raise HTTPException(status_code=404, detail="研究记忆不存在")
    return Response(status_code=204)


@router.get("/api/user-preferences")
def get_preferences(session_id: str = Query("local", alias="sessionId")) -> dict[str, Any]:
    return {"preferences": research_memory_store.get_preferences(session_id=session_id)}


@router.patch("/api/user-preferences")
def update_preferences(payload: PreferencesRequest) -> dict[str, Any]:
    return {
        "preferences": research_memory_store.update_preferences(
            payload.model_dump(by_alias=True, exclude_none=True)
        )
    }


@router.delete("/api/user-preferences", status_code=204)
def clear_preferences() -> Response:
    research_memory_store.clear_preferences()
    return Response(status_code=204)


__all__ = ["router"]
