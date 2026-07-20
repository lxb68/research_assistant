"""统一后台任务、事件、取消、重试与研究会话查询接口。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.api import ResearchChatRequest
from app.services.background_jobs import BackgroundJobCapacityExceeded, background_job_manager
from app.services.conversations import conversation_store


router = APIRouter()


class JobCreateRequest(BaseModel):
    type: str = Field(..., min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str = Field("local", alias="sessionId", min_length=1, max_length=200)
    user_id: str | None = Field(None, alias="userId", max_length=200)
    conversation_id: str | None = Field(None, alias="conversationId", max_length=200)
    message_id: str | None = Field(None, alias="messageId", max_length=200)
    response_message_id: str | None = Field(None, alias="responseMessageId", max_length=200)
    dedupe_key: str | None = Field(None, alias="dedupeKey", max_length=500)


class ConversationRenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)


def _submit(
    job_type: str,
    payload: dict[str, Any],
    *,
    session_id: str = "local",
    user_id: str | None = None,
    dedupe_key: str | None = None,
    retryable: bool = True,
) -> tuple[dict[str, Any], bool]:
    try:
        return background_job_manager.submit(
            job_type,
            payload,
            session_id=session_id,
            user_id=user_id,
            dedupe_key=dedupe_key,
            retryable=retryable,
        )
    except BackgroundJobCapacityExceeded as error:
        raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "1"}) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/jobs", status_code=202)
def create_job(request: JobCreateRequest) -> dict[str, Any]:
    stored_payload = dict(request.payload)
    if request.type == "research_chat":
        research = ResearchChatRequest.model_validate(request.payload)
        if not request.conversation_id or not request.message_id:
            raise HTTPException(status_code=400, detail="research_chat 必须提供 conversationId 和 messageId")
        title = str(request.payload.get("title") or research.question[:60])
        conversation_store.ensure_conversation(
            request.conversation_id,
            title=title,
            session_id=request.session_id,
        )
        conversation_store.upsert_message(
            request.conversation_id,
            request.message_id,
            role="user",
            content=research.question,
        )
        stored_payload = {
            "conversationId": request.conversation_id,
            "messageId": request.message_id,
            "responseMessageId": request.response_message_id or f"{request.message_id}-answer",
            "payload": request.payload,
        }
    job, created = _submit(
        request.type,
        stored_payload,
        session_id=request.session_id,
        user_id=request.user_id,
        dedupe_key=request.dedupe_key,
    )
    return {"created": created, **job}


@router.get("/api/jobs")
def list_jobs(
    session_id: str = Query("local", alias="sessionId"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    jobs = background_job_manager.list(session_id=session_id, limit=limit)
    return {"count": len(jobs), "jobs": jobs}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = background_job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    return job


@router.get("/api/jobs/{job_id}/events")
def get_job_events(
    job_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    if not background_job_manager.get(job_id):
        raise HTTPException(status_code=404, detail="后台任务不存在")
    events = background_job_manager.events(job_id, after=after, limit=limit)
    return {"events": events, "nextCursor": events[-1]["sequence"] if events else after}


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    job = background_job_manager.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    return job


@router.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str) -> dict[str, Any]:
    try:
        retried = background_job_manager.retry(job_id)
    except (ValueError, BackgroundJobCapacityExceeded) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not retried:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    job, created = retried
    return {"created": created, **job}


@router.post("/api/jobs/pdf-import", status_code=202)
async def create_pdf_import_job(
    file: UploadFile = File(...),
    title: str = Form(""),
    authors: str = Form(""),
    abstract: str = Form(""),
    year: str = Form(""),
    doi: str = Form(""),
    url: str = Form(""),
    custom_tags: str = Form(""),
    session_id: str = Form("local"),
) -> dict[str, Any]:
    staging_root = Path(settings.backend_storage_dir) / "job_uploads"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_path = staging_root / f"{uuid.uuid4().hex}.pdf"
    staging_path.write_bytes(await file.read())
    split_values = lambda value: [item.strip() for item in value.replace("；", ";").replace("，", ",").replace(";", ",").split(",") if item.strip()]
    try:
        job, created = _submit(
            "pdf_import",
            {
                "stagingPath": str(staging_path),
                "filename": file.filename or "paper.pdf",
                "title": title,
                "authors": split_values(authors),
                "abstract": abstract,
                "year": year,
                "doi": doi,
                "url": url,
                "customTags": split_values(custom_tags),
            },
            session_id=session_id,
            retryable=False,
        )
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return {"created": created, **job}


@router.get("/api/conversations")
def list_conversations(
    session_id: str = Query("local", alias="sessionId"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    conversations = conversation_store.list(session_id=session_id, limit=limit)
    return {"count": len(conversations), "conversations": conversations}


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    conversation = conversation_store.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="研究对话不存在")
    return conversation


@router.patch("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRenameRequest) -> dict[str, Any]:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="对话标题不能为空")
    if not conversation_store.rename(conversation_id, title):
        raise HTTPException(status_code=404, detail="研究对话不存在")
    return {"id": conversation_id, "title": title}


@router.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str) -> Response:
    if not conversation_store.delete(conversation_id):
        raise HTTPException(status_code=404, detail="研究对话不存在")
    return Response(status_code=204)


__all__ = ["router"]
