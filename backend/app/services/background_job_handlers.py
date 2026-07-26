"""将具体业务任务适配到统一后台任务协议。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.agents import DomainTreeAgent, HunterAgent, OrchestratorAgent
from app.agents.domainTree_agent import KnowledgeGraphQualityError
from app.core.config import settings
from app.schemas.api import DatasetDownloadRequest, DomainTreeGenerateRequest, ResearchChatRequest
from app.services.background_jobs import BackgroundJobContext, BackgroundJobManager
from app.services.conversations import conversation_store
from app.services.model_config import ModelConfigStore
from app.services.project_scope import ProjectScopeService
from app.services.research_memory import research_memory_store
from app.services.zotero_sync import ZoteroSyncService


_SEMANTIC_TRANSIENT_FAILURES = {
    "timeout",
    "rate_limited",
    "upstream",
    "invalid_json",
    "empty_response",
    "failure_rate_exceeded",
}
_SEMANTIC_CONFIGURATION_FAILURES = {
    "authentication",
    "quota_exhausted",
    "not_found",
    "content_filtered",
    "request_budget_exceeded",
    "input_token_budget_exceeded",
}
_SEMANTIC_CIRCUIT_FAILURES = {
    "failure_rate_exceeded",
    "request_budget_exceeded",
    "input_token_budget_exceeded",
}


def _semantic_auto_resume_decision(extraction: dict[str, Any]) -> tuple[bool, str]:
    """只自动恢复瞬时故障；配置错误和大面积输出截断等待用户处理。"""
    raw_reasons = extraction.get("failureReasons")
    reasons = {
        str(category): max(0, int(count or 0))
        for category, count in (raw_reasons.items() if isinstance(raw_reasons, dict) else [])
    }
    if any(reasons.get(category, 0) for category in _SEMANTIC_CONFIGURATION_FAILURES):
        return False, "检测到认证、配额或模型配置错误，已暂停自动恢复"

    root_failures = {
        category: count
        for category, count in reasons.items()
        if category not in _SEMANTIC_CIRCUIT_FAILURES
    }
    root_failure_count = sum(root_failures.values())
    truncated_count = root_failures.get("output_truncated", 0)
    if truncated_count >= 3 and (
        not root_failure_count or truncated_count / root_failure_count >= 0.5
    ):
        return False, "检测到大面积模型输出截断，请调整模型或思考模式后手动继续"

    transient_count = sum(reasons.get(category, 0) for category in _SEMANTIC_TRANSIENT_FAILURES)
    if transient_count:
        return True, "检测到可恢复的网络、限流或响应格式故障"
    return False, "未识别到适合自动恢复的瞬时故障"


def _semantic_recovery_details(
    extraction: dict[str, Any],
    *,
    status: str,
    attempt: int,
    limit: int,
    message: str,
) -> dict[str, Any]:
    """把恢复决策作为任务进度持久化，供重连和手动继续入口使用。"""
    return {
        "totalChunks": int(extraction.get("processedChunkCount") or 0)
        + int(extraction.get("failedChunkCount") or 0),
        "completedChunks": int(extraction.get("processedChunkCount") or 0)
        + int(extraction.get("failedChunkCount") or 0),
        "processedChunks": int(extraction.get("processedChunkCount") or 0),
        "failedChunks": int(extraction.get("failedChunkCount") or 0),
        "cacheHits": int(extraction.get("cacheHitCount") or 0),
        "cacheMisses": int(extraction.get("cacheMissCount") or 0),
        "pendingChunks": int(extraction.get("failedChunkCount") or 0),
        "coverageRatio": float(extraction.get("coverageRatio") or 0),
        "failureReasons": dict(extraction.get("failureReasons") or {}),
        "circuitOpenReason": str(extraction.get("circuitOpenReason") or ""),
        "autoResumeStatus": status,
        "autoResumeAttempt": attempt,
        "autoResumeLimit": limit,
        "manualResumeAvailable": status == "paused",
        "recoveryMessage": message,
        "domainTreeReady": True,
    }


def _research_arguments(payload: ResearchChatRequest) -> dict[str, Any]:
    arguments = ProjectScopeService(settings.hunter_metadata_db).build_research_arguments(
        project_id=payload.project_id,
        project_ids=payload.project_ids,
        requested_paper_ids=payload.paper_ids,
        history=[message.model_dump() for message in payload.history],
    )
    arguments["response_context"] = research_memory_store.build_response_context(
        project_ids=arguments["project_ids"],
    )
    return arguments


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

    def report(progress: int, stage: str, message: str) -> None:
        context.progress(progress, stage=stage, message=message)

    result = asyncio.run(
        OrchestratorAgent(log_callback=log, progress_callback=report).run(
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

    recovery_state: dict[str, Any] = {}

    def report(update: dict[str, Any]) -> None:
        completed = int(update.get("completedChunks") or 0)
        total = int(update.get("totalChunks") or 0)
        progress = int(completed * 90 / total) + 5 if total else 5
        safe_update = {key: value for key, value in update.items() if key != "partialResult"}
        safe_update.update(recovery_state)
        context.progress(
            progress,
            stage=str(update.get("stage") or "building"),
            message=str(update.get("message") or "正在构建领域树与知识图谱"),
            details=safe_update,
        )

    def execute(*, resume: bool) -> list[dict[str, Any]] | None:
        if resume:
            return agent.resume_knowledge_graph_sync(
                payload.project_id,
                model=payload.model or model_payload,
                semantic_max_output_tokens=payload.semantic_max_output_tokens,
                cancel_event=context.cancel_event,
                progress_callback=report,
                metric_callback=context.record_model_call,
            )
        return agent.handle_domain_tree_sync(
            payload.project_id,
            action=payload.action,
            all_toc=payload.all_toc,
            new_toc=payload.new_toc,
            model=payload.model or model_payload,
            language=payload.language,
            primary_heading_count=payload.primary_heading_count,
            secondary_heading_count=payload.secondary_heading_count,
            max_output_tokens=payload.max_output_tokens,
            semantic_max_output_tokens=payload.semantic_max_output_tokens,
            delete_toc=payload.delete_toc,
            cancel_event=context.cancel_event,
            progress_callback=report,
            metric_callback=context.record_model_call,
        )

    auto_resume_limit = settings.semantic_graph_auto_resume_attempts
    auto_resume_attempt = 0
    try:
        tags = execute(resume=payload.action == "resume")
    except KnowledgeGraphQualityError as initial_error:
        quality_error = initial_error
        while auto_resume_attempt < auto_resume_limit:
            should_resume, decision_message = _semantic_auto_resume_decision(
                quality_error.extraction
            )
            if not should_resume:
                break
            auto_resume_attempt += 1
            recovery_state.clear()
            recovery_state.update(
                {
                    "autoResumeStatus": "running",
                    "autoResumeAttempt": auto_resume_attempt,
                    "autoResumeLimit": auto_resume_limit,
                    "resumeMode": "automatic",
                }
            )
            details = _semantic_recovery_details(
                quality_error.extraction,
                status="running",
                attempt=auto_resume_attempt,
                limit=auto_resume_limit,
                message=decision_message,
            )
            context.progress(
                5,
                stage="semantic_auto_resume",
                message=f"正在自动继续语义抽取（{auto_resume_attempt}/{auto_resume_limit}）",
                details={**details, **recovery_state},
            )
            delay = settings.semantic_graph_auto_resume_delay_seconds * (
                2 ** (auto_resume_attempt - 1)
            )
            if delay and context.cancel_event.wait(delay):
                context.check_cancelled()
            try:
                tags = execute(resume=True)
                break
            except KnowledgeGraphQualityError as next_error:
                quality_error = next_error
        else:
            decision_message = "自动恢复次数已用尽"

        if "tags" not in locals():
            _should_resume, decision_message = _semantic_auto_resume_decision(
                quality_error.extraction
            )
            details = _semantic_recovery_details(
                quality_error.extraction,
                status="paused",
                attempt=auto_resume_attempt,
                limit=auto_resume_limit,
                message=decision_message,
            )
            context.progress(
                5,
                stage="semantic_resume_paused",
                message="语义自动恢复已暂停，可调整模型后继续失败分块",
                details=details,
            )
            raise quality_error

    if not tags:
        raise ValueError("未找到可用于生成领域树的 Markdown 或目录数据")
    domain_tree_path = agent.get_result_path(payload.project_id)
    if not domain_tree_path.exists():
        raise RuntimeError("领域树已生成，但读取结果失败")
    graph_path = domain_tree_path.parent / "knowledge_graph.json"
    manifest_path = domain_tree_path.parent / "manifest.json"
    result = agent.get_result(payload.project_id) or {}
    graph = result.get("knowledgeGraph") if isinstance(result.get("knowledgeGraph"), dict) else {}
    extraction = graph.get("extraction") if isinstance(graph.get("extraction"), dict) else {}
    return {
        "projectId": payload.project_id,
        "domainTreePath": str(domain_tree_path),
        "knowledgeGraphPath": str(graph_path) if graph_path.exists() else None,
        "manifestPath": str(manifest_path) if manifest_path.exists() else None,
        "quality": extraction,
        "resume": {
            "mode": "manual" if payload.action == "resume" else (
                "automatic" if auto_resume_attempt else "none"
            ),
            "attempts": auto_resume_attempt,
        },
        "modelUsage": context.model_usage_summary(),
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


def _zotero_sync(context: BackgroundJobContext, raw: dict[str, Any]) -> dict[str, Any]:
    source_id = str(raw.get("sourceId") or raw.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("sourceId is required")
    context.progress(2, stage="connecting", message="正在连接 Zotero Local API")

    def report(progress: int, stage: str, message: str) -> None:
        context.progress(progress, stage=stage, message=message)

    return ZoteroSyncService(
        log_callback=context.log,
        progress_callback=report,
    ).sync(source_id, cancel_event=context.cancel_event)


def register_background_job_handlers(manager: BackgroundJobManager) -> None:
    """在应用组合根注册业务处理器，避免调度器反向依赖 API 路由。"""
    manager.register("dataset_download", _dataset_download)
    manager.register("research_chat", _research_chat)
    manager.register("domain_tree", _domain_tree)
    manager.register("pdf_import", _pdf_import)
    manager.register("zotero_sync", _zotero_sync)


__all__ = ["register_background_job_handlers"]
