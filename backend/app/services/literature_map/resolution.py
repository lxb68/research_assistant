"""论文目标解析与关系候选合并。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.literature_map.models import (
    EvidenceReference,
    LiteratureRelation,
    RelationCandidate,
)
from app.services.literature_map.normalization import VocabularyNormalizer, lexical_key
from app.services.literature_map.versioning import stable_map_id


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    target_id: str
    target_type: str
    status: str
    method: str
    confidence: float


class PaperEntityResolver:
    """只把本地论文库中能够唯一确认的目标标记为 resolved。"""

    def __init__(self, papers: list[dict[str, Any]]) -> None:
        self.by_id = {
            str(paper.get("id") or paper.get("recordId") or "").strip(): paper
            for paper in papers
            if str(paper.get("id") or paper.get("recordId") or "").strip()
        }
        self.by_doi = self._index(papers, lambda paper: lexical_key(str(paper.get("doi") or "")))
        self.by_title = self._index(
            papers,
            lambda paper: lexical_key(str(paper.get("title") or "")),
        )

    @staticmethod
    def _index(
        papers: list[dict[str, Any]],
        key_fn,
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for paper in papers:
            paper_id = str(paper.get("id") or paper.get("recordId") or "").strip()
            key = key_fn(paper)
            if paper_id and key:
                result.setdefault(key, []).append(paper_id)
        return result

    def resolve(self, source_paper_id: str, candidate: RelationCandidate) -> ResolvedTarget:
        proposed_id = candidate.target_paper_id.strip()
        label = candidate.target_label.strip()
        matches: list[tuple[str, str]] = []
        if proposed_id in self.by_id:
            matches.append((proposed_id, "paper_id"))
        for value, method in ((proposed_id, "doi"), (label, "doi")):
            ids = self.by_doi.get(lexical_key(value), [])
            matches.extend((paper_id, method) for paper_id in ids)
        title_ids = self.by_title.get(lexical_key(label), [])
        matches.extend((paper_id, "title") for paper_id in title_ids)
        unique = list(dict.fromkeys(paper_id for paper_id, _method in matches))
        if len(unique) == 1:
            target_id = unique[0]
            if target_id == source_paper_id:
                return ResolvedTarget(
                    target_id=target_id,
                    target_type="paper",
                    status="rejected",
                    method="self_reference",
                    confidence=0.0,
                )
            method = next(method for paper_id, method in matches if paper_id == target_id)
            return ResolvedTarget(
                target_id=target_id,
                target_type="paper",
                status="resolved",
                method=method,
                confidence=1.0 if method in {"paper_id", "doi"} else 0.9,
            )
        if len(unique) > 1:
            return ResolvedTarget(
                target_id=label or proposed_id,
                target_type=candidate.target_type or "paper",
                status="ambiguous",
                method="multiple_matches",
                confidence=0.4,
            )
        return ResolvedTarget(
            target_id=label or proposed_id,
            target_type=candidate.target_type or "unresolved_label",
            status="candidate",
            method="unresolved",
            confidence=0.6,
        )


class RelationMerger:
    """把同一语义边的多条候选合并为一条多证据关系。"""

    def __init__(
        self,
        *,
        resolver: PaperEntityResolver,
        normalizer: VocabularyNormalizer,
    ) -> None:
        self.resolver = resolver
        self.normalizer = normalizer

    def merge(
        self,
        *,
        project_id: str,
        source_paper_id: str,
        extractor_version: str,
        candidates: list[RelationCandidate],
    ) -> list[LiteratureRelation]:
        merged: dict[tuple[str, str, str], LiteratureRelation] = {}
        for candidate in candidates:
            normalized = self.normalizer.normalize("relation", candidate.relation_type)
            target = self.resolver.resolve(source_paper_id, candidate)
            if target.status == "rejected" or not target.target_id:
                continue
            key = (
                normalized.canonical,
                lexical_key(target.target_id),
                target.target_type,
            )
            relation = merged.get(key)
            if relation is None:
                relation = LiteratureRelation(
                    id=stable_map_id(
                        "relation",
                        project_id,
                        source_paper_id,
                        normalized.canonical,
                        target.target_id,
                        target.target_type,
                    ),
                    project_id=project_id,
                    source_paper_id=source_paper_id,
                    relation_type=normalized.canonical,
                    target_id=target.target_id,
                    target_type=target.target_type,
                    qualifiers={
                        **candidate.qualifiers,
                        "targetLabel": candidate.target_label,
                        **(
                            {"proposedTargetPaperId": candidate.target_paper_id}
                            if candidate.target_paper_id
                            else {}
                        ),
                    },
                    evidence_refs=[],
                    confidence=min(candidate.confidence, target.confidence),
                    status=target.status,
                    extractor_version=extractor_version,
                    raw_relation_type=candidate.relation_type,
                    canonical_relation_type=normalized.canonical,
                    normalizer_version=normalized.version,
                    normalization_confidence=normalized.confidence,
                    resolution_method=target.method,
                )
                merged[key] = relation
            else:
                relation.confidence = max(
                    relation.confidence,
                    min(candidate.confidence, target.confidence),
                )
                relation.qualifiers = self._merge_qualifiers(
                    relation.qualifiers,
                    candidate.qualifiers,
                )
            relation.evidence_refs = self._merge_evidence(
                relation.evidence_refs,
                candidate.evidence_refs,
            )
        return list(merged.values())

    @staticmethod
    def _merge_evidence(
        existing: list[EvidenceReference],
        incoming: list[EvidenceReference],
    ) -> list[EvidenceReference]:
        values = {
            (item.record_id, item.chunk_index, item.quote): item
            for item in [*existing, *incoming]
        }
        return list(values.values())

    @staticmethod
    def _merge_qualifiers(
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(existing)
        for key, value in incoming.items():
            if key not in result:
                result[key] = value
            elif result[key] != value:
                values = result[key] if isinstance(result[key], list) else [result[key]]
                for item in value if isinstance(value, list) else [value]:
                    if item not in values:
                        values.append(item)
                result[key] = values
        return result


__all__ = ["PaperEntityResolver", "RelationMerger", "ResolvedTarget"]
