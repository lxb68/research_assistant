from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from app.services.embedding_store import EmbeddingClient, SQLiteVectorStore, cosine_similarity


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
    record_id: str
    title: str
    text: str
    score: float
    year: str = ""
    source: str = ""
    url: str = ""
    section: str = ""
    chunk_index: int = 0

    def to_dict(self) -> dict[str, Any]:
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
        }


class RAGRetriever:
    """面向中英文论文分块的本地 BM25 检索器。"""

    def __init__(
        self,
        *,
        chunk_size: int = 1800,
        max_chunks: int = 6,
        max_context_chars: int = 18000,
        max_chunks_per_paper: int = 2,
        embedding_client: EmbeddingClient | None = None,
        vector_store: SQLiteVectorStore | None = None,
        bm25_weight: float = 0.45,
        vector_weight: float = 0.55,
    ) -> None:
        self.chunk_size = max(400, chunk_size)
        self.max_chunks = max(1, max_chunks)
        self.max_context_chars = max(1000, max_context_chars)
        self.max_chunks_per_paper = max(1, max_chunks_per_paper)
        self.embedding_client = embedding_client
        self.vector_store = vector_store
        total_weight = max(0.0001, bm25_weight + vector_weight)
        self.bm25_weight = bm25_weight / total_weight
        self.vector_weight = vector_weight / total_weight
        self.last_retrieval_mode = "bm25"
        self.last_diagnostics: dict[str, Any] = {}

    def retrieve(self, query: str, papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        self.last_retrieval_mode = "bm25"
        if self.embedding_client and self.embedding_client.configured and self.vector_store:
            try:
                searchable = [self._searchable_text(chunk) for chunk in candidates]
                candidate_vectors = self.vector_store.get_or_create(searchable, client=self.embedding_client)
                query_vector = self.embedding_client.embed([normalized_query])[0]
                vector_scores = [cosine_similarity(query_vector, vector) for vector in candidate_vectors]
                self.last_retrieval_mode = "hybrid"
            except (requests.RequestException, RuntimeError, ValueError):
                self.last_retrieval_mode = "bm25_fallback"

        normalized_bm25 = self._normalize_scores(bm25_scores)
        normalized_vectors = self._normalize_scores(vector_scores)
        ranked: list[EvidenceChunk] = []
        for index, chunk in enumerate(candidates):
            if self.last_retrieval_mode == "hybrid":
                chunk.score = self.bm25_weight * normalized_bm25[index] + self.vector_weight * normalized_vectors[index]
            else:
                chunk.score = normalized_bm25[index]
            if chunk.score > 0:
                ranked.append(chunk)
        ranked.sort(key=lambda item: item.score, reverse=True)
        selected = self._select_diverse(ranked)
        selected_text = " ".join(self._searchable_text(item).lower() for item in selected)
        unique_query_tokens = set(query_tokens)
        matched_query_tokens = {token for token in unique_query_tokens if token in selected_text}
        self.last_diagnostics = {
            "retrievalMode": self.last_retrieval_mode,
            "candidateCount": len(candidates),
            "evidenceCount": len(selected),
            "distinctPaperCount": len({item.record_id for item in selected}),
            "queryCoverage": round(len(matched_query_tokens) / max(1, len(unique_query_tokens)), 4),
            "topScore": round(selected[0].score, 4) if selected else 0.0,
        }
        return [item.to_dict() for item in selected]

    def build_context(self, evidence: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        for index, item in enumerate(evidence, start=1):
            metadata = [str(item.get("title") or "未命名文献")]
            if item.get("year"):
                metadata.append(str(item["year"]))
            if item.get("section"):
                metadata.append(f"章节：{item['section']}")
            blocks.append(f"[{index}] {' · '.join(metadata)}\n{str(item.get('text') or '').strip()}")
        return "\n\n---\n\n".join(blocks)

    def _paper_chunks(self, paper: dict[str, Any]) -> list[EvidenceChunk]:
        base = {
            "record_id": str(paper.get("id") or paper.get("recordId") or ""),
            "title": str(paper.get("title") or "未命名文献"),
            "year": str(paper.get("year") or ""),
            "source": str(paper.get("source") or ""),
            "url": str(paper.get("url") or ""),
        }
        split_chunks = paper.get("splitChunks") or paper.get("split_chunks")
        if isinstance(split_chunks, list):
            chunks: list[EvidenceChunk] = []
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
                if self._is_excluded(category=category, section=section, text=f"{summary}\n{content}"):
                    continue
                text = f"分块摘要：{summary}\n\n{content}" if summary else content
                chunks.append(EvidenceChunk(**base, text=text, score=0, section=section, chunk_index=index))
            if chunks:
                return chunks

        content = self._read_markdown(paper) or str(paper.get("abstract") or "").strip()
        return [
            EvidenceChunk(**base, text=text, score=0, chunk_index=index)
            for index, text in enumerate(self._split_text(content))
            if text and not self._is_excluded(category="", section="", text=text)
        ]

    def _read_markdown(self, paper: dict[str, Any]) -> str:
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

    def _split_text(self, text: str) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            if current and len(current) + len(paragraph) + 2 > self.chunk_size:
                chunks.append(current)
                current = ""
            current = f"{current}\n\n{paragraph}".strip()
        if current:
            chunks.append(current)
        return chunks

    def _is_excluded(self, *, category: str, section: str, text: str) -> bool:
        normalized_category = category.replace("-", "_").replace(" ", "_")
        if normalized_category in _EXCLUDED_CATEGORIES:
            return True
        normalized_section = section.lower().strip()
        if normalized_section and any(pattern in normalized_section for pattern in _EXCLUDED_SECTION_PATTERNS):
            return True
        first_line = next((line.strip(" #\t:：.-").lower() for line in text.splitlines() if line.strip()), "")
        return any(first_line.startswith(pattern) for pattern in _EXCLUDED_SECTION_PATTERNS)

    def _searchable_text(self, chunk: EvidenceChunk) -> str:
        return f"{chunk.title} {chunk.section} {chunk.text}"

    def _tokenize(self, text: str) -> list[str]:
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

    def _select_diverse(self, ranked: list[EvidenceChunk]) -> list[EvidenceChunk]:
        selected: list[EvidenceChunk] = []
        per_paper: defaultdict[str, int] = defaultdict(int)
        context_size = 0
        for candidate in ranked:
            if per_paper[candidate.record_id] >= self.max_chunks_per_paper:
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
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    def _normalize_scores(self, scores: list[float]) -> list[float]:
        if not scores:
            return []
        minimum = min(scores)
        maximum = max(scores)
        if maximum <= minimum:
            return [1.0 if value > 0 else 0.0 for value in scores]
        return [(value - minimum) / (maximum - minimum) for value in scores]


__all__ = ["RAGRetriever", "EvidenceChunk"]
