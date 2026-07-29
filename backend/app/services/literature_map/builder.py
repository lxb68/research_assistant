"""编排单篇论文卡片的增量构建，不参与问答生成。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.literature_map.models import (
    LiteratureRelation,
    PaperCard,
    PaperCardDraft,
)
from app.services.literature_map.repository import LiteratureMapRepository
from app.services.literature_map.versioning import (
    compute_document_version,
    stable_map_id,
)


class PaperCardExtractor(Protocol):
    extractor_version: str

    def extract(
        self,
        paper: dict[str, Any],
        evidence_chunks: list[dict[str, Any]],
    ) -> tuple[PaperCardDraft, dict[str, Any]]: ...


@dataclass(slots=True)
class LiteratureMapBuildResult:
    paper_id: str
    status: str
    document_version: str
    claim_count: int = 0
    relation_count: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LiteratureMapBuilder:
    """连接版本计算、抽取器和仓储；依赖均由外部注入。"""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        repository: LiteratureMapRepository,
        extractor: PaperCardExtractor,
        project_id: str = "",
        schema_version: int | None = None,
    ) -> None:
        self.repository = repository
        self.extractor = extractor
        self.project_id = str(project_id or "").strip()
        self.schema_version = int(schema_version or self.SCHEMA_VERSION)

    def build_paper(
        self,
        paper: dict[str, Any],
        evidence_chunks: list[dict[str, Any]],
        *,
        document_text: str = "",
        force: bool = False,
    ) -> LiteratureMapBuildResult:
        paper_id = str(paper.get("id") or paper.get("recordId") or "").strip()
        if not paper_id:
            raise ValueError("论文缺少稳定 paper_id")
        document_version = compute_document_version(
            paper,
            document_text=document_text,
            evidence_chunks=evidence_chunks,
        )
        if not force and not self.repository.needs_rebuild(
            paper_id,
            document_version=document_version,
            extractor_version=self.extractor.extractor_version,
            schema_version=self.schema_version,
        ):
            card = self.repository.get_card(paper_id)
            return LiteratureMapBuildResult(
                paper_id=paper_id,
                status="reused",
                document_version=document_version,
                claim_count=len(card.claims) if card else 0,
                relation_count=len(
                    self.repository.list_relations(
                        project_id=self.project_id,
                        source_paper_id=paper_id,
                    )
                ),
            )

        self.repository.mark_build_started(
            paper_id,
            document_version=document_version,
            extractor_version=self.extractor.extractor_version,
        )
        try:
            draft, diagnostics = self.extractor.extract(paper, evidence_chunks)
            card = PaperCard(
                paper_id=paper_id,
                title=str(paper.get("title") or "未命名文献").strip(),
                year=str(paper.get("year") or "").strip(),
                document_version=document_version,
                extractor_version=self.extractor.extractor_version,
                schema_version=self.schema_version,
                summary=draft.summary,
                source_language=draft.source_language,
                facets=draft.facets,
                claims=draft.claims,
            )
            relations = [
                LiteratureRelation(
                    id=stable_map_id(
                        "relation",
                        self.project_id,
                        paper_id,
                        candidate.relation_type,
                        candidate.target_paper_id or candidate.target_label,
                    ),
                    project_id=self.project_id,
                    source_paper_id=paper_id,
                    relation_type=candidate.relation_type,
                    target_id=candidate.target_paper_id or candidate.target_label,
                    target_type=(
                        "paper"
                        if candidate.target_paper_id
                        else candidate.target_type or "unresolved_label"
                    ),
                    qualifiers={
                        **candidate.qualifiers,
                        **(
                            {"targetLabel": candidate.target_label}
                            if candidate.target_paper_id
                            else {}
                        ),
                    },
                    evidence_refs=candidate.evidence_refs,
                    confidence=candidate.confidence,
                    status=(
                        "resolved"
                        if candidate.target_paper_id
                        else "candidate"
                    ),
                    extractor_version=self.extractor.extractor_version,
                )
                for candidate in draft.relation_candidates
            ]
            self.repository.save_card(
                card,
                relations=relations,
                relation_scope_project_id=self.project_id,
            )
            completed_diagnostics = {
                **diagnostics,
                "claimCount": len(card.claims),
                "relationCount": len(relations),
            }
            self.repository.mark_build_finished(
                paper_id,
                status="ready",
                diagnostics=completed_diagnostics,
            )
            return LiteratureMapBuildResult(
                paper_id=paper_id,
                status="built",
                document_version=document_version,
                claim_count=len(card.claims),
                relation_count=len(relations),
                diagnostics=completed_diagnostics,
            )
        except Exception as error:
            self.repository.mark_build_finished(
                paper_id,
                status="failed",
                error_message=str(error),
            )
            raise


__all__ = [
    "LiteratureMapBuildResult",
    "LiteratureMapBuilder",
    "PaperCardExtractor",
]
