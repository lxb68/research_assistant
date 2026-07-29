"""文献地图各阶段共享的类型化数据契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _string_list(values: Any, *, limit: int = 64, item_limit: int = 1000) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            value
            for raw in values[:limit]
            if (value := str(raw or "").strip()[:item_limit])
        )
    )


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """指向可回查原文块的证据引用。"""

    record_id: str
    chunk_index: int
    quote: str
    section: str = ""
    origin_type: str = "paper_text"
    extraction_confidence: float = 1.0

    @property
    def ref(self) -> str:
        return f"{self.record_id}:{self.chunk_index}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceReference":
        return cls(
            record_id=str(value.get("record_id") or value.get("recordId") or "").strip(),
            chunk_index=int(value.get("chunk_index") or value.get("chunkIndex") or 0),
            quote=str(value.get("quote") or "").strip(),
            section=str(value.get("section") or "").strip(),
            origin_type=str(
                value.get("origin_type") or value.get("originType") or "paper_text"
            ).strip(),
            extraction_confidence=max(
                0.0,
                min(
                    float(
                        value.get("extraction_confidence")
                        or value.get("extractionConfidence")
                        or 1
                    ),
                    1.0,
                ),
            ),
        )


@dataclass(slots=True)
class MapClaim:
    """论文卡片中的原子声明；谓词不使用固定领域词表。"""

    id: str
    paper_id: str
    kind: str
    subject: str
    predicate: str
    object: str
    qualifiers: dict[str, Any] = field(default_factory=dict)
    attribution_type: str = "document_statement"
    confidence: float = 0.0
    support_status: str = "supported"
    evidence_refs: list[EvidenceReference] = field(default_factory=list)
    raw_kind: str = ""
    canonical_kind: str = ""
    normalizer_version: str = ""
    normalization_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MapClaim":
        return cls(
            id=str(value.get("id") or ""),
            paper_id=str(value.get("paper_id") or value.get("paperId") or ""),
            kind=str(value.get("kind") or "statement"),
            subject=str(value.get("subject") or ""),
            predicate=str(value.get("predicate") or ""),
            object=str(value.get("object") or ""),
            qualifiers=dict(value.get("qualifiers") or {}),
            attribution_type=str(
                value.get("attribution_type")
                or value.get("attributionType")
                or "document_statement"
            ),
            confidence=float(value.get("confidence") or 0),
            support_status=str(
                value.get("support_status") or value.get("supportStatus") or "supported"
            ),
            evidence_refs=[
                EvidenceReference.from_dict(item)
                for item in value.get("evidence_refs") or value.get("evidenceRefs") or []
                if isinstance(item, dict)
            ],
            raw_kind=str(value.get("raw_kind") or value.get("rawKind") or ""),
            canonical_kind=str(
                value.get("canonical_kind") or value.get("canonicalKind") or ""
            ),
            normalizer_version=str(
                value.get("normalizer_version") or value.get("normalizerVersion") or ""
            ),
            normalization_confidence=float(
                value.get("normalization_confidence")
                or value.get("normalizationConfidence")
                or 0
            ),
        )


@dataclass(slots=True)
class RelationCandidate:
    """单篇论文中抽取出的关系候选，允许目标暂时只以标签存在。"""

    relation_type: str
    target_label: str
    target_paper_id: str = ""
    target_type: str = "unresolved_label"
    qualifiers: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    evidence_refs: list[EvidenceReference] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationCandidate":
        return cls(
            relation_type=str(
                value.get("relation_type") or value.get("relationType") or ""
            ),
            target_label=str(value.get("target_label") or value.get("targetLabel") or ""),
            target_paper_id=str(
                value.get("target_paper_id") or value.get("targetPaperId") or ""
            ),
            target_type=str(
                value.get("target_type")
                or value.get("targetType")
                or "unresolved_label"
            ),
            qualifiers=dict(value.get("qualifiers") or {}),
            confidence=float(value.get("confidence") or 0),
            evidence_refs=[
                EvidenceReference.from_dict(item)
                for item in value.get("evidence_refs") or value.get("evidenceRefs") or []
                if isinstance(item, dict)
            ],
        )


@dataclass(slots=True)
class PaperCardDraft:
    """抽取器返回、尚未写入数据库的单篇论文卡片。"""

    summary: str = ""
    source_language: str = ""
    facets: dict[str, list[str]] = field(default_factory=dict)
    claims: list[MapClaim] = field(default_factory=list)
    relation_candidates: list[RelationCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "source_language": self.source_language,
            "facets": self.facets,
            "claims": [item.to_dict() for item in self.claims],
            "relation_candidates": [
                item.to_dict() for item in self.relation_candidates
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PaperCardDraft":
        return cls(
            summary=str(value.get("summary") or ""),
            source_language=str(
                value.get("source_language") or value.get("sourceLanguage") or ""
            ),
            facets={
                str(key): _string_list(values)
                for key, values in dict(value.get("facets") or {}).items()
            },
            claims=[
                MapClaim.from_dict(item)
                for item in value.get("claims") or []
                if isinstance(item, dict)
            ],
            relation_candidates=[
                RelationCandidate.from_dict(item)
                for item in (
                    value.get("relation_candidates")
                    or value.get("relationCandidates")
                    or []
                )
                if isinstance(item, dict)
            ],
        )


@dataclass(slots=True)
class PaperExtractionResult:
    """可暂存和重放的模型抽取结果。"""

    draft: PaperCardDraft
    diagnostics: dict[str, Any] = field(default_factory=dict)
    raw_response: str = ""

    def __iter__(self):
        """兼容旧的 ``draft, diagnostics = extract(...)`` 调用。"""
        yield self.draft
        yield self.diagnostics


@dataclass(slots=True)
class PaperCard:
    """可持久化的版本化论文卡片。"""

    paper_id: str
    title: str
    year: str
    document_version: str
    extractor_version: str
    schema_version: int
    summary: str = ""
    source_language: str = ""
    facets: dict[str, list[str]] = field(default_factory=dict)
    claims: list[MapClaim] = field(default_factory=list)
    status: str = "ready"
    canonical_facets: dict[str, list[str]] = field(default_factory=dict)
    metadata_warnings: list[str] = field(default_factory=list)
    metadata_provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "facets": {
                str(key): _string_list(values)
                for key, values in self.facets.items()
                if str(key).strip()
            },
            "claims": [item.to_dict() for item in self.claims],
        }


@dataclass(slots=True)
class LiteratureRelation:
    """跨论文或论文到概念的证据化关系。"""

    id: str
    project_id: str
    source_paper_id: str
    relation_type: str
    target_id: str
    target_type: str
    qualifiers: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[EvidenceReference] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "candidate"
    extractor_version: str = ""
    raw_relation_type: str = ""
    canonical_relation_type: str = ""
    normalizer_version: str = ""
    normalization_confidence: float = 0.0
    resolution_method: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }


__all__ = [
    "EvidenceReference",
    "LiteratureRelation",
    "MapClaim",
    "PaperCard",
    "PaperCardDraft",
    "PaperExtractionResult",
    "RelationCandidate",
]
