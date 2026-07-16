"""Standalone MinerU processing route."""

import asyncio

from fastapi import APIRouter, HTTPException

from app.agents import HunterAgent
from app.core.config import settings
from app.services.mineru import MinerURequest, mineru_processing


router = APIRouter()


def _process_mineru_sync(request: MinerURequest) -> dict:
    agent = HunterAgent()
    paper = agent.get_saved_paper(request.project_id)
    resolved_pdf_path = agent.find_local_pdf_for_paper(paper) if paper else None
    result = mineru_processing(
        request.project_id,
        request.file_name,
        resolved_pdf_path or request.pdf_path,
    )
    if not paper:
        return result
    try:
        updated_paper = agent.update_saved_paper(
            request.project_id,
            {
                "markdownPath": result.get("markdownPath", ""),
                "markdownOutputDir": result.get("outputDir", ""),
                "mineruResult": result,
            },
        )
        split_result = agent.split_saved_paper_from_markdown(
            request.project_id,
            min_split_length=request.split_min_length or settings.split_min_length,
            max_split_length=request.split_max_length or settings.split_max_length,
        )
        return {**result, "paper": updated_paper, "split": split_result}
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
