"""将具体业务任务适配到统一后台任务协议。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.agents import DomainTreeAgent, HunterAgent, OrchestratorAgent
from app.core.config import settings
from app.schemas.api import DatasetDownloadRequest, DomainTreeGenerateRequest, ResearchChatRequest
from app.services.background_jobs import BackgroundJobContext, BackgroundJobManager
from app.services.conversations import conversation_store
from app.services.model_config import ModelConfigStore
from app.services.project_scope import ProjectScopeService


def _research_arguments(payload: ResearchChatRequest) -> dict[str, Any]:
    return ProjectScopeService(settings.hunter_metadata_db).build_research_arguments(
        project_id=payload.project_id,
        requested_paper_ids=payload.paper_ids,
        history=[message.model_dump() for message in payload.history],
    )


def _dataset_download(context: BackgroundJobContext, raw: dict[str, Any]) -> dict[str, Any]:
    payload = DatasetDownloadRequest.model_validate(raw)
    context.progress(2, stage="preparing", message="正在准备数据集检索")

    def log(message: str) -> None:
        context.log(message)

    result = HunterAgent(log_callback=log).run(
        payload.keyword,
        sources=payload.sources,
        limit_per_source=payload.limit_per_source,
        download_pdf=payload.download_pdf,
        year_from=payload.year_from,
        year_to=payload.year_to,
        min_impact_factor=payload.min_impact_factor,
        ccf_levels=payload.ccf_levels,
        cancel_event=context.cancel_event,
    )
    context.progress(95, stage="saving", message="正在保存数据集结果")
    return result


def _research_chat(context: BackgroundJobContext, raw: dict[str, Any]) -> dict[str, Any]:
    request_payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    payload = ResearchChatRequest.model_validate(request_payload)
    conversation_id = str(raw.get("conversationId") or "").strip()
    message_id = str(raw.get("messageId") or context.job_id).strip()
    response_message_id = str(raw.get("responseMessageId") or f"{message_id}-answer")
    context.progress(3, stage="planning", message="正在分析研究问题")

    def log(message: str) -> None:
        context.log(message)

    result = asyncio.run(
        OrchestratorAgent(log_callback=log).run(
            payload.question,
            action="auto",
            arguments=_research_arguments(payload),
            cancel_event=context.cancel_event,
        ),
    )
    if conversation_id:
        body = result.get("result") if isinstance(result.get("result"), dict) else {}
        required = body.get("requiredMaterials") or []
        material_text = "\n".join(
            f"{index + 1}. {item.get('description', '')}"
            for index, item in enumerate(required)
            if isinstance(item, dict)
        )
        if body.get("status") in {"needs_materials", "needs_user_action"}:
            content = str(body.get("message") or "当前流程需要你的协助。")
            if material_text:
                content += f"\n\n建议补充：\n{material_text}"
        else:
            content = str(body.get("answer") or "研究任务已完成，但没有返回可展示的回答。")
        conversation_store.upsert_message(
            conversation_id,
            response_message_id,
            role="assistant",
            content=content,
            sources=body.get("sources") if isinstance(body.get("sources"), list) else [],
            context_sources=body.get("retrievedSources") if isinstance(body.get("retrievedSources"), list) else body.get("sources") or [],
            response_mode="direct" if result.get("action") == "direct" else "research",
            job_id=context.job_id,
        )
    context.progress(96, stage="persisting", message="正在保存研究回答")
    return result


def _domain_tree(context: BackgroundJobContext, raw: dict[str, Any]) -> dict[str, Any]:
    payload = DomainTreeGenerateRequest.model_validate(raw)
    model_payload = ModelConfigStore().build_model_payload()
    if not model_payload:
        raise ValueError("请先配置模型参数")
    agent = DomainTreeAgent()

    def report(update: dict[str, Any]) -> None:
        completed = int(update.get("completedChunks") or 0)
        total = int(update.get("totalChunks") or 0)
        progress = int(completed * 90 / total) + 5 if total else 5
        safe_update = {key: value for key, value in update.items() if key != "partialResult"}
        context.progress(
            progress,
            stage=str(update.get("stage") or "building"),
            message=str(update.get("message") or "正在构建领域树与知识图谱"),
            details=safe_update,
        )

    tags = agent.handle_domain_tree_sync(
        payload.project_id,
        action=payload.action,
        all_toc=payload.all_toc,
        new_toc=payload.new_toc,
        model=payload.model or model_payload,
        language=payload.language,
        delete_toc=payload.delete_toc,
        cancel_event=context.cancel_event,
        progress_callback=report,
    )
    if not tags:
        raise ValueError("未找到可用于生成领域树的 Markdown 或目录数据")
    domain_tree_path = agent.get_result_path(payload.project_id)
    if not domain_tree_path.exists():
        raise RuntimeError("领域树已生成，但读取结果失败")
    graph_path = domain_tree_path.parent / "knowledge_graph.json"
    manifest_path = domain_tree_path.parent / "manifest.json"
    return {
        "projectId": payload.project_id,
        "domainTreePath": str(domain_tree_path),
        "knowledgeGraphPath": str(graph_path) if graph_path.exists() else None,
        "manifestPath": str(manifest_path) if manifest_path.exists() else None,
    }


def _pdf_import(context: BackgroundJobContext, raw: dict[str, Any]) -> dict[str, Any]:
    staging_root = (Path(settings.backend_storage_dir) / "job_uploads").resolve()
    staging_path = Path(str(raw.get("stagingPath") or "")).resolve()
    if staging_root not in staging_path.parents or not staging_path.is_file():
        raise ValueError("PDF 暂存文件无效或已过期")
    filename = str(raw.get("filename") or staging_path.name)
    context.log(f"已接收 PDF 文件：{filename}，大小 {staging_path.stat().st_size} bytes")
    context.progress(5, stage="parsing", message="正在解析 PDF")
    try:
        paper = HunterAgent(log_callback=context.log).import_pdf_paper(
            pdf_bytes=staging_path.read_bytes(),
            filename=filename,
            title=str(raw.get("title") or ""),
            authors=list(raw.get("authors") or []),
            abstract=str(raw.get("abstract") or ""),
            year=str(raw.get("year") or ""),
            doi=str(raw.get("doi") or ""),
            url=str(raw.get("url") or ""),
            custom_tags=list(raw.get("customTags") or []),
            cancel_event=context.cancel_event,
        )
        return {"paper": paper}
    finally:
        staging_path.unlink(missing_ok=True)


def register_background_job_handlers(manager: BackgroundJobManager) -> None:
    """在应用组合根注册业务处理器，避免调度器反向依赖 API 路由。"""
    manager.register("dataset_download", _dataset_download)
    manager.register("research_chat", _research_chat)
    manager.register("domain_tree", _domain_tree)
    manager.register("pdf_import", _pdf_import)


__all__ = ["register_background_job_handlers"]
