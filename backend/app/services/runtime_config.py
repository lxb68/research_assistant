"""生成可安全展示给前端的后端运行配置快照。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings


def _configured(value: object) -> bool:
    return bool(str(value or "").strip())


def get_public_runtime_config() -> dict[str, Any]:
    """返回非敏感参数与凭据配置状态，绝不返回凭据原文。"""
    return {
        "restartRequired": True,
        "server": {
            "host": settings.host,
            "port": settings.port,
            "corsOrigins": list(settings.cors_origins),
            "requestTimeoutSeconds": settings.request_timeout,
            "logLevel": settings.log_level,
        },
        "research": {
            "maxPapers": settings.research_agent_max_papers,
            "maxSources": settings.research_agent_max_sources,
            "maxContextChars": settings.research_agent_max_context_chars,
            "requestTimeoutSeconds": settings.research_agent_request_timeout,
            "minimumEvidence": settings.orchestrator_min_evidence,
            "maxRetrievalRounds": settings.orchestrator_max_retrieval_rounds,
            "maxActionRounds": settings.orchestrator_max_action_rounds,
            "searchLimitPerSource": settings.orchestrator_search_limit_per_source,
        },
        "retrieval": {
            "chunkTargetTokens": settings.rag_chunk_target_tokens,
            "chunkMaxTokens": settings.rag_chunk_max_tokens,
            "chunkOverlapTokens": settings.rag_chunk_overlap_tokens,
            "bm25Weight": settings.rag_bm25_weight,
            "vectorWeight": settings.rag_vector_weight,
            "embeddingModel": settings.rag_embedding_model,
            "embeddingBaseUrl": settings.rag_embedding_base_url,
            "embeddingConfigured": _configured(settings.rag_embedding_api_key),
            "localEmbeddingModel": settings.rag_local_embedding_model,
            "localEmbeddingBaseUrl": settings.rag_local_embedding_base_url,
            "localEmbeddingProtocol": settings.rag_local_embedding_protocol,
            "hybridGraphEnabled": settings.hybrid_graph_enabled,
            "hybridGraphProjectId": settings.hybrid_graph_project_id,
        },
        "documents": {
            "splitMinimumLength": settings.split_min_length,
            "splitMaximumLength": settings.split_max_length,
            "mineruApiBase": settings.mineru_api_base,
            "mineruModelVersion": settings.mineru_model_version,
            "mineruTokenConfigured": _configured(settings.mineru_api_token),
            "mineruLocalFallback": settings.mineru_enable_local_cli_fallback,
        },
        "workers": {
            "backgroundJobWorkers": settings.background_job_max_workers,
            "backgroundJobPendingLimit": settings.background_job_max_pending_tasks,
            "streamWorkers": settings.stream_max_workers,
            "streamPendingLimit": settings.stream_max_pending_tasks,
            "semanticGraphWorkers": settings.semantic_graph_max_workers,
        },
        "storage": {
            "backend": settings.backend_storage_dir,
            "papers": settings.hunter_download_dir,
            "markdown": settings.mineru_output_dir,
            "paperDatabase": settings.hunter_metadata_db,
            "vectorDatabase": settings.rag_vector_store_path,
        },
        "integrations": [
            {
                "id": "pubmed",
                "name": "PubMed / NCBI",
                "configured": _configured(settings.ncbi_email) or _configured(settings.ncbi_api_key),
                "details": "邮箱或 API Key 已配置" if _configured(settings.ncbi_email) or _configured(settings.ncbi_api_key) else "未配置邮箱和 API Key",
            },
            {
                "id": "ieee",
                "name": "IEEE Xplore",
                "configured": _configured(settings.ieee_api_key),
                "details": "API Key 已配置" if _configured(settings.ieee_api_key) else "API Key 未配置",
            },
            {
                "id": "semantic_scholar",
                "name": "Semantic Scholar",
                "configured": _configured(settings.semantic_scholar_api_key),
                "details": "API Key 已配置" if _configured(settings.semantic_scholar_api_key) else "API Key 未配置",
            },
            {
                "id": "mineru",
                "name": "MinerU",
                "configured": _configured(settings.mineru_api_token),
                "details": "云端 Token 已配置" if _configured(settings.mineru_api_token) else "云端 Token 未配置",
            },
            {
                "id": "tencent_translation",
                "name": "腾讯云翻译",
                "configured": _configured(settings.tencent_translation_secret_id) and _configured(settings.tencent_translation_secret_key),
                "details": "SecretId / SecretKey 已配置" if _configured(settings.tencent_translation_secret_id) and _configured(settings.tencent_translation_secret_key) else "凭据不完整",
            },
            {
                "id": "remote_embedding",
                "name": "远程 Embedding",
                "configured": _configured(settings.rag_embedding_api_key),
                "details": "API Key 已配置" if _configured(settings.rag_embedding_api_key) else "API Key 未配置，将尝试本地后端",
            },
        ],
    }


__all__ = ["get_public_runtime_config"]
