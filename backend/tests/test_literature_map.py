from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app.services.literature_map import (
    EvidenceReference,
    LiteratureMapBuilder,
    LiteratureMapExtractionPolicy,
    LiteratureMapExtractor,
    LiteratureMapProjectService,
    LiteratureMapRepository,
    PaperEntityResolver,
    RelationCandidate,
    RelationMerger,
    VocabularyNormalizer,
    compute_document_version,
)
from app.services.paper_repository import PaperRepository
from app.services.project_repository import ProjectRepository


def _paper() -> dict[str, str]:
    return {
        "id": "paper-1",
        "title": "Evidence-Centered Research",
        "year": "2026",
        "doi": "10.1000/example",
    }


def _chunks() -> list[dict[str, object]]:
    return [
        {
            "record_id": "paper-1",
            "chunk_index": 3,
            "section": "Method",
            "text": "We propose a versioned evidence ledger for research synthesis.",
            "origin_type": "paper_text",
            "extraction_confidence": 0.98,
        },
        {
            "record_id": "paper-1",
            "chunk_index": 8,
            "section": "Related Work",
            "text": "Our method uses the retrieval procedure introduced by PriorMap.",
            "origin_type": "paper_text",
            "extraction_confidence": 0.96,
        },
    ]


def _extractor_response() -> str:
    return json.dumps(
        {
            "summary": "A versioned evidence ledger for research synthesis.",
            "source_language": "en",
            "facets": [
                {"name": "research_task", "values": ["research synthesis"]}
            ],
            "claims": [
                {
                    "kind": "contribution",
                    "subject": "the paper",
                    "predicate": "proposes",
                    "object": "a versioned evidence ledger",
                    "attribution_type": "author_claimed_contribution",
                    "confidence": 0.95,
                    "evidence": [
                        {
                            "ref": "paper-1:3",
                            "quote": "We propose a versioned evidence ledger",
                        }
                    ],
                },
                {
                    "kind": "unsupported",
                    "subject": "the paper",
                    "predicate": "solves",
                    "object": "every synthesis problem",
                    "confidence": 0.99,
                    "evidence": [
                        {
                            "ref": "paper-1:3",
                            "quote": "This quotation is not present in the source.",
                        }
                    ],
                },
            ],
            "relation_candidates": [
                {
                    "relation_type": "uses",
                    "target_label": "PriorMap retrieval procedure",
                    "target_type": "method",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "ref": "paper-1:8",
                            "quote": "uses the retrieval procedure introduced by PriorMap",
                        }
                    ],
                }
            ],
        }
    )


class _FailOnceRepository(LiteratureMapRepository):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.should_fail = True

    def save_card(self, *args, **kwargs) -> None:
        if self.should_fail:
            self.should_fail = False
            raise RuntimeError("模拟卡片事务失败")
        super().save_card(*args, **kwargs)


def test_builder_replays_staged_extraction_after_database_failure() -> None:
    with TemporaryDirectory() as directory:
        repository = _FailOnceRepository(Path(directory) / "literature-map.sqlite3")
        completion = Mock(return_value=_extractor_response())
        extractor = LiteratureMapExtractor(
            completion=completion,
            model={"model": "test"},
            extractor_version="extractor-v1",
            timeout=30,
        )
        builder = LiteratureMapBuilder(repository=repository, extractor=extractor)

        try:
            builder.build_paper(_paper(), _chunks())
        except RuntimeError as error:
            assert str(error) == "模拟卡片事务失败"
        else:
            raise AssertionError("首次写卡应触发模拟故障")

        result = builder.build_paper(_paper(), _chunks())

        assert completion.call_count == 1
        assert result.status == "built"
        assert result.diagnostics["extractionReplayed"] is True


def test_relation_merger_deduplicates_and_rejects_false_resolved_targets() -> None:
    normalizer = VocabularyNormalizer()
    merger = RelationMerger(
        resolver=PaperEntityResolver(
            [
                {"id": "paper-1", "title": "Source"},
                {"id": "paper-2", "title": "PriorMap"},
            ]
        ),
        normalizer=normalizer,
    )
    evidence = EvidenceReference(
        record_id="paper-1",
        chunk_index=3,
        quote="We use PriorMap.",
    )
    relations = merger.merge(
        project_id="",
        source_paper_id="paper-1",
        extractor_version="extractor-v1",
        candidates=[
            RelationCandidate(
                relation_type="Uses",
                target_label="PriorMap",
                target_paper_id="paper-2",
                evidence_refs=[evidence],
                confidence=0.9,
            ),
            RelationCandidate(
                relation_type="uses",
                target_label="PriorMap",
                target_paper_id="paper-2",
                evidence_refs=[evidence],
                confidence=0.8,
            ),
            RelationCandidate(
                relation_type="uses",
                target_label="Source",
                target_paper_id="paper-1",
            ),
            RelationCandidate(
                relation_type="uses",
                target_label="不存在的论文",
                target_paper_id="chunk:3",
            ),
        ],
    )

    assert len(relations) == 2
    resolved = next(item for item in relations if item.status == "resolved")
    candidate = next(item for item in relations if item.status == "candidate")
    assert resolved.target_id == "paper-2"
    assert resolved.canonical_relation_type == "uses"
    assert len(resolved.evidence_refs) == 1
    assert candidate.target_id == "不存在的论文"
    assert candidate.resolution_method == "unresolved"


def test_document_version_is_stable_and_content_addressed() -> None:
    first = compute_document_version(_paper(), evidence_chunks=_chunks())
    second = compute_document_version(
        _paper(),
        evidence_chunks=list(reversed(_chunks())),
    )
    changed_chunks = _chunks()
    changed_chunks[0] = {**changed_chunks[0], "text": "Changed source text."}
    changed = compute_document_version(_paper(), evidence_chunks=changed_chunks)

    assert first == second
    assert changed != first


def test_extractor_accepts_only_quotes_that_exist_in_the_referenced_chunk() -> None:
    completion = Mock(return_value=_extractor_response())
    extractor = LiteratureMapExtractor(
        completion=completion,
        model={"model": "test"},
        extractor_version="extractor-v1",
        timeout=30,
    )

    draft, diagnostics = extractor.extract(_paper(), _chunks())

    assert len(draft.claims) == 1
    assert draft.claims[0].predicate == "proposes"
    assert draft.claims[0].evidence_refs[0].ref == "paper-1:3"
    assert diagnostics["rejectedClaimCount"] == 1
    assert len(draft.relation_candidates) == 1


def test_extractor_does_not_use_annotations_as_formal_evidence() -> None:
    completion = Mock(return_value=_extractor_response())
    extractor = LiteratureMapExtractor(
        completion=completion,
        model={"model": "test"},
        extractor_version="extractor-v1",
        timeout=30,
    )
    chunks = [
        {
            "record_id": "paper-1",
            "chunk_index": 3,
            "text": "We propose a versioned evidence ledger for research synthesis.",
            "origin_type": "pdf_annotation",
        }
    ]

    try:
        extractor.extract(_paper(), chunks)
    except ValueError as error:
        assert "没有可用于构建文献地图" in str(error)
    else:
        raise AssertionError("批注不应成为正式文献地图证据")


def test_extractor_evidence_sources_are_configurable() -> None:
    completion = Mock(return_value=_extractor_response())
    extractor = LiteratureMapExtractor(
        completion=completion,
        model={"model": "test"},
        extractor_version="extractor-v1",
        timeout=30,
        policy=LiteratureMapExtractionPolicy(
            allowed_origin_types=frozenset({"ocr_text"})
        ),
    )
    chunks = [{**_chunks()[0], "origin_type": "ocr_text"}]

    draft, diagnostics = extractor.extract(_paper(), chunks)

    assert len(draft.claims) == 1
    assert draft.claims[0].evidence_refs[0].origin_type == "ocr_text"
    assert diagnostics["acceptedClaimCount"] == 1


def test_builder_reuses_unchanged_card_and_rebuilds_changed_document() -> None:
    with TemporaryDirectory() as directory:
        repository = LiteratureMapRepository(Path(directory) / "literature-map.sqlite3")
        completion = Mock(return_value=_extractor_response())
        extractor = LiteratureMapExtractor(
            completion=completion,
            model={"model": "test"},
            extractor_version="extractor-v1",
            timeout=30,
        )
        builder = LiteratureMapBuilder(
            repository=repository,
            extractor=extractor,
            project_id="project-a",
        )

        first = builder.build_paper(_paper(), _chunks())
        reused = builder.build_paper(_paper(), list(reversed(_chunks())))
        changed_chunks = _chunks()
        changed_chunks[0] = {
            **changed_chunks[0],
            "text": (
                "We propose a versioned evidence ledger for research synthesis. "
                "The implementation is incremental."
            ),
        }
        rebuilt = builder.build_paper(_paper(), changed_chunks)

        assert first.status == "built"
        assert reused.status == "reused"
        assert rebuilt.status == "built"
        assert completion.call_count == 2
        card = repository.get_card("paper-1")
        assert card is not None
        assert len(card.claims) == 1
        relations = repository.list_relations(
            project_id="project-a",
            source_paper_id="paper-1",
        )
        assert len(relations) == 1
        assert relations[0].status == "candidate"


def test_repository_replaces_stale_claims_in_one_transaction() -> None:
    with TemporaryDirectory() as directory:
        repository = LiteratureMapRepository(Path(directory) / "literature-map.sqlite3")
        completion = Mock(return_value=_extractor_response())
        extractor = LiteratureMapExtractor(
            completion=completion,
            model={"model": "test"},
            extractor_version="extractor-v1",
            timeout=30,
        )
        builder = LiteratureMapBuilder(repository=repository, extractor=extractor)
        builder.build_paper(_paper(), _chunks())

        response = json.loads(_extractor_response())
        response["claims"] = []
        response["relation_candidates"] = []
        completion.return_value = json.dumps(response)
        builder.build_paper(_paper(), _chunks(), force=True)

        card = repository.get_card("paper-1")
        assert card is not None
        assert card.claims == []
        assert repository.list_relations(source_paper_id="paper-1") == []


def test_project_service_reuses_global_map_across_projects() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        metadata_path = root / "metadata.sqlite3"
        papers = PaperRepository(metadata_path)
        projects = ProjectRepository(metadata_path)
        paper = {
            **_paper(),
            "splitChunks": [
                {},
                {},
                {},
                {
                    "content": "We propose a versioned evidence ledger for research synthesis.",
                    "headings": [{"heading": "Method"}],
                },
                {},
                {},
                {},
                {},
                {
                    "content": "Our method uses the retrieval procedure introduced by PriorMap.",
                    "headings": [{"heading": "Related Work"}],
                },
            ],
        }
        papers.save(paper)
        first_project = projects.create(name="项目一", paper_ids=["paper-1"])
        second_project = projects.create(name="项目二", paper_ids=["paper-1"])
        repository = LiteratureMapRepository(root / "literature-map.sqlite3")
        extractor = LiteratureMapExtractor(
            completion=Mock(return_value=_extractor_response()),
            model={"model": "test"},
            extractor_version="extractor-v1",
            timeout=30,
        )
        service = LiteratureMapProjectService(
            projects=projects,
            papers=papers,
            repository=repository,
            extractor_version="extractor-v1",
        )

        before = service.snapshot(first_project["id"])
        built = service.build_project(first_project["id"], extractor=extractor)
        second = service.snapshot(second_project["id"])

        assert before["status"] == "empty"
        assert before["pendingPaperCount"] == 1
        assert built["status"] == "ready"
        assert second["status"] == "ready"
        assert second["paperCount"] == 1
        assert second["relationCount"] == 1
