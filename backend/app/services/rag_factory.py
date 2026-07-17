"""集中构建知识库检索器，避免 Agent 与工具层重复装配依赖。"""

from __future__ import annotations

from app.core.config import settings
from app.services.embedding_store import EmbeddingClient, SQLiteVectorStore
from app.services.rag_retriever import RAGRetriever


def build_default_rag_retriever(
    *,
    target_chunk_tokens: int | None = None,
    max_chunk_tokens: int | None = None,
    overlap_tokens: int | None = None,
    max_chunks: int | None = None,
    max_context_chars: int | None = None,
) -> RAGRetriever:
    """按统一配置构建支持向量降级的本地知识库检索器。"""
    bailian_embedding_client = EmbeddingClient(
        base_url=settings.rag_embedding_base_url,
        api_key=settings.rag_embedding_api_key,
        model=settings.rag_embedding_model,
        timeout=settings.rag_embedding_timeout,
        provider="bailian",
        protocol="openai_compatible",
        batch_size=10,
        requires_api_key=True,
    )
    local_embedding_client = EmbeddingClient(
        base_url=settings.rag_local_embedding_base_url,
        api_key=settings.rag_local_embedding_api_key,
        model=settings.rag_local_embedding_model,
        timeout=settings.rag_local_embedding_timeout,
        provider=f"local_{settings.rag_local_embedding_protocol}",
        protocol=settings.rag_local_embedding_protocol,
        batch_size=16,
        requires_api_key=False,
    )
    return RAGRetriever(
        target_chunk_tokens=target_chunk_tokens or settings.rag_chunk_target_tokens,
        max_chunk_tokens=max_chunk_tokens or settings.rag_chunk_max_tokens,
        overlap_tokens=overlap_tokens if overlap_tokens is not None else settings.rag_chunk_overlap_tokens,
        max_chunks=max_chunks or settings.research_agent_max_sources,
        max_context_chars=max_context_chars or settings.research_agent_max_context_chars,
        max_chunks_per_paper=settings.rag_max_chunks_per_paper,
        embedding_clients=[bailian_embedding_client, local_embedding_client],
        vector_store=SQLiteVectorStore(settings.rag_vector_store_path),
        bm25_weight=settings.rag_bm25_weight,
        vector_weight=settings.rag_vector_weight,
    )


__all__ = ["build_default_rag_retriever"]
