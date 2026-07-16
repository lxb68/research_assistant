"""流式任务的 HTTP 提交与异步订阅适配层。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.services.stream_tasks import (
    StreamCapacityExceeded,
    StreamProducer,
    StreamTask,
    stream_task_manager,
)


router = APIRouter()


def ndjson_worker_response(request: Request, producer: StreamProducer) -> StreamingResponse:
    """提交到统一执行池，并立即订阅该任务的 NDJSON 事件。"""
    try:
        task = stream_task_manager.submit(producer)
    except StreamCapacityExceeded as error:
        raise HTTPException(
            status_code=503,
            detail=str(error),
            headers={"Retry-After": "1"},
        ) from error
    return _subscription_response(request, task)


@router.get("/api/stream/jobs/{job_id}")
async def subscribe_stream_job(job_id: str, request: Request) -> StreamingResponse:
    """按任务 ID 订阅仍在内存中的有界事件缓冲。"""
    task = stream_task_manager.get(job_id)
    if not task:
        raise HTTPException(status_code=404, detail="流式任务不存在或已过期")
    return _subscription_response(request, task)


@router.post("/api/stream/jobs/{job_id}/cancel")
async def cancel_stream_job(job_id: str) -> dict[str, str]:
    """仅在用户明确操作时取消后台任务。"""
    task = stream_task_manager.get(job_id)
    if not task:
        raise HTTPException(status_code=404, detail="流式任务不存在或已过期")
    if stream_task_manager.cancel(job_id):
        return {"jobId": job_id, "status": "cancelling"}
    return {"jobId": job_id, "status": task.status}


def _subscription_response(request: Request, task: StreamTask) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(request, task.id),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Stream-Job-Id": task.id,
        },
    )


async def _event_stream(request: Request, job_id: str) -> AsyncIterator[str]:
    task = stream_task_manager.begin_subscription(job_id)
    if not task:
        yield _encode({"type": "error", "jobId": job_id, "message": "流式任务不存在"})
        yield _encode({"type": "done", "jobId": job_id, "status": "failed"})
        return

    sequence = 0
    last_output_at = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                return

            events, sequence = task.buffer.read_after(sequence)
            if events:
                for event in events:
                    yield _encode(event.payload)
                    last_output_at = time.monotonic()
                    if event.payload.get("type") == "done":
                        return
                continue

            now = time.monotonic()
            if now - last_output_at >= settings.stream_heartbeat_seconds:
                yield _encode(
                    {
                        "type": "heartbeat",
                        "jobId": job_id,
                        "status": task.status,
                        "droppedLogCount": task.buffer.dropped_log_count,
                    },
                )
                last_output_at = now
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        raise
    finally:
        stream_task_manager.end_subscription(job_id)


def _encode(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


__all__ = ["ndjson_worker_response", "router"]
