"""Paper search, dataset download and local library routes."""

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app.agents import HunterAgent
from app.api.streaming import ndjson_worker_response
from app.schemas.api import (
    CleanupMissingPdfsRequest,
    DatasetDownloadRequest,
    DeduplicatePapersRequest,
    DeletePapersRequest,
    ImportPaperRequest,
    ManualPdfLinkRequest,
)
from app.core.config import settings
from app.services.project_repository import ProjectRepository
from app.services.paper_search import search_papers


router = APIRouter()


def split_form_values(value: str) -> list[str]:
    values = [value]
    for separator in [",", ";", "，", "；", "、"]:
        values = [part for item in values for part in item.split(separator)]
    return [part.strip() for part in values if part.strip()]


def _run_dataset(agent: HunterAgent, payload: DatasetDownloadRequest, *, cancel_event=None) -> dict:
    return agent.run(
        payload.keyword,
        sources=payload.sources,
        limit_per_source=payload.limit_per_source,
        download_pdf=payload.download_pdf,
        year_from=payload.year_from,
        year_to=payload.year_to,
        min_impact_factor=payload.min_impact_factor,
        ccf_levels=payload.ccf_levels,
        cancel_event=cancel_event,
    )


@router.get("/api/papers/search")
def paper_search(
    q: str = Query(..., min_length=1),
    source: str = Query("arxiv"),
    limit: int = Query(10, ge=1, le=50),
) -> dict:
    try:
        return search_papers(source=source, query=q, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/datasets/download")
def dataset_download(payload: DatasetDownloadRequest) -> dict:
    try:
        return _run_dataset(HunterAgent(), payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/datasets/download/stream")
async def dataset_download_stream(payload: DatasetDownloadRequest, request: Request):
    def produce(emit, cancel_event) -> None:
        agent = HunterAgent(log_callback=lambda message: emit({"type": "log", "message": message}))
        emit({"type": "result", "result": _run_dataset(agent, payload, cancel_event=cancel_event)})

    return ndjson_worker_response(request, produce)


@router.post("/api/papers/link-local-pdf")
def link_local_pdf(payload: ManualPdfLinkRequest) -> dict:
    try:
        record = HunterAgent().attach_local_pdf(
            pdf_path=Path(payload.pdf_path).expanduser(),
            record_id=payload.record_id,
            doi=payload.doi,
            title=payload.title,
        )
        return {"status": "ok", "paper": record}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/papers/cleanup-missing-pdfs/preview")
def preview_cleanup_missing_pdfs() -> dict:
    try:
        result = HunterAgent().preview_records_without_local_pdf()
        candidates = [
            {
                "id": str(record.get("id") or ""),
                "title": str(record.get("title") or "未命名文献"),
                "source": str(record.get("source") or ""),
            }
            for record in result.pop("candidateRecords", [])
        ]
        return {"status": "ok", **result, "candidates": candidates}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/papers/cleanup-missing-pdfs")
def cleanup_missing_pdfs(payload: CleanupMissingPdfsRequest | None = None) -> dict:
    try:
        result = HunterAgent().cleanup_records_without_local_pdf(payload.ids if payload else None)
        removed_ids = [str(record.get("id") or "") for record in result.get("removedRecords", [])]
        removed_project_references = ProjectRepository(settings.hunter_metadata_db).remove_paper_references(removed_ids)
        return {"status": "ok", **result, "removedProjectReferenceCount": removed_project_references}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/papers")
def list_papers(
    limit: int = Query(100, ge=1, le=500),
    keyword: str | None = Query(None),
) -> dict:
    try:
        papers = HunterAgent().list_saved_papers(limit=limit, keyword=keyword)
        return {"count": len(papers), "papers": papers}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/papers/{record_id}")
def get_paper(record_id: str) -> dict:
    try:
        paper = HunterAgent().get_saved_paper(record_id)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper record not found")
        return {"paper": paper}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/papers/delete")
def delete_papers(payload: DeletePapersRequest) -> dict:
    try:
        return {"status": "ok", **HunterAgent().delete_saved_papers(payload.ids)}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/papers/deduplicate")
def deduplicate_papers(payload: DeduplicatePapersRequest | None = None) -> dict:
    try:
        result = HunterAgent().deduplicate_saved_papers(record_id=payload.record_id if payload else None)
        return {"status": "ok", **result}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/papers/{record_id}/pdf")
def get_paper_pdf(record_id: str, request: Request) -> FileResponse:
    del request
    try:
        agent = HunterAgent()
        paper = agent.get_saved_paper(record_id)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper record not found")
        pdf_path = agent.find_local_pdf_for_paper(paper)
        if not pdf_path or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=404, detail="Local PDF file not found")
        return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name, content_disposition_type="inline")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/papers/{record_id}/open", response_model=None)
def open_paper_source(record_id: str):
    try:
        agent = HunterAgent()
        paper = agent.get_saved_paper(record_id)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper record not found")
        pdf_path = agent.find_local_pdf_for_paper(paper)
        if pdf_path and pdf_path.is_file() and pdf_path.suffix.lower() == ".pdf":
            return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name, content_disposition_type="inline")
        external_url = str(paper.get("url", "")).strip()
        if external_url:
            return RedirectResponse(external_url, status_code=307)
        raise HTTPException(status_code=404, detail="No local PDF or external source URL available")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/papers/import")
def import_paper(payload: ImportPaperRequest) -> dict:
    try:
        paper = HunterAgent().import_paper(**payload.model_dump())
        return {"status": "ok", "paper": paper}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


def _import_pdf(agent: HunterAgent, content: bytes, filename: str, **fields) -> dict:
    return agent.import_pdf_paper(
        pdf_bytes=content,
        filename=filename,
        authors=split_form_values(fields.pop("authors")),
        custom_tags=split_form_values(fields.pop("custom_tags")),
        **fields,
    )


@router.post("/api/papers/import-pdf")
async def import_pdf_paper(
    file: UploadFile = File(...), title: str = Form(""), authors: str = Form(""),
    abstract: str = Form(""), year: str = Form(""), doi: str = Form(""),
    url: str = Form(""), custom_tags: str = Form(""),
) -> dict:
    try:
        paper = _import_pdf(
            HunterAgent(), await file.read(), file.filename or "paper.pdf",
            title=title, authors=authors, abstract=abstract, year=year, doi=doi, url=url, custom_tags=custom_tags,
        )
        return {"status": "ok", "paper": paper}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/papers/import-pdf/stream")
async def import_pdf_paper_stream(
    request: Request,
    file: UploadFile = File(...), title: str = Form(""), authors: str = Form(""),
    abstract: str = Form(""), year: str = Form(""), doi: str = Form(""),
    url: str = Form(""), custom_tags: str = Form(""),
):
    content = await file.read()
    filename = file.filename or "paper.pdf"

    def produce(emit, cancel_event) -> None:
        push_log = lambda message: emit({"type": "log", "message": message})
        push_log(f"已接收 PDF 文件：{filename}，大小 {len(content)} bytes")
        push_log("开始解析 PDF：优先使用 MinerU，失败后降级使用 PyMuPDF")
        paper = _import_pdf(
            HunterAgent(log_callback=push_log), content, filename,
            title=title, authors=authors, abstract=abstract, year=year, doi=doi, url=url, custom_tags=custom_tags,
            cancel_event=cancel_event,
        )
        emit({"type": "result", "paper": paper})
        push_log("PDF 导入完成，已保存到本地数据集")

    return ndjson_worker_response(request, produce)


__all__ = ["router", "split_form_values"]
