"""融合 BM25 与向量相似度，为研究问答召回本地证据片段。"""

from __future__ import annotations

import html
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from app.services.embedding_store import (
    EmbeddingClient,
    SQLiteVectorStore,
    cosine_similarity,
    tfidf_cosine_scores,
)
from app.services.evidence_assembler import EvidenceAssembler
from app.services.candidate_retriever import CandidateRetriever
from app.services.document_structure_indexer import DocumentStructureIndexer
from app.services.rag_chunking import BaseMarkdownBlock, MarkdownRAGChunker
from app.services.split import parse_markdown_sections


_EXCLUDED_CATEGORIES = {
    "references", "reference", "bibliography", "acknowledgements", "acknowledgments",
    "funding", "funding_statement", "conflict_of_interest", "competing_interests",
    "venue_description", "journal_description", "conference_description",
}
_EXCLUDED_SECTION_PATTERNS = (
    "references", "bibliography", "acknowledgement", "acknowledgment", "funding",
    "fund support", "conflict of interest", "competing interest", "declaration of interest",
    "journal description", "about the journal", "conference description", "about the conference",
    "参考文献", "引用文献", "致谢", "基金支持", "基金项目", "利益冲突", "竞争性利益",
    "期刊说明", "期刊简介", "会议说明", "会议简介",
)


@dataclass(slots=True)
class EvidenceChunk:
    """表示一个可排序、可追溯到论文来源的证据片段。"""
    record_id: str
    title: str
    text: str
    score: float
    year: str = ""
    source: str = ""
    url: str = ""
    section: str = ""
    chunk_index: int = 0
    token_count: int = 0
    overlap_token_count: int = 0
    base_chunk_indices: list[int] | None = None
    summary: str = ""
    semantic_type: str = "prose"
    structure_id: str = ""
    structure_sequence: int = 0
    continues_from: str | None = None
    continues_to: str | None = None
    is_structure_start: bool = False
    is_structure_end: bool = False

    def to_dict(self) -> dict[str, Any]:
        """把当前数据对象转换为接口可返回的字典。"""
        return {
            "record_id": self.record_id,
            "title": self.title,
            "text": self.text,
            "score": self.score,
            "year": self.year,
            "source": self.source,
            "url": self.url,
            "section": self.section,
            "chunk_index": self.chunk_index,
            "token_count": self.token_count,
            "overlap_token_count": self.overlap_token_count,
            "base_chunk_indices": self.base_chunk_indices or [],
            "semantic_type": self.semantic_type,
            "structure_id": self.structure_id,
            "structure_sequence": self.structure_sequence,
            "continues_from": self.continues_from,
            "continues_to": self.continues_to,
            "is_structure_start": self.is_structure_start,
            "is_structure_end": self.is_structure_end,
        }


@dataclass(slots=True)
class CandidateSearchResult:
    """候选搜索阶段的输出，不包含最终证据选择决策。"""

    query: str
    candidates: list[EvidenceChunk]
    ranked: list[EvidenceChunk]
    query_tokens: list[str]
    diagnostics: dict[str, Any]


class RAGRetriever:
    """面向中英文论文分块的本地 BM25 检索器。"""

    def __init__(
        self,
        *,
        target_chunk_tokens: int = 500,
        max_chunk_tokens: int = 700,
        overlap_tokens: int = 80,
        max_chunks: int = 6,
        max_context_chars: int = 18000,
        max_chunks_per_paper: int = 2,
        embedding_client: EmbeddingClient | None = None,
        embedding_clients: list[EmbeddingClient] | None = None,
        vector_store: SQLiteVectorStore | None = None,
        bm25_weight: float = 0.45,
        vector_weight: float = 0.55,
    ) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.structure_indexer = DocumentStructureIndexer(
            target_tokens=target_chunk_tokens,
            max_tokens=max_chunk_tokens,
            overlap_tokens=overlap_tokens,
        )
        self.chunker = self.structure_indexer.chunker
        self.max_chunks = max(1, max_chunks)
        self.max_context_chars = max(1000, max_context_chars)
        self.max_chunks_per_paper = max(1, max_chunks_per_paper)
        self.embedding_clients = [
            client
            for client in [*(embedding_clients or []), *([embedding_client] if embedding_client else [])]
            if client is not None
        ]
        self.vector_store = vector_store
        total_weight = max(0.0001, bm25_weight + vector_weight)
        self.bm25_weight = bm25_weight / total_weight
        self.vector_weight = vector_weight / total_weight
        self.last_retrieval_mode = "bm25"
        self.last_diagnostics: dict[str, Any] = {}
        self.evidence_assembler = EvidenceAssembler(
            max_chunks=self.max_chunks,
            max_context_chars=self.max_context_chars,
            tokenize=self._tokenize,
        )
        self.candidate_retriever = CandidateRetriever(
            index_paper=lambda paper: self.structure_indexer.index_paper(paper, chunk_factory=EvidenceChunk),
            tokenize=self._tokenize,
            searchable_text=self._searchable_text,
            embedding_clients=self.embedding_clients,
            vector_store=self.vector_store,
            bm25_weight=self.bm25_weight,
            vector_weight=self.vector_weight,
        )

    def search_candidates(
        self,
        query: str,
        papers: list[dict[str, Any]],
        *,
        section_score_adjuster: Callable[[str], float] | None = None,
        chunk_score_adjuster: Callable[[EvidenceChunk], float] | None = None,
    ) -> CandidateSearchResult:
        """兼容门面：把宽候选召回委托给独立 CandidateRetriever。"""
        batch = self.candidate_retriever.retrieve(
            query,
            papers,
            section_score_adjuster=section_score_adjuster,
            chunk_score_adjuster=chunk_score_adjuster,
        )
        self.last_retrieval_mode = str(batch.diagnostics.get("retrievalMode") or "bm25")
        return CandidateSearchResult(
            query=batch.query,
            candidates=batch.candidates,
            ranked=batch.ranked,
            query_tokens=batch.query_tokens,
            diagnostics=batch.diagnostics,
        )

    def retrieve(
        self,
        query: str,
        papers: list[dict[str, Any]],
        *,
        minimum_evidence_count: int | None = None,
        section_score_adjuster: Callable[[str], float] | None = None,
        chunk_score_adjuster: Callable[[EvidenceChunk], float] | None = None,
    ) -> list[dict[str, Any]]:
        """兼容门面：先搜索候选，再由证据组装器执行最终选择。"""
        search_result = self.search_candidates(
            query,
            papers,
            section_score_adjuster=section_score_adjuster,
            chunk_score_adjuster=chunk_score_adjuster,
        )
        required_capacity = max(0, int(minimum_evidence_count or 0))
        effective_max_groups_per_paper = self._effective_max_groups_per_paper(
            search_result.ranked,
            minimum_evidence_count=required_capacity,
        )
        assembly = self.evidence_assembler.assemble(
            search_result.ranked,
            search_result.candidates,
            max_groups_per_paper=effective_max_groups_per_paper,
        )
        selected = assembly.evidence
        selected_text = " ".join(self._searchable_text(item).lower() for item in selected)
        unique_query_tokens = set(search_result.query_tokens)
        matched_query_tokens = {token for token in unique_query_tokens if token in selected_text}
        self.last_diagnostics = {
            **search_result.diagnostics,
            **assembly.diagnostics,
            "evidenceCount": len(selected),
            "distinctPaperCount": len({item.record_id for item in selected}),
            "selectedPaperIds": list(dict.fromkeys(item.record_id for item in selected)),
            "selectedContinuationCount": sum(
                bool(item.continues_from or item.continues_to) for item in selected
            ),
            "configuredMaxChunksPerPaper": self.max_chunks_per_paper,
            "effectiveMaxChunksPerPaper": effective_max_groups_per_paper,
            "configuredMaxGroupsPerPaper": self.max_chunks_per_paper,
            "effectiveMaxGroupsPerPaper": effective_max_groups_per_paper,
            "minimumEvidenceCount": required_capacity,
            "queryCoverage": round(len(matched_query_tokens) / max(1, len(unique_query_tokens)), 4),
            "topScore": round(selected[0].score, 4) if selected else 0.0,
        }
        return [item.to_dict() for item in selected]

    def _effective_max_groups_per_paper(
        self,
        ranked: list[EvidenceChunk],
        *,
        minimum_evidence_count: int,
    ) -> int:
        """保证每篇逻辑证据组上限不会与最低证据数形成不可达配置。"""
        ranked_paper_count = len({item.record_id for item in ranked})
        if minimum_evidence_count <= 0 or ranked_paper_count == 0:
            return self.max_chunks_per_paper
        # 单篇论文查询可以选取多个互补片段；多论文查询仍优先保持来源多样性。
        required_per_paper = math.ceil(minimum_evidence_count / ranked_paper_count)
        return max(self.max_chunks_per_paper, required_per_paper)

    def build_context(self, evidence: list[dict[str, Any]]) -> str:
        """把证据列表整理成适合发送给大模型的上下文文本。"""
        blocks: list[str] = []
        for index, item in enumerate(evidence, start=1):
            metadata = [str(item.get("title") or "未命名文献")]
            if item.get("year"):
                metadata.append(str(item["year"]))
            if item.get("section"):
                metadata.append(f"章节：{item['section']}")
            if item.get("structure_id"):
                position = int(item.get("structure_sequence") or 0) + 1
                relation = ""
                if item.get("continues_from") and item.get("continues_to"):
                    relation = "，前后均有连续片段"
                elif item.get("continues_from"):
                    relation = "，承接上一片段"
                elif item.get("continues_to"):
                    relation = "，后续仍有连续片段"
                metadata.append(
                    f"连续结构：{str(item.get('semantic_type') or 'structure')} 第 {position} 段{relation}"
                )
            if item.get("graph_backed"):
                metadata.append("检索路径：知识图谱导航后已回查原文")
            blocks.append(f"[{index}] {' · '.join(metadata)}\n{str(item.get('text') or '').strip()}")
        return "\n\n---\n\n".join(blocks)

    def resolve_chunk_references(
        self,
        papers: list[dict[str, Any]],
        references: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按 recordId 与 chunkIndex 精确恢复历史对话引用的证据片段。"""
        requested = [
            (str(item.get("record_id") or item.get("recordId") or ""), int(item.get("chunk_index") or item.get("chunkIndex") or 0))
            for item in references
            if str(item.get("record_id") or item.get("recordId") or "").strip()
        ]
        if not requested:
            return []
        requested_set = set(requested)
        matches: dict[tuple[str, int], EvidenceChunk] = {}
        for paper in papers:
            for chunk in self._paper_chunks(paper):
                key = (chunk.record_id, chunk.chunk_index)
                if key in requested_set:
                    chunk.score = max(1.0, float(chunk.score))
                    matches[key] = chunk
        return [matches[key].to_dict() for key in requested if key in matches]

    def resolve_quote_references(
        self,
        papers: list[dict[str, Any]],
        references: list[dict[str, Any]],
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """用 `documentId + evidenceQuote` 把图谱导航结果回定位到当前 RAG 原文块。"""
        # 图谱导航需要先核验全部候选，再由上层裁剪最终回答证据。
        safe_limit = max(1, int(limit))
        chunks_by_paper = {
            str(paper.get("id") or paper.get("recordId") or ""): self._paper_chunks(paper)
            for paper in papers
            if str(paper.get("id") or paper.get("recordId") or "")
        }
        resolved: dict[tuple[str, int], dict[str, Any]] = {}
        for reference in references:
            record_id = str(reference.get("recordId") or reference.get("record_id") or "").strip()
            quote = self._normalize_quote(reference.get("quote"))
            if not record_id or not quote or record_id not in chunks_by_paper:
                continue
            expected_section = str(reference.get("section") or "").strip().casefold()
            matches: list[tuple[float, EvidenceChunk]] = []
            for chunk in chunks_by_paper[record_id]:
                normalized_text = self._normalize_quote(chunk.text)
                if quote not in normalized_text:
                    continue
                score = float(reference.get("relevanceScore") or 0) + 1.0
                if expected_section and expected_section in chunk.section.casefold():
                    score += 0.15
                matches.append((score, chunk))
            if not matches:
                continue
            score, chunk = max(matches, key=lambda item: item[0])
            key = (record_id, chunk.chunk_index)
            item = resolved.setdefault(
                key,
                {
                    **chunk.to_dict(),
                    "score": max(float(chunk.score), score),
                    "graph_backed": True,
                    "retrieval_channels": ["graph_navigation", "original_text"],
                    "graph_evidence_ids": [],
                    "graph_relation_ids": [],
                    "graph_navigation_claims": [],
                    "graph_quotes": [],
                },
            )
            item["score"] = max(float(item.get("score") or 0), score)
            graph_evidence_id = str(reference.get("graphEvidenceId") or "")
            if graph_evidence_id and graph_evidence_id not in item["graph_evidence_ids"]:
                item["graph_evidence_ids"].append(graph_evidence_id)
            for relation_id in reference.get("relationIds") or []:
                relation_id = str(relation_id)
                if relation_id and relation_id not in item["graph_relation_ids"]:
                    item["graph_relation_ids"].append(relation_id)
            for claim in reference.get("navigationClaims") or []:
                claim = str(claim).strip()
                if claim and claim not in item["graph_navigation_claims"]:
                    item["graph_navigation_claims"].append(claim)
            raw_quote = str(reference.get("quote") or "").strip()
            if raw_quote and raw_quote not in item["graph_quotes"]:
                item["graph_quotes"].append(raw_quote)

        return sorted(
            resolved.values(),
            key=lambda item: float(item.get("score") or 0),
            reverse=True,
        )[:safe_limit]

    def list_paper_sections(self, paper: dict[str, Any], *, limit: int = 20) -> list[dict[str, Any]]:
        """以稳定的只读结构返回论文分块章节，供目录工具复用。"""
        safe_limit = max(1, min(int(limit), 50))
        return [
            {
                "chunkIndex": chunk.chunk_index,
                "section": chunk.section,
                "summary": chunk.summary,
                "tokenCount": chunk.token_count,
                "semanticType": chunk.semantic_type,
                "structureId": chunk.structure_id,
                "structureSequence": chunk.structure_sequence,
                "continuesFrom": chunk.continues_from,
                "continuesTo": chunk.continues_to,
                "excerpt": chunk.text[:1200],
            }
            for chunk in self._paper_chunks(paper)[:safe_limit]
        ]

    def _paper_chunks(self, paper: dict[str, Any]) -> list[EvidenceChunk]:
        """把单篇论文整理为可检索的证据片段。"""
        return self.structure_indexer.index_paper(paper, chunk_factory=EvidenceChunk)

    def _read_markdown(self, paper: dict[str, Any]) -> str:
        """读取Markdown。"""
        return self.structure_indexer.read_markdown(paper)

    def _is_excluded(self, *, category: str, section: str, text: str) -> bool:
        """判断片段是否属于参考文献等应排除内容。"""
        return self.structure_indexer.is_excluded(category=category, section=section, text=text)

    def _searchable_text(self, chunk: EvidenceChunk) -> str:
        """拼接证据片段中参与检索的字段。"""
        return f"{chunk.title} {chunk.section} {chunk.summary} {chunk.text}"

    @staticmethod
    def _normalize_quote(value: Any) -> str:
        """以保守方式规范空白和 HTML 实体，保持逐字回查语义。"""
        return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()

    def _tokenize(self, text: str) -> list[str]:
        """按中英文规则把文本切分为检索词元。"""
        lowered = str(text).lower()
        tokens = re.findall(r"[a-z0-9][a-z0-9+._-]{1,}", lowered)
        for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
            if len(sequence) == 1:
                tokens.append(sequence)
            else:
                tokens.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
        return tokens

    def _bm25(
        self,
        *,
        query_tokens: list[str],
        document_tokens: list[str],
        document_frequency: Counter[str],
        document_count: int,
        average_length: float,
    ) -> float:
        """计算查询与文档集合之间的 BM25 得分。"""
        frequencies = Counter(document_tokens)
        length = len(document_tokens)
        score = 0.0
        for token in set(query_tokens):
            frequency = frequencies[token]
            if frequency == 0:
                continue
            inverse_frequency = math.log(1 + (document_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * length / max(1, average_length))
            score += inverse_frequency * frequency * 2.5 / denominator
        return score

    def _normalize_scores(self, scores: list[float]) -> list[float]:
        """规范化得分。"""
        if not scores:
            return []
        minimum = min(scores)
        maximum = max(scores)
        if maximum <= minimum:
            return [1.0 if value > 0 else 0.0 for value in scores]
        return [(value - minimum) / (maximum - minimum) for value in scores]


__all__ = ["RAGRetriever", "EvidenceChunk", "CandidateSearchResult"]
