"""项目文献地图的应用服务：连接项目成员、论文正文与全局派生地图。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from app.services.literature_map.builder import LiteratureMapBuilder, PaperCardExtractor
from app.services.literature_map.metadata_quality import PaperMetadataValidator
from app.services.literature_map.models import RelationCandidate
from app.services.literature_map.normalization import VocabularyNormalizer
from app.services.literature_map.repository import LiteratureMapRepository
from app.services.literature_map.resolution import PaperEntityResolver, RelationMerger
from app.services.literature_map.versioning import compute_document_version
from app.services.paper_repository import PaperRepository
from app.services.project_repository import ProjectRepository
from app.services.split import parse_markdown_sections


ProgressCallback = Callable[[int, int, str, str], None]
CancelCallback = Callable[[], None]


@dataclass(frozen=True, slots=True)
class PaperEvidenceAdapter:
    """把论文持久化格式转换成抽取器使用的稳定证据块。"""

    semantic_origin_types: dict[str, str] = field(
        default_factory=lambda: {
            "table": "table",
            "equation": "equation",
            "formula": "equation",
            "figure_caption": "figure_caption",
            "caption": "figure_caption",
        }
    )

    def chunks(self, paper: dict[str, Any]) -> list[dict[str, Any]]:
        paper_id = str(paper.get("id") or paper.get("recordId") or "").strip()
        if not paper_id:
            return []
        raw_chunks = paper.get("splitChunks") or paper.get("split_chunks")
        if isinstance(raw_chunks, list):
            prepared = [
                chunk
                for index, raw in enumerate(raw_chunks)
                if (chunk := self._from_split_chunk(paper_id, index, raw)) is not None
            ]
            if prepared:
                return prepared
        return self._from_markdown(paper_id, self._read_markdown(paper))

    def _from_split_chunk(
        self,
        paper_id: str,
        index: int,
        raw: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("content") or raw.get("text") or "").strip()
        if not text:
            return None
        headings = raw.get("headings") if isinstance(raw.get("headings"), list) else []
        section = " > ".join(
            str(item.get("heading") or "").strip()
            for item in headings
            if isinstance(item, dict) and str(item.get("heading") or "").strip()
        )
        semantic_type = str(
            raw.get("semanticType") or raw.get("semantic_type") or ""
        ).strip().lower()
        return {
            "record_id": paper_id,
            "chunk_index": index,
            "section": section,
            "text": text,
            "origin_type": self.semantic_origin_types.get(semantic_type, "paper_text"),
            "extraction_confidence": 1.0,
        }

    @staticmethod
    def _read_markdown(paper: dict[str, Any]) -> str:
        for key in ("markdownPath", "markdown_path"):
            value = str(paper.get(key) or "").strip()
            if value:
                path = Path(value)
                if path.is_file():
                    return path.read_text(encoding="utf-8", errors="ignore")
        output_dir = str(
            paper.get("markdownOutputDir") or paper.get("markdown_output_dir") or ""
        ).strip()
        path = Path(output_dir) / "full.md" if output_dir else None
        return (
            path.read_text(encoding="utf-8", errors="ignore")
            if path and path.is_file()
            else ""
        )

    @staticmethod
    def _from_markdown(paper_id: str, markdown: str) -> list[dict[str, Any]]:
        _outline, sections = parse_markdown_sections(markdown)
        return [
            {
                "record_id": paper_id,
                "chunk_index": index,
                "section": str(section.get("heading") or "").strip(),
                "text": text,
                "origin_type": "paper_text",
                "extraction_confidence": 1.0,
            }
            for index, section in enumerate(sections)
            if (text := str(section.get("content") or "").strip())
        ]


class LiteratureMapProjectService:
    """构建全局论文卡片，并按项目成员投影文献地图。"""

    def __init__(
        self,
        *,
        projects: ProjectRepository,
        papers: PaperRepository,
        repository: LiteratureMapRepository,
        extractor_version: str,
        schema_version: int = LiteratureMapBuilder.SCHEMA_VERSION,
        evidence_adapter: PaperEvidenceAdapter | None = None,
        normalizer: VocabularyNormalizer | None = None,
        metadata_validator: PaperMetadataValidator | None = None,
    ) -> None:
        self.projects = projects
        self.papers = papers
        self.repository = repository
        self.extractor_version = str(extractor_version or "").strip()
        self.schema_version = int(schema_version)
        self.evidence_adapter = evidence_adapter or PaperEvidenceAdapter()
        self.normalizer = normalizer or VocabularyNormalizer()
        self.metadata_validator = metadata_validator or PaperMetadataValidator()
        if not self.extractor_version:
            raise ValueError("extractor_version 不能为空")

    def project_papers(self, project_id: str) -> list[dict[str, Any]]:
        self.projects.require(project_id)
        return self.papers.list_by_ids(self.projects.list_paper_ids(project_id))

    def snapshot(self, project_id: str) -> dict[str, Any]:
        prepared = self._prepared_project_papers(project_id)
        paper_ids = [str(paper["id"]) for paper, _chunks in prepared]
        cards = self.repository.list_cards_by_ids(paper_ids)
        cards_by_id = {card.paper_id: card for card in cards}
        stale_paper_ids = [
            paper_id
            for paper, chunks in prepared
            if (
                (paper_id := str(paper["id"]))
                and self.repository.needs_rebuild(
                    paper_id,
                    document_version=compute_document_version(
                        paper,
                        evidence_chunks=chunks,
                    ),
                    extractor_version=self.extractor_version,
                    schema_version=self.schema_version,
                )
            )
        ]
        build_statuses = self.repository.get_build_statuses(paper_ids)
        failed_paper_ids = [
            paper_id
            for paper_id in stale_paper_ids
            if build_statuses.get(paper_id, {}).get("status") == "failed"
        ]
        relations = self.repository.list_relations_for_sources(
            paper_ids,
            project_id="",
        )
        status = (
            "empty"
            if not cards
            else (
                "partial"
                if failed_paper_ids
                else ("stale" if stale_paper_ids else "ready")
            )
        )
        return {
            "projectId": project_id,
            "status": status,
            "sourcePaperCount": len(prepared),
            "paperCount": len(cards),
            "claimCount": sum(len(card.claims) for card in cards),
            "relationCount": len(relations),
            "pendingPaperCount": len(stale_paper_ids),
            "failedPaperCount": len(failed_paper_ids),
            "failedPaperIds": failed_paper_ids,
            "failures": [
                {
                    "paperId": paper_id,
                    "error": build_statuses[paper_id].get("error", ""),
                }
                for paper_id in failed_paper_ids
            ],
            "stalePaperIds": stale_paper_ids,
            "cards": [cards_by_id[paper_id].to_dict() for paper_id in paper_ids if paper_id in cards_by_id],
            "relations": [relation.to_dict() for relation in relations],
        }

    def build_project(
        self,
        project_id: str,
        *,
        extractor: PaperCardExtractor,
        force: bool = False,
        progress: ProgressCallback | None = None,
        check_cancelled: CancelCallback | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepared_project_papers(project_id)
        if not prepared:
            raise ValueError("当前项目没有可用于构建文献地图的 Markdown 文献")
        library_papers = self.papers.list(limit=500)
        normalized_card_count = self.repair_project_cards(
            project_id,
            papers=[paper for paper, _chunks in prepared],
        )
        relation_merger = RelationMerger(
            resolver=PaperEntityResolver(library_papers),
            normalizer=self.normalizer,
        )
        repaired_relation_count = self.repair_project_relations(
            project_id,
            paper_ids=[str(paper["id"]) for paper, _chunks in prepared],
            relation_merger=relation_merger,
        )
        builder = LiteratureMapBuilder(
            repository=self.repository,
            extractor=extractor,
            project_id="",
            schema_version=self.schema_version,
            normalizer=self.normalizer,
            relation_merger=relation_merger,
            metadata_validator=self.metadata_validator,
        )
        counts = {"built": 0, "reused": 0, "failed": 0}
        failures: list[dict[str, str]] = []
        total = len(prepared)
        for index, (paper, chunks) in enumerate(prepared, start=1):
            if check_cancelled:
                check_cancelled()
            paper_id = str(paper["id"])
            try:
                result = builder.build_paper(paper, chunks, force=force)
                counts[result.status] = counts.get(result.status, 0) + 1
                outcome = result.status
            except Exception as error:
                counts["failed"] += 1
                failures.append({"paperId": paper_id, "error": str(error)[:1000]})
                outcome = "failed"
            if progress:
                progress(index, total, paper_id, outcome)
        return {
            "projectId": project_id,
            "status": "partial" if failures else "ready",
            "totalPaperCount": total,
            "builtPaperCount": counts["built"],
            "reusedPaperCount": counts["reused"],
            "failedPaperCount": counts["failed"],
            "repairedRelationCount": repaired_relation_count,
            "normalizedCardCount": normalized_card_count,
            "failures": failures,
            "snapshot": self.snapshot(project_id),
        }

    def repair_project_relations(
        self,
        project_id: str,
        *,
        paper_ids: list[str] | None = None,
        relation_merger: RelationMerger | None = None,
    ) -> int:
        """用真实文献库重新解析已有关系；不调用模型，可安全重复执行。"""
        source_ids = paper_ids or [
            str(paper["id"]) for paper in self.project_papers(project_id)
        ]
        existing = self.repository.list_relations_for_sources(
            source_ids,
            project_id="",
        )
        if not existing:
            return 0
        merger = relation_merger or RelationMerger(
            resolver=PaperEntityResolver(self.papers.list(limit=500)),
            normalizer=self.normalizer,
        )
        by_source: dict[str, list[RelationCandidate]] = {}
        for relation in existing:
            target_label = str(
                relation.qualifiers.get("targetLabel") or relation.target_id
            ).strip()
            by_source.setdefault(relation.source_paper_id, []).append(
                RelationCandidate(
                    relation_type=(
                        relation.raw_relation_type or relation.relation_type
                    ),
                    target_label=target_label,
                    target_paper_id=(
                        relation.target_id
                        if relation.target_type == "paper"
                        else str(
                            relation.qualifiers.get("proposedTargetPaperId") or ""
                        )
                    ),
                    target_type=relation.target_type,
                    qualifiers=relation.qualifiers,
                    confidence=relation.confidence,
                    evidence_refs=relation.evidence_refs,
                )
            )
        repaired = []
        for source_id, candidates in by_source.items():
            repaired.extend(
                merger.merge(
                    project_id="",
                    source_paper_id=source_id,
                    extractor_version=self.extractor_version,
                    candidates=candidates,
                )
            )
        self.repository.rewrite_relations(
            source_paper_ids=list(by_source),
            relations=repaired,
            project_id="",
        )
        return len(repaired)

    def repair_project_cards(
        self,
        project_id: str,
        *,
        papers: list[dict[str, Any]] | None = None,
    ) -> int:
        """回填旧卡片的规范词表和元数据质量字段，不重新抽取正文。"""
        project_papers = papers or self.project_papers(project_id)
        papers_by_id = {
            str(paper.get("id") or ""): paper
            for paper in project_papers
            if str(paper.get("id") or "")
        }
        cards = self.repository.list_cards_by_ids(list(papers_by_id))
        updated = 0
        for card in cards:
            paper = papers_by_id[card.paper_id]
            normalized = replace(
                card,
                canonical_facets=self.normalizer.normalize_facets(card.facets),
                claims=[
                    self.normalizer.normalize_claim(claim)
                    for claim in card.claims
                ],
                metadata_warnings=self.metadata_validator.validate(paper),
                metadata_provenance={
                    **card.metadata_provenance,
                    "source": str(paper.get("source") or "library"),
                    "validatorVersion": self.metadata_validator.version,
                },
            )
            if normalized == card:
                continue
            self.repository.save_card(normalized, relations=None)
            updated += 1
        return updated

    def _prepared_project_papers(
        self,
        project_id: str,
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        return [
            (paper, chunks)
            for paper in self.project_papers(project_id)
            if (chunks := self.evidence_adapter.chunks(paper))
        ]


__all__ = ["LiteratureMapProjectService", "PaperEvidenceAdapter"]
