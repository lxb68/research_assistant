"""Standalone MinerU processing route."""

import asyncio

from fastapi import APIRouter, HTTPException

from app.agents import HunterAgent
from app.core.config import settings
from app.services.mineru import MinerURequest, mineru_processing


router = APIRouter()


def _process_mineru_sync(request: MinerURequest) -> dict:
    agent = HunterAgent()
    record_id = str(request.record_id or request.project_id or "").strip()
    paper = agent.get_saved_paper(record_id) if record_id else None
    resolved_pdf_path = agent.find_local_pdf_for_paper(paper) if paper else None
    result = mineru_processing(
        project_id=record_id or None,
        file_name=request.file_name,
        pdf_path=str(resolved_pdf_path or request.pdf_path or "") or None,
        output_name=request.output_name or record_id or None,
        mineru_token=request.mineru_token,
    )
    if not paper:
        return result
    try:
        indexed_paper = agent.index_saved_structured_markdown(
            record_id,
            markdown_path=str(result.get("markdownPath") or ""),
            output_dir=str(result.get("outputDir") or ""),
            parser="mineru",
            conversion_result=result,
            min_split_length=request.split_min_length or settings.split_min_length,
            max_split_length=request.split_max_length or settings.split_max_length,
        )
        return {**result, "paper": indexed_paper, "split": indexed_paper}
    except Exception as error:
        raise RuntimeError(f"MinerU 转换成功，但更新论文元数据失败：{error}") from error


@router.post("/api/mineru/process")
async def process_mineru(request: MinerURequest):
    try:
        return await asyncio.to_thread(_process_mineru_sync, request)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


__all__ = ["router"]
