"""在正文片段检索前，按结构化查询方向执行轻量级文献级初筛。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class DocumentCandidateResult:
    papers: list[dict[str, Any]]
    diagnostics: dict[str, Any]


class DocumentCandidateRetriever:
    """使用通用元数据相关性和分面配额筛选论文，不解释具体领域词。"""

    def shortlist(
        self,
        papers: list[dict[str, Any]],
        *,
        query: str,
        retrieval_facets: list[dict[str, Any]] | None,
        requirement_specs: list[dict[str, Any]] | None,
        limit: int,
    ) -> DocumentCandidateResult:
        if len(papers) <= max(1, int(limit)):
            return DocumentCandidateResult(
                list(papers),
                {
                    "attempted": False,
                    "candidateDocumentCount": len(papers),
                    "selectedDocumentCount": len(papers),
                    "reason": "within_limit",
                },
            )

        query_specs = self._query_specs(
            query,
            retrieval_facets or [],
            requirement_specs or [],
        )
        if not query_specs:
            return self._fallback(papers, reason="no_query_specs")

        document_tokens = [self._document_tokens(paper) for paper in papers]
        document_frequency: dict[str, int] = {}
        for tokens in document_tokens:
            for token in set(tokens):
                document_frequency[token] = document_frequency.get(token, 0) + 1

        per_query_scores: list[list[float]] = []
        aggregate_scores = [0.0 for _ in papers]
        for spec in query_specs:
            query_tokens = self._tokens(spec)
            scores = [
                self._score(
                    paper,
                    tokens,
                    query_tokens,
                    document_frequency,
                    document_count=len(papers),
                )
                for paper, tokens in zip(papers, document_tokens)
            ]
            per_query_scores.append(scores)
            for index, score in enumerate(scores):
                aggregate_scores[index] = max(aggregate_scores[index], score)

        if max(aggregate_scores, default=0.0) <= 0:
            return self._fallback(papers, reason="no_metadata_overlap")

        selected_indices: list[int] = []
        selected_set: set[int] = set()
        per_query_quota = max(1, min(3, max(1, int(limit)) // len(query_specs)))
        for scores in per_query_scores:
            ranked = sorted(range(len(papers)), key=lambda index: scores[index], reverse=True)
            added_for_query = 0
            for index in ranked:
                if scores[index] <= 0:
                    break
                if index not in selected_set:
                    selected_set.add(index)
                    selected_indices.append(index)
                    added_for_query += 1
                if added_for_query >= per_query_quota:
                    break

        ranked_all = sorted(
            range(len(papers)),
            key=lambda index: aggregate_scores[index],
            reverse=True,
        )
        for index in ranked_all:
            if len(selected_indices) >= max(1, int(limit)):
                break
            if aggregate_scores[index] <= 0 or index in selected_set:
                continue
            selected_set.add(index)
            selected_indices.append(index)

        selected_indices = selected_indices[: max(1, int(limit))]
        selected = [papers[index] for index in selected_indices]
        return DocumentCandidateResult(
            selected,
            {
                "attempted": True,
                "strategy": "metadata_facet_quota",
                "candidateDocumentCount": len(papers),
                "selectedDocumentCount": len(selected),
                "querySpecCount": len(query_specs),
                "selectedPaperIds": [
                    str(paper.get("id") or "") for paper in selected
                ],
            },
        )

    def _fallback(
        self,
        papers: list[dict[str, Any]],
        *,
        reason: str,
    ) -> DocumentCandidateResult:
        # 无法可靠排序时保持全部授权论文，宁可增加检索成本也不静默损失召回率。
        return DocumentCandidateResult(
            list(papers),
            {
                "attempted": True,
                "candidateDocumentCount": len(papers),
                "selectedDocumentCount": len(papers),
                "reason": reason,
                "fallbackToAuthorizedCorpus": True,
            },
        )

    @classmethod
    def _query_specs(
        cls,
        query: str,
        facets: list[dict[str, Any]],
        requirements: list[dict[str, Any]],
    ) -> list[str]:
        values = [str(query or "").strip()]
        for item in facets:
            if not isinstance(item, dict):
                continue
            values.append(
                " ".join(
                    [
                        str(item.get("query") or ""),
                        str(item.get("goal") or ""),
                        *[str(value) for value in item.get("concepts") or []],
                        *[str(value) for value in item.get("phrases") or []],
                    ]
                ).strip()
            )
        for item in requirements:
            if isinstance(item, dict):
                values.append(str(item.get("description") or "").strip())
        return list(dict.fromkeys(value for value in values if cls._tokens(value)))

    @classmethod
    def _document_tokens(cls, paper: dict[str, Any]) -> list[str]:
        abstract = (
            paper.get("abstract")
            or paper.get("abstractText")
            or paper.get("summary")
            or ""
        )
        return cls._tokens(
            " ".join(
                [
                    str(paper.get("title") or ""),
                    str(paper.get("keyword") or ""),
                    str(abstract),
                ]
            )
        )

    @classmethod
    def _score(
        cls,
        paper: dict[str, Any],
        document_tokens: list[str],
        query_tokens: list[str],
        document_frequency: dict[str, int],
        *,
        document_count: int,
    ) -> float:
        if not document_tokens or not query_tokens:
            return 0.0
        counts: dict[str, int] = {}
        for token in document_tokens:
            counts[token] = counts.get(token, 0) + 1
        title_tokens = set(cls._tokens(str(paper.get("title") or "")))
        score = 0.0
        for token in set(query_tokens):
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            inverse_frequency = math.log(
                1 + (document_count + 1) / (document_frequency.get(token, 0) + 1)
            )
            title_boost = 2.5 if token in title_tokens else 1.0
            score += title_boost * inverse_frequency * (1 + math.log(frequency))
        return score

    @staticmethod
    def _tokens(value: str) -> list[str]:
        normalized = str(value or "").casefold()
        latin = re.findall(r"[a-z0-9][a-z0-9_\-]{1,}", normalized)
        cjk_sequences = re.findall(r"[\u3400-\u9fff]+", normalized)
        cjk = [
            sequence[index : index + 2]
            for sequence in cjk_sequences
            for index in range(max(0, len(sequence) - 1))
        ]
        return [*latin, *cjk]


__all__ = ["DocumentCandidateResult", "DocumentCandidateRetriever"]
