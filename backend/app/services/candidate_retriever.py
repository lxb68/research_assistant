"""融合稀疏和向量得分召回宽候选池。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math
from typing import Any, Callable

import requests

from app.services.embedding_store import SQLiteVectorStore, cosine_similarity, tfidf_cosine_scores


@dataclass(slots=True)
class CandidateBatch:
    """宽候选池及其排序诊断，不包含证据选择决策。"""

    query: str
    candidates: list[Any] = field(default_factory=list)
    ranked: list[Any] = field(default_factory=list)
    query_tokens: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


class CandidateRetriever:
    """只负责建立候选池并排序，不执行证据预算与多样性选择。"""

    def __init__(
        self,
        *,
        index_paper: Callable[[dict[str, Any]], list[Any]],
        tokenize: Callable[[str], list[str]],
        searchable_text: Callable[[Any], str],
        embedding_clients: list[Any],
        vector_store: SQLiteVectorStore | None,
        bm25_weight: float,
        vector_weight: float,
    ) -> None:
        self._index_paper = index_paper
        self._tokenize = tokenize
        self._searchable_text = searchable_text
        self._embedding_clients = embedding_clients
        self._vector_store = vector_store
        self._bm25_weight = bm25_weight
        self._vector_weight = vector_weight

    def retrieve(
        self,
        query: str,
        papers: list[dict[str, Any]],
        *,
        section_score_adjuster: Callable[[str], float] | None = None,
        chunk_score_adjuster: Callable[[Any], float] | None = None,
    ) -> CandidateBatch:
        normalized_query = str(query).strip()
        if not normalized_query:
            return CandidateBatch("")
        candidates = [chunk for paper in papers for chunk in self._index_paper(paper)]
        if not candidates:
            return CandidateBatch(normalized_query)
        query_tokens = self._tokenize(normalized_query)
        if not query_tokens:
            return CandidateBatch(normalized_query, candidates=candidates)

        tokenized = [self._tokenize(self._searchable_text(chunk)) for chunk in candidates]
        frequencies: Counter[str] = Counter()
        for tokens in tokenized:
            frequencies.update(set(tokens))
        average_length = sum(len(tokens) for tokens in tokenized) / max(1, len(tokenized))
        bm25_scores: list[float] = []
        for chunk, tokens in zip(candidates, tokenized, strict=True):
            score = self._bm25(query_tokens, tokens, frequencies, len(candidates), average_length)
            title, section = str(chunk.title).lower(), str(chunk.section).lower()
            for token in set(query_tokens):
                if token in title:
                    score += 1.2
                if section and token in section:
                    score += 0.65
            bm25_scores.append(score)

        searchable = [self._searchable_text(chunk) for chunk in candidates]
        vector_scores = [0.0] * len(candidates)
        failures: list[str] = []
        backend = ""
        mode = "hybrid_tfidf"
        if self._vector_store:
            for client in self._embedding_clients:
                if not client.configured:
                    continue
                try:
                    vectors = self._vector_store.get_or_create(searchable, client=client)
                    query_vector = client.embed([normalized_query])[0]
                    vector_scores = [cosine_similarity(query_vector, vector) for vector in vectors]
                    backend = client.provider
                    mode = f"hybrid_{client.provider}"
                    break
                except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as error:
                    failures.append(f"{client.provider}:{type(error).__name__}")
        if not backend:
            vector_scores = tfidf_cosine_scores(normalized_query, searchable)
            backend = "tfidf"

        normalized_bm25 = self._normalize_scores(bm25_scores)
        normalized_vectors = self._normalize_scores(vector_scores)
        ranked: list[Any] = []
        for index, chunk in enumerate(candidates):
            chunk.score = self._bm25_weight * normalized_bm25[index] + self._vector_weight * normalized_vectors[index]
            if section_score_adjuster:
                chunk.score += float(section_score_adjuster(chunk.section))
            if chunk_score_adjuster:
                chunk.score += float(chunk_score_adjuster(chunk))
            if chunk.score > 0:
                ranked.append(chunk)
        ranked.sort(key=lambda item: item.score, reverse=True)
        token_counts = [item.token_count for item in candidates if item.token_count > 0]
        return CandidateBatch(
            query=normalized_query,
            candidates=candidates,
            ranked=ranked,
            query_tokens=query_tokens,
            diagnostics={
                "retrievalMode": mode,
                "embeddingBackend": backend,
                "embeddingFailures": failures,
                "chunkingStrategy": "mineru_structure_semantic_token_overlap",
                "candidateCount": len(candidates),
                "averageChunkTokens": round(sum(token_counts) / max(1, len(token_counts)), 2),
                "maxChunkTokens": max(token_counts, default=0),
                "overlappedChunkCount": sum(item.overlap_token_count > 0 for item in candidates),
                "structuredCandidateCount": sum(bool(item.structure_id) for item in candidates),
                "rankedCandidateCount": len(ranked),
            },
        )

    @staticmethod
    def _bm25(query: list[str], document: list[str], frequencies: Counter[str], count: int, average: float) -> float:
        if not document:
            return 0.0
        term_counts = Counter(document)
        score = 0.0
        for token in query:
            frequency = term_counts[token]
            if not frequency:
                continue
            inverse = math.log(1 + (count - frequencies[token] + 0.5) / (frequencies[token] + 0.5))
            score += inverse * frequency * 2.5 / (frequency + 1.5 * (1 - 0.75 + 0.75 * len(document) / max(1, average)))
        return score

    @staticmethod
    def _normalize_scores(scores: list[float]) -> list[float]:
        if not scores:
            return []
        minimum, maximum = min(scores), max(scores)
        if maximum == minimum:
            return [1.0 if maximum > 0 else 0.0 for _ in scores]
        return [(value - minimum) / (maximum - minimum) for value in scores]


__all__ = ["CandidateBatch", "CandidateRetriever"]
