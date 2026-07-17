"""融合 BM25 与向量相似度，为研究问答召回本地证据片段。"""

from __future__ import annotations

import html
import math
import re
from collections import Counter, defaultdict
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
        self.chunker = MarkdownRAGChunker(
            target_tokens=target_chunk_tokens,
            max_tokens=max_chunk_tokens,
            overlap_tokens=overlap_tokens,
        )
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

    def retrieve(
        self,
        query: str,
        papers: list[dict[str, Any]],
        *,
        minimum_evidence_count: int | None = None,
        section_score_adjuster: Callable[[str], float] | None = None,
        chunk_score_adjuster: Callable[[EvidenceChunk], float] | None = None,
    ) -> list[dict[str, Any]]:
        """融合稀疏与向量得分，返回去冗余后的高相关证据。"""
        normalized_query = str(query).strip()
        if not normalized_query:
            return []
        candidates = [chunk for paper in papers for chunk in self._paper_chunks(paper)]
        if not candidates:
            return []

        query_tokens = self._tokenize(normalized_query)
        if not query_tokens:
            return []
        tokenized = [self._tokenize(self._searchable_text(chunk)) for chunk in candidates]
        document_frequency: Counter[str] = Counter()
        for tokens in tokenized:
            document_frequency.update(set(tokens))
        average_length = sum(len(tokens) for tokens in tokenized) / max(1, len(tokenized))

        bm25_scores: list[float] = []
        for chunk, tokens in zip(candidates, tokenized, strict=True):
            score = self._bm25(
                query_tokens=query_tokens,
                document_tokens=tokens,
                document_frequency=document_frequency,
                document_count=len(candidates),
                average_length=average_length,
            )
            lowered_title = chunk.title.lower()
            lowered_section = chunk.section.lower()
            for token in set(query_tokens):
                if token in lowered_title:
                    score += 1.2
                if chunk.section and token in lowered_section:
                    score += 0.65
            bm25_scores.append(score)

        vector_scores = [0.0] * len(candidates)
        searchable = [self._searchable_text(chunk) for chunk in candidates]
        embedding_failures: list[str] = []
        active_embedding_backend = ""
        self.last_retrieval_mode = "hybrid_tfidf"
        if self.vector_store:
            for embedding_client in self.embedding_clients:
                if not embedding_client.configured:
                    continue
                try:
                    candidate_vectors = self.vector_store.get_or_create(searchable, client=embedding_client)
                    query_vector = embedding_client.embed([normalized_query])[0]
                    vector_scores = [cosine_similarity(query_vector, vector) for vector in candidate_vectors]
                    active_embedding_backend = embedding_client.provider
                    self.last_retrieval_mode = f"hybrid_{embedding_client.provider}"
                    break
                except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as error:
                    embedding_failures.append(f"{embedding_client.provider}:{type(error).__name__}")

        if not active_embedding_backend:
            # 百炼与本地嵌入均不可用时仍提供 TF-IDF 稀疏语义得分，不退化为单一 BM25。
            vector_scores = tfidf_cosine_scores(normalized_query, searchable)
            active_embedding_backend = "tfidf"
            self.last_retrieval_mode = "hybrid_tfidf"

        normalized_bm25 = self._normalize_scores(bm25_scores)
        normalized_vectors = self._normalize_scores(vector_scores)
        ranked: list[EvidenceChunk] = []
        for index, chunk in enumerate(candidates):
            if self.last_retrieval_mode.startswith("hybrid_"):
                chunk.score = self.bm25_weight * normalized_bm25[index] + self.vector_weight * normalized_vectors[index]
            else:
                chunk.score = normalized_bm25[index]
            if section_score_adjuster is not None:
                chunk.score += float(section_score_adjuster(chunk.section))
            if chunk_score_adjuster is not None:
                chunk.score += float(chunk_score_adjuster(chunk))
            if chunk.score > 0:
                ranked.append(chunk)
        ranked.sort(key=lambda item: item.score, reverse=True)
        required_capacity = max(0, int(minimum_evidence_count or 0))
        effective_max_chunks_per_paper = self._effective_max_chunks_per_paper(
            ranked,
            minimum_evidence_count=required_capacity,
        )
        selected = self._select_diverse(
            ranked,
            max_chunks_per_paper=effective_max_chunks_per_paper,
        )
        selected = self._complete_selected_structures(selected, candidates)
        selected_text = " ".join(self._searchable_text(item).lower() for item in selected)
        unique_query_tokens = set(query_tokens)
        matched_query_tokens = {token for token in unique_query_tokens if token in selected_text}
        candidate_token_counts = [item.token_count for item in candidates if item.token_count > 0]
        self.last_diagnostics = {
            "retrievalMode": self.last_retrieval_mode,
            "embeddingBackend": active_embedding_backend,
            "embeddingFailures": embedding_failures,
            "chunkingStrategy": "mineru_structure_semantic_token_overlap",
            "candidateCount": len(candidates),
            "averageChunkTokens": round(
                sum(candidate_token_counts) / max(1, len(candidate_token_counts)),
                2,
            ),
            "maxChunkTokens": max(candidate_token_counts, default=0),
            "overlappedChunkCount": sum(item.overlap_token_count > 0 for item in candidates),
            "structuredCandidateCount": sum(bool(item.structure_id) for item in candidates),
            "selectedStructureCount": len({
                (item.record_id, item.structure_id)
                for item in selected
                if item.structure_id
            }),
            "selectedContinuationCount": sum(
                bool(item.continues_from or item.continues_to) for item in selected
            ),
            "evidenceCount": len(selected),
            "distinctPaperCount": len({item.record_id for item in selected}),
            "selectedPaperIds": list(dict.fromkeys(item.record_id for item in selected)),
            "configuredMaxChunksPerPaper": self.max_chunks_per_paper,
            "effectiveMaxChunksPerPaper": effective_max_chunks_per_paper,
            "minimumEvidenceCount": required_capacity,
            "queryCoverage": round(len(matched_query_tokens) / max(1, len(unique_query_tokens)), 4),
            "topScore": round(selected[0].score, 4) if selected else 0.0,
        }
        return [item.to_dict() for item in selected]

    def _complete_selected_structures(
        self,
        selected: list[EvidenceChunk],
        candidates: list[EvidenceChunk],
    ) -> list[EvidenceChunk]:
        """命中连续结构时优先携带相邻分片，避免只返回算法、表格或代码的中段。"""
        if not selected or not any(item.structure_id for item in selected):
            return selected

        members: dict[tuple[str, str], list[EvidenceChunk]] = defaultdict(list)
        for candidate in candidates:
            if candidate.structure_id:
                members[(candidate.record_id, candidate.structure_id)].append(candidate)
        for values in members.values():
            values.sort(key=lambda item: item.structure_sequence)

        completed: list[EvidenceChunk] = []
        seen: set[tuple[str, int]] = set()
        context_size = 0

        def append(item: EvidenceChunk) -> bool:
            nonlocal context_size
            key = (item.record_id, item.chunk_index)
            if key in seen:
                return False
            if len(completed) >= self.max_chunks:
                return False
            if context_size + len(item.text) > self.max_context_chars:
                return False
            completed.append(item)
            seen.add(key)
            context_size += len(item.text)
            return True

        expanded_structures: set[tuple[str, str]] = set()
        for seed in selected:
            structure_key = (seed.record_id, seed.structure_id)
            if seed.structure_id and structure_key not in expanded_structures:
                expanded_structures.add(structure_key)
                structure_members = members.get(structure_key, [seed])
                remaining_capacity = self.max_chunks - len(completed)
                seed_position = next(
                    (
                        index
                        for index, member in enumerate(structure_members)
                        if member.chunk_index == seed.chunk_index
                    ),
                    0,
                )
                # 超长结构无法一次装入时，选择包含命中片段的连续窗口，不能只取结构开头。
                window_start = max(
                    0,
                    min(
                        seed_position - remaining_capacity // 2,
                        len(structure_members) - remaining_capacity,
                    ),
                )
                structure_window = structure_members[
                    window_start : window_start + remaining_capacity
                ]
                for member in structure_window:
                    append(member)
                continue
            append(seed)

        # 若结构补全仍有余量，继续保留原排序中的非结构证据。
        for seed in selected:
            append(seed)
        return completed

    def _effective_max_chunks_per_paper(
        self,
        ranked: list[EvidenceChunk],
        *,
        minimum_evidence_count: int,
    ) -> int:
        """保证每篇上限不会与最低证据数形成不可达配置。"""
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
        base = {
            "record_id": str(paper.get("id") or paper.get("recordId") or ""),
            "title": str(paper.get("title") or "未命名文献"),
            "year": str(paper.get("year") or ""),
            "source": str(paper.get("source") or ""),
            "url": str(paper.get("url") or ""),
        }
        split_chunks = paper.get("splitChunks") or paper.get("split_chunks")
        if isinstance(split_chunks, list):
            base_blocks: list[BaseMarkdownBlock] = []
            for index, raw in enumerate(split_chunks):
                if not isinstance(raw, dict):
                    continue
                content = str(raw.get("content") or "").strip()
                if not content:
                    continue
                headings = raw.get("headings") if isinstance(raw.get("headings"), list) else []
                section = " > ".join(
                    str(heading.get("heading") or "").strip()
                    for heading in headings
                    if isinstance(heading, dict) and str(heading.get("heading") or "").strip()
                )
                summary = str(raw.get("summary") or "").strip()
                category = str(raw.get("semanticCategory") or raw.get("semantic_category") or "").strip().lower()
                semantic_type = str(raw.get("semanticType") or raw.get("semantic_type") or "prose").strip().lower()
                structure_id = str(raw.get("structureId") or raw.get("structure_id") or "").strip()
                if self._is_excluded(category=category, section=section, text=f"{summary}\n{content}"):
                    continue
                base_blocks.append(
                    BaseMarkdownBlock(
                        content=content,
                        index=index,
                        headings=headings,
                        summary=summary,
                        semantic_category=category or "body",
                        semantic_type=semantic_type or "prose",
                        structure_id=structure_id,
                        structure_part_index=int(raw.get("structurePartIndex") or raw.get("structure_part_index") or 0),
                        structure_part_count=int(raw.get("structurePartCount") or raw.get("structure_part_count") or 0),
                        continues_from=raw.get("continuesFrom") or raw.get("continues_from"),
                        continues_to=raw.get("continuesTo") or raw.get("continues_to"),
                    )
                )
            if base_blocks:
                outline = paper.get("splitOutline") or paper.get("split_outline") or []
                prepared = self.chunker.build(
                    base_blocks,
                    outline=outline if isinstance(outline, list) else [],
                )
                return [
                    EvidenceChunk(
                        **base,
                        text=item.text,
                        score=0,
                        section=item.section,
                        chunk_index=index,
                        token_count=item.token_count,
                        overlap_token_count=item.overlap_token_count,
                        base_chunk_indices=item.base_chunk_indices,
                        summary=item.summary,
                        semantic_type=item.semantic_type,
                        structure_id=item.structure_id,
                        structure_sequence=item.structure_sequence,
                        continues_from=item.continues_from,
                        continues_to=item.continues_to,
                        is_structure_start=item.is_structure_start,
                        is_structure_end=item.is_structure_end,
                    )
                    for index, item in enumerate(prepared)
                ]

        content = self._read_markdown(paper) or str(paper.get("abstract") or "").strip()
        outline, sections = parse_markdown_sections(content)
        fallback_blocks: list[BaseMarkdownBlock] = []
        for index, section_data in enumerate(sections):
            section_content = str(section_data.get("content") or "").strip()
            heading = str(section_data.get("heading") or "").strip()
            heading_item = {
                "heading": heading,
                "level": int(section_data.get("level") or 1),
                "position": int(section_data.get("position") or index + 1),
            }
            if not section_content or self._is_excluded(category="", section=heading, text=section_content):
                continue
            fallback_blocks.append(
                BaseMarkdownBlock(
                    content=section_content,
                    index=index,
                    headings=[heading_item] if heading else [],
                )
            )
        prepared = self.chunker.build(fallback_blocks, outline=outline)
        return [
            EvidenceChunk(
                **base,
                text=item.text,
                score=0,
                section=item.section,
                chunk_index=index,
                token_count=item.token_count,
                overlap_token_count=item.overlap_token_count,
                base_chunk_indices=item.base_chunk_indices,
                summary=item.summary,
                semantic_type=item.semantic_type,
                structure_id=item.structure_id,
                structure_sequence=item.structure_sequence,
                continues_from=item.continues_from,
                continues_to=item.continues_to,
                is_structure_start=item.is_structure_start,
                is_structure_end=item.is_structure_end,
            )
            for index, item in enumerate(prepared)
        ]

    def _read_markdown(self, paper: dict[str, Any]) -> str:
        """读取Markdown。"""
        for key in ("markdownPath", "markdown_path"):
            path_value = str(paper.get(key) or "").strip()
            if path_value:
                path = Path(path_value)
                if path.exists() and path.is_file():
                    return path.read_text(encoding="utf-8", errors="ignore")
        output_dir = str(paper.get("markdownOutputDir") or paper.get("markdown_output_dir") or "").strip()
        if output_dir:
            path = Path(output_dir) / "full.md"
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8", errors="ignore")
        return ""

    def _is_excluded(self, *, category: str, section: str, text: str) -> bool:
        """判断片段是否属于参考文献等应排除内容。"""
        normalized_category = category.replace("-", "_").replace(" ", "_")
        if normalized_category in _EXCLUDED_CATEGORIES:
            return True
        normalized_section = section.lower().strip()
        if normalized_section and any(pattern in normalized_section for pattern in _EXCLUDED_SECTION_PATTERNS):
            return True
        first_line = next((line.strip(" #\t:：.-").lower() for line in text.splitlines() if line.strip()), "")
        return any(first_line.startswith(pattern) for pattern in _EXCLUDED_SECTION_PATTERNS)

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

    def _select_diverse(
        self,
        ranked: list[EvidenceChunk],
        *,
        max_chunks_per_paper: int | None = None,
    ) -> list[EvidenceChunk]:
        """从排序结果中选择来源和内容更丰富的证据。"""
        selected: list[EvidenceChunk] = []
        per_paper: defaultdict[str, int] = defaultdict(int)
        context_size = 0
        paper_limit = max(1, int(max_chunks_per_paper or self.max_chunks_per_paper))
        for candidate in ranked:
            if per_paper[candidate.record_id] >= paper_limit:
                continue
            if context_size + len(candidate.text) > self.max_context_chars:
                continue
            candidate_tokens = set(self._tokenize(candidate.text))
            if any(self._jaccard(candidate_tokens, set(self._tokenize(item.text))) > 0.82 for item in selected):
                continue
            selected.append(candidate)
            per_paper[candidate.record_id] += 1
            context_size += len(candidate.text)
            if len(selected) >= self.max_chunks:
                break
        return selected

    def _jaccard(self, left: set[str], right: set[str]) -> float:
        """计算两个词元集合的 Jaccard 相似度。"""
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    def _normalize_scores(self, scores: list[float]) -> list[float]:
        """规范化得分。"""
        if not scores:
            return []
        minimum = min(scores)
        maximum = max(scores)
        if maximum <= minimum:
            return [1.0 if value > 0 else 0.0 for value in scores]
        return [(value - minimum) / (maximum - minimum) for value in scores]


__all__ = ["RAGRetriever", "EvidenceChunk"]
