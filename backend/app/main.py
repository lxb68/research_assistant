import json
# FastAPI 应用入口：集中定义论文、解析、领域树和研究代理相关接口。
import importlib.util
import asyncio
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.agents import DomainTreeAgent, HunterAgent, OrchestratorAgent
from app.core.config import settings
from app.services.mineru import MinerURequest, mineru_processing
from app.services.model_client import discover_models
from app.services.model_config import ModelConfigStore
from app.services.paper_search import SUPPORTED_SOURCES, search_papers


MULTIPART_AVAILABLE = importlib.util.find_spec("multipart") is not None

if not MULTIPART_AVAILABLE:
    UploadFile = bytes  # type: ignore[assignment]
    File = lambda *args, **kwargs: None  # type: ignore[assignment]
    Form = lambda *args, **kwargs: None  # type: ignore[assignment]


# 请求模型集中约束接口输入，避免业务函数重复校验基础字段。
class DatasetDownloadRequest(BaseModel):
    """定义数据集检索与下载接口的请求参数。"""
    keyword: str = Field(..., min_length=1, description="检索关键词")
    sources: list[str] = Field(default_factory=lambda: ["arxiv", "crossref"], description="论文来源")
    limit_per_source: int = Field(10, ge=1, le=200, description="每个来源期望返回的论文数量")
    download_pdf: bool = Field(True, description="是否同时下载 PDF")
    year_from: int | None = Field(None, ge=1900, le=2100, description="起始年份")
    year_to: int | None = Field(None, ge=1900, le=2100, description="结束年份")
    min_impact_factor: float | None = Field(None, ge=0, description="最小影响因子")
    ccf_levels: list[str] = Field(default_factory=list, description="CCF 分级筛选")


class ManualPdfLinkRequest(BaseModel):
    """定义手动关联本地 PDF 的请求参数。"""
    pdf_path: str = Field(..., min_length=1, description="后端机器可访问的本地 PDF 路径")
    record_id: str | None = Field(None, description="论文记录 ID")
    doi: str | None = Field(None, description="论文 DOI")
    title: str | None = Field(None, description="论文标题")


class DeletePapersRequest(BaseModel):
    """定义批量删除论文记录的请求参数。"""
    ids: list[str] = Field(..., min_length=1, max_length=500, description="论文记录 ID 列表")


class DeduplicatePapersRequest(BaseModel):
    """定义论文去重接口的请求参数。"""
    record_id: str | None = Field(None, description="可选：仅对指定记录 ID 执行去重")


class ImportPaperRequest(BaseModel):
    """定义手动导入论文元数据的请求参数。"""
    raw_text: str = Field("", description="粘贴的标题、DOI 或摘要")
    title: str = Field("", description="论文标题")
    authors: list[str] = Field(default_factory=list, description="作者列表")
    abstract: str = Field("", description="论文摘要")
    year: str = Field("", description="年份")
    doi: str = Field("", description="DOI")
    url: str = Field("", description="原文链接")
    pdf_url: str = Field("", description="PDF 链接")
    custom_tags: list[str] = Field(default_factory=list, description="自定义标签")


class DomainTreeGenerateRequest(BaseModel):
    """定义领域树生成或修订接口的请求参数。"""
    project_id: str = Field(..., min_length=1, description="领域树对应的项目 ID 或论文记录 ID")
    action: str = Field("rebuild", description="生成动作：rebuild / revise / keep")
    language: str = Field("中文", description="提示词语言")
    all_toc: str | None = Field(None, description="可选：完整目录文本")
    new_toc: str | None = Field(None, description="可选：新增目录内容")
    delete_toc: str | None = Field(None, description="可选：待删除目录内容")
    model: str | None = Field(None, description="可选：覆盖默认模型配置")


class ModelConfigRequest(BaseModel):
    """定义模型配置保存接口的请求参数。"""
    provider: str = Field("", description="供应商标识；为空时按 Base URL 自动识别")
    protocol: str = Field("", description="模型服务协议；为空时使用供应商默认协议")
    model: str = Field(..., min_length=1, description="模型名称")
    base_url: str = Field(..., min_length=1, description="LLM Base URL")
    api_key: str = Field("", description="LLM API 密钥")


class ModelDiscoveryRequest(BaseModel):
    """定义供应商模型发现接口的请求参数。"""
    provider: str = Field("", description="供应商标识；为空时按 Base URL 自动识别")
    protocol: str = Field("", description="模型服务协议；为空时使用供应商默认协议")
    base_url: str = Field(..., min_length=1, description="模型服务 Base URL")
    api_key: str = Field("", description="模型服务 API Key；留空时尝试使用同供应商已保存密钥")


class ChatMessage(BaseModel):
    """描述研究对话中的单条消息。"""
    role: str = Field(..., pattern="^(user|assistant)$", description="消息角色")
    content: str = Field(..., min_length=1, max_length=20000, description="消息内容")


class ResearchChatRequest(BaseModel):
    """定义研究问答接口的请求参数。"""
    question: str = Field(..., min_length=1, max_length=20000, description="研究问题")
    history: list[ChatMessage] = Field(default_factory=list, max_length=20, description="最近对话历史")
    paper_ids: list[str] = Field(default_factory=list, max_length=100, description="可选：限定论文记录 ID")


class OrchestratorRequest(BaseModel):
    """定义代理编排接口的请求参数。"""
    task: str = Field(..., min_length=1, max_length=20000, description="需要编排执行的研究任务")
    action: str = Field("auto", pattern="^(auto|chat|search|domain_tree)$", description="指定动作或自动路由")
    arguments: dict = Field(default_factory=dict, description="传递给目标 Agent 的受限参数")


app = FastAPI(
    title="Research Assistant API",
    description="用于文献检索与研究代理的 Python FastAPI 后端。",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 健康检查与基础能力发现 ---
@app.get("/api/health")
def health() -> dict:
    """返回后端健康状态和可用论文来源。"""
    return {
        "status": "ok",
        "service": "research-assistant-fastapi-backend",
        "sources": sorted(SUPPORTED_SOURCES.keys()),
    }


@app.get("/api/papers/sources")
def paper_sources() -> dict:
    """返回当前支持的论文检索来源。"""
    return {"sources": sorted(SUPPORTED_SOURCES.keys())}


# --- 研究问答与代理编排 ---
@app.post("/api/research/chat")
async def research_chat(payload: ResearchChatRequest) -> dict:
    """执行一次非流式研究问答。"""
    try:
        result = await OrchestratorAgent().run(
            payload.question,
            action="chat",
            arguments={
                "history": [message.model_dump() for message in payload.history],
                "paper_ids": payload.paper_ids,
                "allow_external_search": not bool(payload.paper_ids),
            },
        )
        return {"status": "ok", **result}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/research/chat/stream")
def research_chat_stream(payload: ResearchChatRequest) -> StreamingResponse:
    """以 NDJSON 流返回研究问答进度和最终结果。"""
    def encode_event(event: dict) -> str:
        """把事件编码为一行 NDJSON 数据。"""
        return json.dumps(event, ensure_ascii=False) + "\n"

    def event_stream():
        """持续产出后台任务写入的流式事件。"""
        events: queue.Queue[dict] = queue.Queue()

        def push_log(message: str) -> None:
            """把运行日志写入当前流式任务队列。"""
            events.put({"type": "log", "message": message})

        def run_agent() -> None:
            """在线程中执行代理并回传结果或异常。"""
            try:
                result = asyncio.run(
                    OrchestratorAgent(log_callback=push_log).run(
                        payload.question,
                        action="chat",
                        arguments={
                            "history": [message.model_dump() for message in payload.history],
                            "paper_ids": payload.paper_ids,
                            "allow_external_search": not bool(payload.paper_ids),
                        },
                    )
                )
                events.put({"type": "result", "result": result})
            except Exception as error:
                events.put({"type": "error", "message": str(error)})
            finally:
                events.put({"type": "done"})

        threading.Thread(target=run_agent, daemon=True).start()
        while True:
            event = events.get()
            yield encode_event(event)
            if event.get("type") == "done":
                break

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/orchestrator/run")
async def orchestrator_run(payload: OrchestratorRequest) -> dict:
    """根据任务内容选择并运行受限研究代理。"""
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


@app.get("/api/debug/routes")
def debug_routes() -> dict:
    """列出当前 FastAPI 应用注册的接口路由。"""
    return {
        "routes": sorted(
            {
                f"{','.join(sorted(route.methods or []))} {route.path}"
                for route in app.routes
                if getattr(route, "path", "")
            },
        ),
    }


# --- 模型配置：对外只返回脱敏后的公开信息 ---
@app.get("/api/settings/model-config")
def get_model_config() -> dict:
    """返回脱敏后的模型配置。"""
    store = ModelConfigStore()
    return {"status": "ok", **store.get_public_config()}


@app.get("/api/settings/model-providers")
def get_model_providers() -> dict:
    """返回后端支持的模型供应商与协议目录。"""
    return {"status": "ok", "providers": ModelConfigStore().get_provider_catalog()}


@app.post("/api/settings/model-config/discover")
def discover_provider_models(payload: ModelDiscoveryRequest) -> dict:
    """连接模型服务并返回当前账号或本地运行时可用的模型。"""
    try:
        store = ModelConfigStore()
        candidate = store.build_candidate(
            provider=payload.provider,
            protocol=payload.protocol,
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
        models = discover_models(candidate)
        return {"status": "ok", "models": models, "count": len(models)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"模型服务连接失败：{error}") from error


@app.post("/api/settings/model-config")
def save_model_config(payload: ModelConfigRequest) -> dict:
    """校验并保存模型配置。"""
    try:
        store = ModelConfigStore()
        result = store.save(
            provider=payload.provider,
            protocol=payload.protocol,
            model=payload.model,
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
        return {"status": "ok", **result}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


# --- 论文检索、下载和本地数据集管理 ---
@app.get("/api/papers/search")
def paper_search(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    source: str = Query("arxiv", description="论文来源"),
    limit: int = Query(10, ge=1, le=50, description="结果数量，范围 1～50"),
) -> dict:
    """按指定来源检索论文。"""
    try:
        return search_papers(source=source, query=q, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/datasets/download")
def dataset_download(payload: DatasetDownloadRequest) -> dict:
    """检索、筛选并下载符合条件的论文数据集。"""
    try:
        agent = HunterAgent()
        return agent.run(
            payload.keyword,
            sources=payload.sources,
            limit_per_source=payload.limit_per_source,
            download_pdf=payload.download_pdf,
            year_from=payload.year_from,
            year_to=payload.year_to,
            min_impact_factor=payload.min_impact_factor,
            ccf_levels=payload.ccf_levels,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/datasets/download/stream")
def dataset_download_stream(payload: DatasetDownloadRequest) -> StreamingResponse:
    """流式返回数据集下载任务的日志与结果。"""
    def encode_event(event: dict) -> str:
        """把事件编码为一行 NDJSON 数据。"""
        return json.dumps(event, ensure_ascii=False) + "\n"

    def event_stream():
        """持续产出后台任务写入的流式事件。"""
        events: queue.Queue[dict] = queue.Queue()

        def push_log(message: str) -> None:
            """把运行日志写入当前流式任务队列。"""
            events.put({"type": "log", "message": message})

        def run_agent() -> None:
            """在线程中执行代理并回传结果或异常。"""
            try:
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
            except Exception as error:
                events.put({"type": "error", "message": str(error)})
            finally:
                events.put({"type": "done"})

        threading.Thread(target=run_agent, daemon=True).start()

        while True:
            event = events.get()
            yield encode_event(event)
            if event.get("type") == "done":
                break

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/papers/link-local-pdf")
def link_local_pdf(payload: ManualPdfLinkRequest) -> dict:
    """把本地 PDF 文件关联到已保存论文记录。"""
    try:
        local_path = Path(payload.pdf_path).expanduser()
        agent = HunterAgent()
        record = agent.attach_local_pdf(
            pdf_path=local_path,
            record_id=payload.record_id,
            doi=payload.doi,
            title=payload.title,
        )
        return {"status": "ok", "paper": record}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/cleanup-missing-pdfs")
def cleanup_missing_pdfs() -> dict:
    """清理引用了不存在 PDF 文件的论文元数据。"""
    try:
        agent = HunterAgent()
        return {"status": "ok", **agent.cleanup_records_without_local_pdf()}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/papers")
def list_papers(
    limit: int = Query(100, ge=1, le=500, description="返回论文数量"),
    keyword: str | None = Query(None, description="按关键词或标题筛选"),
) -> dict:
    """分页列出本地保存的论文记录。"""
    try:
        agent = HunterAgent()
        papers = agent.list_saved_papers(limit=limit, keyword=keyword)
        return {"count": len(papers), "papers": papers}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/papers/{record_id}")
def get_paper(record_id: str) -> dict:
    """按记录 ID 返回单篇论文详情。"""
    try:
        agent = HunterAgent()
        paper = agent.get_saved_paper(record_id)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper record not found")
        return {"paper": paper}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/delete")
def delete_papers(payload: DeletePapersRequest) -> dict:
    """批量删除论文记录及受管理的关联文件。"""
    try:
        agent = HunterAgent()
        return {"status": "ok", **agent.delete_saved_papers(payload.ids)}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/deduplicate")
def deduplicate_papers(payload: DeduplicatePapersRequest | None = None) -> dict:
    """按稳定标识合并重复论文记录。"""
    try:
        agent = HunterAgent()
        result = agent.deduplicate_saved_papers(record_id=payload.record_id if payload else None)
        return {"status": "ok", **result}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/papers/{record_id}/pdf")
def get_paper_pdf(record_id: str, request: Request) -> FileResponse:
    """返回指定论文的本地 PDF 文件。"""
    del request
    try:
        agent = HunterAgent()
        paper = agent.get_saved_paper(record_id)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper record not found")

        pdf_path = agent.find_local_pdf_for_paper(paper)
        if not pdf_path or not pdf_path.exists() or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=404, detail="Local PDF file not found")

        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=pdf_path.name,
            content_disposition_type="inline",
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/papers/{record_id}/open", response_model=None)
def open_paper_source(record_id: str):
    """优先打开本地 PDF，否则跳转到论文来源地址。"""
    try:
        agent = HunterAgent()
        paper = agent.get_saved_paper(record_id)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper record not found")

        pdf_path = agent.find_local_pdf_for_paper(paper)
        if pdf_path and pdf_path.exists() and pdf_path.is_file() and pdf_path.suffix.lower() == ".pdf":
            return FileResponse(
                pdf_path,
                media_type="application/pdf",
                filename=pdf_path.name,
                content_disposition_type="inline",
            )

        external_url = str(paper.get("url", "")).strip()
        if external_url:
            return RedirectResponse(external_url, status_code=307)

        raise HTTPException(status_code=404, detail="No local PDF or external source URL available")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/papers/import")
def import_paper(payload: ImportPaperRequest) -> dict:
    """导入用户填写的论文元数据。"""
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
        return {"status": "ok", "paper": paper}
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
    """导入 PDF 并解析、保存论文元数据。"""
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
        return {"status": "ok", "paper": paper}
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
    """流式返回 PDF 导入和解析进度。"""
    content = await file.read()
    filename = file.filename or "paper.pdf"

    def encode_event(event: dict) -> str:
        """把事件编码为一行 NDJSON 数据。"""
        return json.dumps(event, ensure_ascii=False) + "\n"

    def event_stream():
        """持续产出后台任务写入的流式事件。"""
        events: queue.Queue[dict] = queue.Queue()

        def push_log(message: str) -> None:
            """把运行日志写入当前流式任务队列。"""
            events.put({"type": "log", "message": message})

        def run_import() -> None:
            """在线程中执行 PDF 导入并回传处理事件。"""
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

        threading.Thread(target=run_import, daemon=True).start()

        while True:
            event = events.get()
            yield encode_event(event)
            if event.get("type") == "done":
                break

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def re_split_values(value: str) -> list[str]:
    """按中英文分隔符拆分并清理文本值。"""
    separators = [",", ";", "，", "；", "、"]
    values = [value]
    for separator in separators:
        values = [part for item in values for part in item.split(separator)]
    return [part.strip() for part in values if part.strip()]


# --- 领域树与知识图谱生成、读取 ---
@app.post("/api/domain-tree/generate")
async def generate_domain_tree(payload: DomainTreeGenerateRequest) -> dict:
    """根据本地论文生成或修订领域树。"""
    try:
        config_store = ModelConfigStore()
        model_payload = config_store.build_model_payload()
        if not model_payload:
            raise HTTPException(status_code=400, detail="请先配置模型参数")

        agent = DomainTreeAgent()
        tags = await agent.handle_domain_tree(
            payload.project_id,
            action=payload.action,
            all_toc=payload.all_toc,
            new_toc=payload.new_toc,
            model=payload.model or model_payload,
            language=payload.language,
            delete_toc=payload.delete_toc,
        )
        if not tags:
            raise HTTPException(status_code=400, detail="未找到可用于生成领域树的 Markdown 或目录数据")

        result = agent.get_result(payload.project_id)
        if not result:
            raise HTTPException(status_code=500, detail="领域树已生成，但读取结果失败")

        return {"status": "ok", **result}
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/domain-tree/{project_id}")
def get_domain_tree(project_id: str) -> dict:
    """读取指定项目已有的领域树结果。"""
    try:
        agent = DomainTreeAgent()
        result = agent.get_result(project_id)
        if not result:
            raise HTTPException(status_code=404, detail="Domain tree has not been generated for this project")
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


# --- 独立 MinerU 转换接口 ---
@app.post("/api/mineru/process")
async def process_mineru(request: MinerURequest):
    """执行一次 MinerU PDF 转换任务。"""
    try:
        agent = HunterAgent()
        pdf_path = request.pdf_path

        if request.record_id:
            paper = agent.get_saved_paper(request.record_id)
            if not paper:
                raise HTTPException(status_code=404, detail="Paper record not found")

            resolved_pdf_path = agent.find_local_pdf_for_paper(paper)
            if not resolved_pdf_path:
                raise HTTPException(status_code=400, detail="Local PDF file not found for this paper")
            pdf_path = str(resolved_pdf_path)

        result = mineru_processing(
            project_id=request.project_id,
            file_name=request.file_name,
            pdf_path=pdf_path,
            output_name=request.output_name or request.record_id,
            mineru_token=request.mineru_token,
        )
        if request.record_id:
            try:
                updated_paper = agent.update_saved_paper(
                    request.record_id,
                    {
                        "markdownPath": result.get("markdownPath", ""),
                        "markdownOutputDir": result.get("outputDir", ""),
                        "sourcePdfPath": result.get("sourcePdfPath", ""),
                    },
                )
                updated_paper = agent.refresh_paper_metadata_from_markdown(
                    request.record_id,
                    markdown_path=result.get("markdownPath", ""),
                )
                updated_paper = agent.split_saved_paper_from_markdown(
                    request.record_id,
                    markdown_path=result.get("markdownPath", ""),
                    min_split_length=request.split_min_length,
                    max_split_length=request.split_max_length,
                )
                dedupe_result = agent.deduplicate_saved_papers(record_id=request.record_id)
                if dedupe_result.get("canonicalPapers"):
                    updated_paper = dedupe_result["canonicalPapers"][0]
            except Exception as error:
                raise HTTPException(
                    status_code=500,
                    detail=f"MinerU conversion succeeded, but failed to update paper metadata: {error}",
                ) from error
            return {"status": "ok", "result": result, "paper": updated_paper}

        return {"status": "ok", "result": result}
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

