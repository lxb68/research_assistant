import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents import HunterAgent
from app.core.config import settings
from app.services.paper_search import SUPPORTED_SOURCES, search_papers


class DatasetDownloadRequest(BaseModel):
    """下载数据集请求参数。"""

    keyword: str = Field(..., min_length=1, description="搜索关键词")
    sources: list[str] = Field(default_factory=lambda: ["arxiv", "crossref"], description="文献来源")
    limit_per_source: int = Field(10, ge=1, le=200, description="每个数据源的目标下载论文数量")
    download_pdf: bool = Field(True, description="是否下载 PDF")
    year_from: int | None = Field(None, ge=1900, le=2100, description="起始年份")
    year_to: int | None = Field(None, ge=1900, le=2100, description="结束年份")
    min_impact_factor: float | None = Field(None, ge=0, description="最低影响因子")
    ccf_levels: list[str] = Field(default_factory=list, description="CCF 等级过滤，例如 A/B/C/NON_CCF")


class ManualPdfLinkRequest(BaseModel):
    """用户手动下载 PDF 后，用本地路径更新论文记录。"""

    pdf_path: str = Field(..., min_length=1, description="后端机器可访问的本地 PDF 路径")
    record_id: str | None = Field(None, description="论文记录 ID")
    doi: str | None = Field(None, description="论文 DOI")
    title: str | None = Field(None, description="论文标题")


class DeletePapersRequest(BaseModel):
    """批量删除已保存论文元数据记录。"""

    ids: list[str] = Field(..., min_length=1, max_length=500, description="论文记录 ID 列表")


app = FastAPI(
    title="Research Assistant API",
    description="Python FastAPI backend for literature search.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    """健康检查接口：确认后端服务和可用文献源。"""
    return {
        "status": "ok",
        "service": "research-assistant-fastapi-backend",
        "sources": sorted(SUPPORTED_SOURCES.keys()),
    }


@app.get("/api/papers/sources")
def paper_sources() -> dict:
    """返回当前后端支持的文献检索来源。"""
    return {
        "sources": sorted(SUPPORTED_SOURCES.keys()),
    }


@app.get("/api/debug/routes")
def debug_routes() -> dict:
    """返回当前后端进程实际注册的路由，用于排查前端 404 是否来自旧进程。"""
    return {
        "routes": sorted(
            {
                f"{','.join(sorted(route.methods or []))} {route.path}"
                for route in app.routes
                if getattr(route, "path", "")
            },
        ),
    }


@app.get("/api/papers/search")
def paper_search(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    source: str = Query("arxiv", description="文献来源：arxiv/pubmed/crossref/ieee"),
    limit: int = Query(10, ge=1, le=50, description="返回数量，范围 1-50"),
) -> dict:
    """单源文献搜索接口：保留给调试和轻量搜索使用。"""
    try:
        return search_papers(source=source, query=q, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/datasets/download")
def dataset_download(payload: DatasetDownloadRequest) -> dict:
    """下载数据集接口：调用 HunterAgent 完成搜索、去重、初筛、PDF 下载和元数据保存。"""
    try:
        print(
            "[DatasetDownload] 收到请求 "
            f"keyword={payload.keyword!r}, sources={payload.sources}, "
            f"target_per_source={payload.limit_per_source}, download_pdf={payload.download_pdf}, "
            f"year_from={payload.year_from}, year_to={payload.year_to}, "
            f"min_impact_factor={payload.min_impact_factor}, ccf_levels={payload.ccf_levels}",
            flush=True,
        )
        agent = HunterAgent()
        result = agent.run(
            payload.keyword,
            sources=payload.sources,
            limit_per_source=payload.limit_per_source,
            download_pdf=payload.download_pdf,
            year_from=payload.year_from,
            year_to=payload.year_to,
            min_impact_factor=payload.min_impact_factor,
            ccf_levels=payload.ccf_levels,
        )
        print(
            "[DatasetDownload] 请求完成 "
            f"searched={result['searchedCount']}, filtered={result['filteredCount']}, saved={result['savedCount']}",
            flush=True,
        )
        return result
    except ValueError as error:
        print(f"[DatasetDownload] 请求参数错误: {error}", flush=True)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        print(f"[DatasetDownload] 请求失败: {error}", flush=True)
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/datasets/download/stream")
def dataset_download_stream(payload: DatasetDownloadRequest) -> StreamingResponse:
    """流式下载数据集接口：逐条推送 HunterAgent 状态，最后返回完整结果。"""

    def encode_event(event: dict) -> str:
        return json.dumps(event, ensure_ascii=False) + "\n"

    def event_stream():
        events: queue.Queue[dict] = queue.Queue()

        def push_log(message: str) -> None:
            events.put({"type": "log", "message": message})

        def run_agent() -> None:
            try:
                print(
                    "[DatasetDownloadStream] 收到请求 "
                    f"keyword={payload.keyword!r}, sources={payload.sources}, "
                    f"limit_per_source={payload.limit_per_source}, download_pdf={payload.download_pdf}, "
                    f"year_from={payload.year_from}, year_to={payload.year_to}, "
                    f"min_impact_factor={payload.min_impact_factor}, ccf_levels={payload.ccf_levels}",
                    flush=True,
                )
                agent = HunterAgent(log_callback=push_log)
                result = agent.run(
                    payload.keyword,
                    sources=payload.sources,
                    limit_per_source=payload.limit_per_source,
                    download_pdf=payload.download_pdf,
                    year_from=payload.year_from,
                    year_to=payload.year_to,
                    min_impact_factor=payload.min_impact_factor,
                    ccf_levels=payload.ccf_levels,
                )
                events.put({"type": "result", "result": result})
                print(
                    "[DatasetDownloadStream] 请求完成 "
                    f"searched={result['searchedCount']}, filtered={result['filteredCount']}, saved={result['savedCount']}",
                    flush=True,
                )
            except Exception as error:
                print(f"[DatasetDownloadStream] 请求失败: {error}", flush=True)
                events.put({"type": "error", "message": str(error)})
            finally:
                events.put({"type": "done"})

        worker = threading.Thread(target=run_agent, daemon=True)
        worker.start()

        while True:
            event = events.get()
            yield encode_event(event)
            if event.get("type") == "done":
                break

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/papers/link-local-pdf")
def link_local_pdf(payload: ManualPdfLinkRequest) -> dict:
    """把用户手动下载到本地的 PDF 文件路径绑定到已有论文记录。"""
    try:
        local_path = Path(payload.pdf_path).expanduser()
        agent = HunterAgent()
        record = agent.attach_local_pdf(
            pdf_path=local_path,
            record_id=payload.record_id,
            doi=payload.doi,
            title=payload.title,
        )
        return {
            "status": "ok",
            "paper": record,
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/cleanup-missing-pdfs")
def cleanup_missing_pdfs() -> dict:
    """删除没有对应本地 PDF 文件的论文元数据记录。"""
    try:
        agent = HunterAgent()
        return {
            "status": "ok",
            **agent.cleanup_records_without_local_pdf(),
        }
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/papers")
def list_papers(
    limit: int = Query(100, ge=1, le=500, description="返回论文数量"),
    keyword: str | None = Query(None, description="按关键词或标题过滤"),
) -> dict:
    """返回已保存论文元数据，用于前端浏览本地数据集。"""
    try:
        agent = HunterAgent()
        papers = agent.list_saved_papers(limit=limit, keyword=keyword)
        return {
            "count": len(papers),
            "papers": papers,
        }
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/delete")
def delete_papers(payload: DeletePapersRequest) -> dict:
    """批量删除已保存论文元数据记录，不删除本地 PDF 文件。"""
    try:
        agent = HunterAgent()
        return {
            "status": "ok",
            **agent.delete_saved_papers(payload.ids),
        }
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
