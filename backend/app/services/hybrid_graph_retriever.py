"""以知识图谱导航候选关系，并通过原文逐字证据回查 RAG 分块。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.services.domain_tree_store import DomainTreeStore
from app.services.evidence_groups import limit_evidence_groups
from app.services.rag_retriever import RAGRetriever


class HybridGraphRetriever:
    """图谱只负责关系导航；最终返回值必须能够回定位到论文原文。"""

    RELATIONAL_QUESTION_TYPES = {"mechanism", "comparison", "evaluation", "synthesis"}

    def __init__(
        self,
        *,
        graph_root: str | Path,
        project_id: str = "workspace-domain-tree",
        store: DomainTreeStore | None = None,
        enabled: bool = True,
        max_relations: int = 8,
        max_evidence: int = 3,
    ) -> None:
        self.graph_root = Path(graph_root).resolve()
        self.project_id = str(project_id or "workspace-domain-tree").strip()
        self.store = store or DomainTreeStore()
        self.enabled = bool(enabled)
        self.max_relations = max(1, min(int(max_relations), 50))
        self.max_evidence = max(1, min(int(max_evidence), 20))

    def retrieve(
        self,
        question: str,
        *,
        papers: list[dict[str, Any]],
        retriever: RAGRetriever,
        question_type: str,
        retrieval_facets: list[dict[str, Any]] | None = None,
        project_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回已通过原文回查的图谱导航证据和可解释诊断。"""
        normalized_type = str(question_type or "simple_fact").strip().lower()
        diagnostics: dict[str, Any] = {
            "enabled": self.enabled,
            "attempted": False,
            "questionType": normalized_type,
            "projectId": str(project_id or self.project_id),
            "graphAvailable": False,
            "matchedEntityCount": 0,
            "matchedRelationCount": 0,
            "graphEvidenceLocatorCount": 0,
            "verifiedEvidenceCount": 0,
            "unresolvedEvidenceCount": 0,
        }
        if not self.enabled or normalized_type not in self.RELATIONAL_QUESTION_TYPES:
            diagnostics["skipReason"] = "question_type_not_relational" if self.enabled else "disabled"
            return [], diagnostics

        diagnostics["attempted"] = True
        resolved_project_id = str(project_id or self.project_id).strip() or self.project_id
        result = self.store.load_result(self.graph_root / resolved_project_id, resolved_project_id)
        if not isinstance(result, dict):
            diagnostics["skipReason"] = "graph_not_found"
            return [], diagnostics
        if str(result.get("graphStatus") or "") != "ready":
            diagnostics["skipReason"] = "graph_not_ready"
            return [], diagnostics

        graph = result.get("knowledgeGraph")
        if not isinstance(graph, dict) or not graph:
            diagnostics["skipReason"] = "graph_empty"
            return [], diagnostics
        diagnostics["graphAvailable"] = True

        query_texts = self._query_texts(question, retrieval_facets or [])
        entities = [item for item in graph.get("entities") or [] if isinstance(item, dict)]
        entity_by_id = {
            str(item.get("id") or ""): item
            for item in entities
            if str(item.get("id") or "")
        }
        entity_scores = {
            entity_id: score
            for entity_id, entity in entity_by_id.items()
            if (score := self._score_item(query_texts, self._entity_search_text(entity))) > 0
        }
        diagnostics["matchedEntityCount"] = len(entity_scores)

        evidence_by_id = {
            str(item.get("id") or ""): item
            for item in graph.get("evidence") or []
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        ranked_relations: list[tuple[float, dict[str, Any], str]] = []
        for relation in graph.get("semanticRelations") or []:
            if not isinstance(relation, dict):
                continue
            source_id = str(relation.get("source") or "")
            target_id = str(relation.get("target") or "")
            source = entity_by_id.get(source_id, {})
            target = entity_by_id.get(target_id, {})
            claim = self._relation_claim(source, relation, target)
            direct_score = self._score_item(query_texts, claim)
            endpoint_score = entity_scores.get(source_id, 0.0) + entity_scores.get(target_id, 0.0)
            evidence_ids = [
                str(value) for value in relation.get("evidenceIds") or [] if str(value) in evidence_by_id
            ]
            if not evidence_ids or (direct_score <= 0 and endpoint_score <= 0):
                continue
            confidence = self._safe_float(relation.get("confidence"), default=0.5)
            score = direct_score + endpoint_score * 0.75 + confidence * 0.1
            ranked_relations.append((score, relation, claim))

        ranked_relations.sort(key=lambda item: item[0], reverse=True)
        selected_relations = ranked_relations[: self.max_relations]
        diagnostics["matchedRelationCount"] = len(selected_relations)

        locators: dict[str, dict[str, Any]] = {}
        for score, relation, claim in selected_relations:
            relation_id = str(relation.get("id") or "")
            for evidence_id in relation.get("evidenceIds") or []:
                evidence_id = str(evidence_id)
                graph_evidence = evidence_by_id.get(evidence_id)
                if not graph_evidence:
                    continue
                locator = locators.setdefault(
                    evidence_id,
                    {
                        "graphEvidenceId": evidence_id,
                        "recordId": str(graph_evidence.get("documentId") or ""),
                        "section": str(graph_evidence.get("section") or ""),
                        "lineStart": int(graph_evidence.get("lineStart") or 0),
                        "quote": str(graph_evidence.get("quote") or "").strip(),
                        "relationIds": [],
                        "navigationClaims": [],
                        "relevanceScore": 0.0,
                    },
                )
                if relation_id and relation_id not in locator["relationIds"]:
                    locator["relationIds"].append(relation_id)
                if claim and claim not in locator["navigationClaims"]:
                    locator["navigationClaims"].append(claim)
                locator["relevanceScore"] = max(float(locator["relevanceScore"]), score)

        ordered_locators = sorted(
            (
                item for item in locators.values()
                if item["recordId"] and item["quote"]
            ),
            key=lambda item: float(item["relevanceScore"]),
            reverse=True,
        )
        diagnostics["graphEvidenceLocatorCount"] = len(ordered_locators)
        resolved = retriever.resolve_quote_references(
            papers,
            ordered_locators,
            limit=max(self.max_evidence, len(ordered_locators)),
        )
        verified = resolved[: self.max_evidence]
        diagnostics["verifiedEvidenceCount"] = len(verified)
        resolved_graph_ids = {
            str(graph_id)
            for item in resolved
            for graph_id in item.get("graph_evidence_ids") or []
        }
        diagnostics["unresolvedEvidenceCount"] = sum(
            str(item.get("graphEvidenceId") or "") not in resolved_graph_ids
            for item in ordered_locators
        )
        diagnostics["relationIds"] = [
            str(relation.get("id") or "") for _, relation, _ in selected_relations
        ]
        return verified, diagnostics

    @staticmethod
    def merge_evidence(
        text_evidence: list[dict[str, Any]],
        graph_evidence: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """按稳定块标识融合双通道结果，并保留各自的可解释元数据。"""
        safe_limit = max(1, int(limit))
        merged: dict[tuple[str, int], dict[str, Any]] = {}

        def add_items(items: list[dict[str, Any]], channel: str) -> None:
            for rank, raw_item in enumerate(items, start=1):
                item = dict(raw_item)
                key = (
                    str(item.get("record_id") or ""),
                    int(item.get("chunk_index") or 0),
                )
                if not key[0]:
                    continue
                contribution = 1.0 / (60 + rank)
                if key not in merged:
                    item["retrieval_channels"] = list(dict.fromkeys([
                        *list(item.get("retrieval_channels") or []),
                        channel,
                    ]))
                    item["hybrid_fusion_score"] = contribution
                    merged[key] = item
                    continue

                current = merged[key]
                current["hybrid_fusion_score"] = (
                    float(current.get("hybrid_fusion_score") or 0) + contribution
                )
                current["score"] = max(
                    float(current.get("score") or 0),
                    float(item.get("score") or 0),
                )
                current["graph_backed"] = bool(
                    current.get("graph_backed") or item.get("graph_backed")
                )
                current["retrieval_channels"] = list(dict.fromkeys([
                    *list(current.get("retrieval_channels") or []),
                    *list(item.get("retrieval_channels") or []),
                    channel,
                ]))
                for field in (
                    "graph_evidence_ids",
                    "graph_relation_ids",
                    "graph_navigation_claims",
                    "graph_quotes",
                ):
                    current[field] = list(dict.fromkeys([
                        *list(current.get(field) or []),
                        *list(item.get(field) or []),
                    ]))

        add_items(text_evidence, "text_rag")
        add_items(graph_evidence, "graph_navigation")
        ranked = sorted(
            merged.values(),
            key=lambda item: (
                float(item.get("hybrid_fusion_score") or 0),
                float(item.get("score") or 0),
            ),
            reverse=True,
        )
        # limit 表示逻辑证据数；相同 structure_id 的连续分块必须整体通过融合层。
        return limit_evidence_groups(ranked, max_groups=safe_limit)

    @staticmethod
    def _query_texts(question: str, facets: list[dict[str, Any]]) -> list[str]:
        values = [str(question or "").strip()]
        for facet in facets:
            if not isinstance(facet, dict):
                continue
            values.extend(
                str(value).strip()
                for value in [facet.get("goal"), facet.get("query")]
                if str(value or "").strip()
            )
            values.extend(str(value).strip() for value in facet.get("concepts") or [] if str(value).strip())
            values.extend(str(value).strip() for value in facet.get("phrases") or [] if str(value).strip())
        return list(dict.fromkeys(value for value in values if value))[:20]

    @classmethod
    def _score_item(cls, queries: list[str], searchable: str) -> float:
        normalized_text = cls._normalize_text(searchable)
        if not normalized_text:
            return 0.0
        text_terms = cls._tokenize(normalized_text)
        score = 0.0
        for query in queries:
            normalized_query = cls._normalize_text(query)
            if not normalized_query:
                continue
            if len(normalized_query) >= 3 and normalized_query in normalized_text:
                score += 3.0
            query_terms = cls._tokenize(normalized_query)
            matched = query_terms & text_terms
            if matched:
                score += len(matched) / max(1, len(query_terms))
                score += sum(0.35 for term in matched if re.fullmatch(r"[a-z0-9][a-z0-9+._/-]+", term))
        return score

    @classmethod
    def _tokenize(cls, value: str) -> set[str]:
        tokens = set(re.findall(r"[a-z0-9][a-z0-9+._/-]{1,}", value.casefold()))
        for sequence in re.findall(r"[\u4e00-\u9fff]+", value):
            if len(sequence) <= 4:
                tokens.add(sequence)
            if len(sequence) > 1:
                tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
        return tokens

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    @staticmethod
    def _entity_search_text(entity: dict[str, Any]) -> str:
        attributes = " ".join(
            f"{item.get('name', '')} {item.get('value', '')}"
            for item in entity.get("attributes") or []
            if isinstance(item, dict)
        )
        return " ".join(
            [
                str(entity.get("name") or ""),
                str(entity.get("type") or ""),
                " ".join(str(value) for value in entity.get("aliases") or []),
                attributes,
            ]
        )

    @staticmethod
    def _relation_claim(
        source: dict[str, Any],
        relation: dict[str, Any],
        target: dict[str, Any],
    ) -> str:
        return " ".join(
            value
            for value in [
                str(source.get("name") or relation.get("source") or "").strip(),
                str(relation.get("predicate") or relation.get("relation") or "").strip(),
                str(target.get("name") or relation.get("target") or "").strip(),
            ]
            if value
        )

    @staticmethod
    def _safe_float(value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


__all__ = ["HybridGraphRetriever"]
