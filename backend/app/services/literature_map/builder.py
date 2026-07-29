"""编排单篇论文卡片的增量构建，不参与问答生成。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.literature_map.metadata_quality import PaperMetadataValidator
from app.services.literature_map.models import (
    PaperCard,
    PaperCardDraft,
    PaperExtractionResult,
)
from app.services.literature_map.normalization import VocabularyNormalizer
from app.services.literature_map.repository import LiteratureMapRepository
from app.services.literature_map.resolution import PaperEntityResolver, RelationMerger
from app.services.literature_map.versioning import compute_document_version


class PaperCardExtractor(Protocol):
    extractor_version: str

    def extract(
        self,
        paper: dict[str, Any],
        evidence_chunks: list[dict[str, Any]],
    ) -> PaperExtractionResult: ...


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
        normalizer: VocabularyNormalizer | None = None,
        relation_merger: RelationMerger | None = None,
        metadata_validator: PaperMetadataValidator | None = None,
    ) -> None:
        self.repository = repository
        self.extractor = extractor
        self.project_id = str(project_id or "").strip()
        self.schema_version = int(schema_version or self.SCHEMA_VERSION)
        self.normalizer = normalizer or VocabularyNormalizer()
        self.relation_merger = relation_merger or RelationMerger(
            resolver=PaperEntityResolver([]),
            normalizer=self.normalizer,
        )
        self.metadata_validator = metadata_validator or PaperMetadataValidator()

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
            extraction = None if force else self.repository.get_extraction(
                paper_id,
                document_version=document_version,
                extractor_version=self.extractor.extractor_version,
                schema_version=self.schema_version,
            )
            replayed = extraction is not None
            if extraction is None:
                extraction = self._coerce_extraction(
                    self.extractor.extract(paper, evidence_chunks)
                )
                # 模型输出先独立暂存；即使后续 SQL 提交失败，重试也不会再次调用模型。
                self.repository.save_extraction(
                    paper_id,
                    document_version=document_version,
                    extractor_version=self.extractor.extractor_version,
                    schema_version=self.schema_version,
                    result=extraction,
                )

            draft = extraction.draft
            claims = [self.normalizer.normalize_claim(claim) for claim in draft.claims]
            canonical_facets = self.normalizer.normalize_facets(draft.facets)
            metadata_warnings = self.metadata_validator.validate(paper)
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
                canonical_facets=canonical_facets,
                claims=claims,
                metadata_warnings=metadata_warnings,
                metadata_provenance={
                    "source": str(paper.get("source") or "library"),
                    "validatorVersion": self.metadata_validator.version,
                },
            )
            relations = self.relation_merger.merge(
                project_id=self.project_id,
                source_paper_id=paper_id,
                candidates=draft.relation_candidates,
                extractor_version=self.extractor.extractor_version,
            )
            self.repository.save_card(
                card,
                relations=relations,
                relation_scope_project_id=self.project_id,
            )
            self.repository.mark_extraction_committed(
                paper_id,
                document_version=document_version,
                extractor_version=self.extractor.extractor_version,
                schema_version=self.schema_version,
            )
            completed_diagnostics = {
                **extraction.diagnostics,
                "extractionReplayed": replayed,
                "claimCount": len(card.claims),
                "relationCandidateCount": len(draft.relation_candidates),
                "relationCount": len(relations),
                "metadataWarningCount": len(metadata_warnings),
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

    @staticmethod
    def _coerce_extraction(value: Any) -> PaperExtractionResult:
        if isinstance(value, PaperExtractionResult):
            return value
        if isinstance(value, tuple) and len(value) == 2:
            draft, diagnostics = value
            return PaperExtractionResult(
                draft=draft,
                diagnostics=dict(diagnostics or {}),
            )
        raise TypeError("抽取器必须返回 PaperExtractionResult 或 (draft, diagnostics)")

__all__ = [
    "LiteratureMapBuildResult",
    "LiteratureMapBuilder",
    "PaperCardExtractor",
]
