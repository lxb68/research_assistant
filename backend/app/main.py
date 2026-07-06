import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.agents import HunterAgent
from app.core.config import settings
from app.services.paper_search import SUPPORTED_SOURCES, search_papers
from app.services.mineru import mineru_processing, MinerURequest


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


class ImportPaperRequest(BaseModel):
    """手动导入论文元数据。"""

    raw_text: str = Field("", description="粘贴的题录、DOI 或摘要文本")
    title: str = Field("", description="论文标题")
    authors: list[str] = Field(default_factory=list, description="作者列表")
    abstract: str = Field("", description="论文摘要")
    year: str = Field("", description="年份")
    doi: str = Field("", description="DOI")
    url: str = Field("", description="原文链接")
    pdf_url: str = Field("", description="PDF 链接")
    custom_tags: list[str] = Field(default_factory=list, description="用户自定义标签")


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


@app.get("/api/papers/{record_id}")
def get_paper(record_id: str) -> dict:
    """返回单篇已保存论文元数据。"""
    try:
        agent = HunterAgent()
        paper = agent.get_saved_paper(record_id)
        if not paper:
            raise HTTPException(status_code=404, detail="论文记录不存在")
        return {
            "paper": paper,
        }
    except HTTPException:
        raise
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


@app.get("/api/papers/{record_id}/pdf")
def get_paper_pdf(record_id: str, request: Request) -> FileResponse:
    """读取已保存论文绑定的本地 PDF 文件。"""
    try:
        agent = HunterAgent()
        # ===== 步骤1：记录请求信息 =====
        print("=" * 60, flush=True)
        print(f"[查看PDF] >>> 收到请求: record_id={record_id!r}", flush=True)
        print(f"[查看PDF] >>> 请求URL: {request.url}", flush=True)
        print(f"[查看PDF] >>> 请求方法: {request.method}", flush=True)
        client_host = request.client.host if request.client else 'unknown'
        print(f"[查看PDF] >>> 客户端Host: {client_host}", flush=True)
        print(f"[查看PDF] >>> Accept请求头: {request.headers.get('accept', '(未设置)')}", flush=True)
        ua = request.headers.get('user-agent', '(未设置)')
        print(f"[查看PDF] >>> User-Agent: {ua[:120]}", flush=True)
        print(f"[查看PDF] >>> Referer: {request.headers.get('referer', '(未设置)')}", flush=True)
        print(f"[查看PDF] >>> Sec-Fetch-Site: {request.headers.get('sec-fetch-site', '(未设置)')}", flush=True)
        print(f"[查看PDF] >>> Sec-Fetch-Dest: {request.headers.get('sec-fetch-dest', '(未设置)')}", flush=True)

        # ===== 步骤2：查找论文记录 =====
        paper = agent.get_saved_paper(record_id)
        if not paper:
            print(f"[查看PDF] [FAIL] 记录不存在: record_id={record_id!r}", flush=True)
            raise HTTPException(status_code=404, detail="论文记录不存在")
        title_preview = str(paper.get('title', ''))[:80]
        print(f"[查看PDF] [OK] 找到论文记录: title={title_preview!r}", flush=True)
        stored_pdf = paper.get('pdfPath', '')
        print(f"[查看PDF] [OK] 数据库中pdfPath字段: {stored_pdf!r}", flush=True)

        # ===== 步骤3：解析本地PDF路径 =====
        pdf_path = agent.find_local_pdf_for_paper(paper)
        print(f"[查看PDF] --- find_local_pdf_for_paper 返回: {str(pdf_path)!r}", flush=True)
        if not pdf_path:
            print(f"[查看PDF] [FAIL] find_local_pdf_for_paper 返回 None", flush=True)
            raise HTTPException(status_code=404, detail="本地 PDF 文件不存在")
        print(f"[查看PDF] [OK] pdf_path.exists() = {pdf_path.exists()}", flush=True)
        print(f"[查看PDF] [OK] pdf_path.is_file() = {pdf_path.is_file()}", flush=True)
        print(f"[查看PDF] [OK] pdf_path.suffix = {pdf_path.suffix!r}", flush=True)
        print(f"[查看PDF] [OK] pdf_path.absolute() = {str(pdf_path.absolute())!r}", flush=True)
        file_size = pdf_path.stat().st_size
        print(f"[查看PDF] [OK] 文件大小 = {file_size} bytes", flush=True)

        if not pdf_path.exists() or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            print(f"[查看PDF] [FAIL] 文件校验失败", flush=True)
            raise HTTPException(status_code=404, detail="本地 PDF 文件不存在")

        # ===== 步骤4：构建响应 =====
        filename = pdf_path.name
        print(f"[查看PDF] --- 构建FileResponse ---", flush=True)
        print(f"[查看PDF]    media_type = 'application/pdf'", flush=True)
        print(f"[查看PDF]    filename = {filename!r}", flush=True)
        print(f"[查看PDF]    content_disposition_type = 'inline'", flush=True)
        print(f"[查看PDF]    预期Content-Disposition头: inline; filename=\"{filename}\"", flush=True)

        response = FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=filename,
            content_disposition_type="inline",
        )

        # ===== 步骤5：打印响应头确认 =====
        print(f"[查看PDF] --- 实际响应信息 ---", flush=True)
        print(f"[查看PDF]    response.media_type = {response.media_type!r}", flush=True)
        print(f"[查看PDF]    response.status_code = {response.status_code}", flush=True)
        for header_name, header_value in response.headers.items():
            print(f"[查看PDF]   响应头 [{header_name}] = {header_value}", flush=True)
        print("=" * 60, flush=True)

        return response
    except HTTPException:
        raise
    except Exception as error:
        print(f"[查看PDF] [ERROR] 异常: {error}", flush=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/papers/{record_id}/open", response_model=None)
def open_paper_source(record_id: str):
    """优先打开本地 PDF；没有本地 PDF 时打开外部原文 URL。"""
    try:
        agent = HunterAgent()
        print(f"[打开原文] 收到请求: record_id={record_id!r}", flush=True)
        paper = agent.get_saved_paper(record_id)
        if not paper:
            print(f"[打开原文] 记录不存在: record_id={record_id!r}", flush=True)
            raise HTTPException(status_code=404, detail="论文记录不存在")

        pdf_path = agent.find_local_pdf_for_paper(paper)
        if pdf_path and pdf_path.exists() and pdf_path.is_file() and pdf_path.suffix.lower() == ".pdf":
            print(
                "[打开原文] 优先返回本地 PDF "
                f"record_id={record_id!r}, stored_pdf_path={paper.get('pdfPath', '')!r}, "
                f"resolved_pdf_path={str(pdf_path)!r}",
                flush=True,
            )
            return FileResponse(
                pdf_path,
                media_type="application/pdf",
                filename=pdf_path.name,
                content_disposition_type="inline",
            )

        external_url = str(paper.get("url", "")).strip()
        if external_url:
            print(
                "[打开原文] 本地 PDF 不可用，回退外部原文链接 "
                f"record_id={record_id!r}, stored_pdf_path={paper.get('pdfPath', '')!r}, external_url={external_url!r}",
                flush=True,
            )
            return RedirectResponse(external_url, status_code=307)

        print(
            "[打开原文] 打开失败：既没有可用的本地 PDF，也没有外部原文链接 "
            f"record_id={record_id!r}, stored_pdf_path={paper.get('pdfPath', '')!r}",
            flush=True,
        )
        raise HTTPException(status_code=404, detail="没有可打开的本地 PDF 或外部原文链接")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/import")
def import_paper(payload: ImportPaperRequest) -> dict:
    """手动导入一条论文元数据记录，支持从题录文本自动解析常见字段。"""
    try:
        agent = HunterAgent()
        paper = agent.import_paper(
            raw_text=payload.raw_text,
            title=payload.title,
            authors=payload.authors,
            abstract=payload.abstract,
            year=payload.year,
            doi=payload.doi,
            url=payload.url,
            pdf_url=payload.pdf_url,
            custom_tags=payload.custom_tags,
        )
        return {
            "status": "ok",
            "paper": paper,
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/import-pdf")
async def import_pdf_paper(
    file: UploadFile = File(..., description="文献 PDF 文件"),
    title: str = Form("", description="手动覆盖标题"),
    authors: str = Form("", description="作者，多个作者用逗号分隔"),
    abstract: str = Form("", description="手动覆盖摘要"),
    year: str = Form("", description="手动覆盖年份"),
    doi: str = Form("", description="手动覆盖 DOI"),
    url: str = Form("", description="原文链接"),
    custom_tags: str = Form("", description="自定义标签，多个标签用逗号分隔"),
) -> dict:
    """导入 PDF 文献，优先 PyMuPDF 自动解析，复杂 PDF 尝试 MinerU。"""
    try:
        content = await file.read()
        agent = HunterAgent()
        paper = agent.import_pdf_paper(
            pdf_bytes=content,
            filename=file.filename or "paper.pdf",
            title=title,
            authors=[author.strip() for author in re_split_values(authors)],
            abstract=abstract,
            year=year,
            doi=doi,
            url=url,
            custom_tags=[tag.strip() for tag in re_split_values(custom_tags)],
        )
        return {
            "status": "ok",
            "paper": paper,
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/import-pdf/stream")
async def import_pdf_paper_stream(
    file: UploadFile = File(..., description="文献 PDF 文件"),
    title: str = Form("", description="手动覆盖标题"),
    authors: str = Form("", description="作者，多个作者用逗号分隔"),
    abstract: str = Form("", description="手动覆盖摘要"),
    year: str = Form("", description="手动覆盖年份"),
    doi: str = Form("", description="手动覆盖 DOI"),
    url: str = Form("", description="原文链接"),
    custom_tags: str = Form("", description="自定义标签，多个标签用逗号分隔"),
) -> StreamingResponse:
    """流式导入 PDF 文献，逐条返回后端解析进展。"""
    content = await file.read()
    filename = file.filename or "paper.pdf"

    def encode_event(event: dict) -> str:
        return json.dumps(event, ensure_ascii=False) + "\n"

    def event_stream():
        events: queue.Queue[dict] = queue.Queue()

        def push_log(message: str) -> None:
            events.put({"type": "log", "message": message})

        def run_import() -> None:
            try:
                push_log(f"已接收 PDF 文件：{filename}，大小 {len(content)} bytes")
                push_log("开始解析 PDF：优先使用 PyMuPDF，必要时尝试 MinerU")
                agent = HunterAgent(log_callback=push_log)
                paper = agent.import_pdf_paper(
                    pdf_bytes=content,
                    filename=filename,
                    title=title,
                    authors=[author.strip() for author in re_split_values(authors)],
                    abstract=abstract,
                    year=year,
                    doi=doi,
                    url=url,
                    custom_tags=[tag.strip() for tag in re_split_values(custom_tags)],
                )
                events.put({"type": "result", "paper": paper})
                push_log("PDF 导入完成，已保存到本地数据集")
            except ValueError as error:
                events.put({"type": "error", "message": str(error)})
            except Exception as error:
                events.put({"type": "error", "message": str(error)})
            finally:
                events.put({"type": "done"})

        worker = threading.Thread(target=run_import, daemon=True)
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


def re_split_values(value: str) -> list[str]:
    separators = [",", ";", "，", "、"]
    values = [value]
    for separator in separators:
        values = [part for item in values for part in item.split(separator)]
    return [part.strip() for part in values if part.strip()]

@app.post("/api/mineru/process")
async def process_mineru(request: MinerURequest, background_tasks: BackgroundTasks):
    """
    接收项目 ID 和文件名，后台执行 MinerU 转换。
    返回任务接受状态，实际进度可通过其他接口查询（或由回调更新）。
    """
    try:
        # 校验文件是否存在等（此处省略）
        # 执行转换（同步执行，若时间长可放入后台任务）
        # 建议使用 BackgroundTasks 或 Celery 异步执行
        background_tasks.add_task(
            mineru_processing,
            request.project_id,
            request.file_name,
            None,  # 回调
            None   # task_info
        )
        return {"status": "accepted", "message": "Processing started."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 如果需要同步等待，可另设接口。