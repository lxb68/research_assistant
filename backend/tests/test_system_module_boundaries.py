"""验证研究问答管线的组件边界与稳定契约。"""

from __future__ import annotations

import json
from unittest.mock import Mock

from app.agents.evidence_evaluator import EvidenceEvaluator
from app.services.answer_composer import AnswerComposer
from app.services.answer_policy import AnswerPolicy
from app.services.candidate_retriever import CandidateRetriever
from app.services.context_resolver import ContextResolver
from app.services.document_structure_indexer import DocumentStructureIndexer
from app.services.document_capabilities import normalize_document_requirements
from app.services.grounding_validator import GroundingValidator
from app.services.evidence_availability import EvidenceAvailabilityEvaluator
from app.services.material_request_policy import MaterialRequestPolicy
from app.services.question_contract_builder import QuestionContractBuilder
from app.services.retrieval_refiner import RetrievalRefiner
from app.services.semantic_context_contract import SemanticContextContractBuilder


def test_context_resolver_only_exposes_unverified_reference_objects() -> None:
    resolved = ContextResolver().resolve(
        "它的方法是什么？",
        [
            {"role": "user", "content": "介绍论文 A"},
            {
                "role": "assistant",
                "content": "论文 A 使用方法 M [1]",
                "sources": [{"recordId": "paper-a", "chunkIndex": 2, "title": "A"}],
            },
        ],
    )

    planning = resolved.for_planning()
    assert planning["history_available"] is True
    assert planning["prior_answers"][0]["allowed_as_evidence"] is False
    assert resolved.candidate_sources[0]["record_id"] == "paper-a"


def test_question_contract_builder_rejects_unknown_scope() -> None:
    contract = QuestionContractBuilder(max_facets=2).build(
        {
            "standalone_question": "论文 A 的机制是什么？",
            "question_type": "mechanism",
            "complexity": "complex",
            "interaction_context": {"mode": "reference", "basis": ["它"]},
            "scope_mode": "referenced",
            "target_paper_ids": ["invented"],
            "retrieval_facets": [{"id": "f1", "query": "mechanism", "preferred_section_types": ["methods"]}],
            "core_requirements": [{"id": "r1", "description": "解释机制", "evidence_intent": "mechanism"}],
        },
        question="它的机制是什么？",
        candidate_sources=[{"record_id": "paper-a", "chunk_index": 2}],
        has_history=True,
    )

    assert contract.needsClarification is True
    assert contract.targetPaperIds == []
    assert contract.invalidTargetIds == ["invented"]
    assert contract.retrievalFacets[0]["preferredSectionTypes"] == ["method"]
    assert contract.requirementSpecs[0]["evidenceIntent"] == "mechanism"


def test_question_contract_structurally_binds_facets_to_core_requirements() -> None:
    contract = QuestionContractBuilder().build(
        {
            "standalone_question": "概括主要类别",
            "question_type": "synthesis",
            "retrieval_facets": [
                {"id": "taxonomy", "query": "main categories", "requirement_ids": ["req-1"]},
                {"id": "implementation", "query": "implementation details"},
                {"id": "invalid", "query": "invalid binding", "requirement_ids": ["unknown"]},
            ],
            "core_requirements": [{"id": "req-1", "description": "说明主要类别"}],
        },
        question="概括主要类别",
        candidate_sources=[],
    )

    facets = {item["id"]: item for item in contract.retrievalFacets}
    assert facets["taxonomy"]["role"] == "required"
    assert facets["taxonomy"]["requirementIds"] == ["req-1"]
    assert facets["implementation"]["role"] == "exploratory"
    assert facets["invalid"]["role"] == "exploratory"
    assert contract.invalidFacetRequirementIds == {"invalid": ["unknown"]}


def test_question_contract_preserves_atomic_chronology_slots() -> None:
    contract = QuestionContractBuilder().build(
        {
            "standalone_question": "梳理技术发展脉络",
            "question_type": "synthesis",
            "complexity": "complex",
            "retrieval_facets": [
                {
                    "id": "timeline",
                    "query": "technology history evolution",
                    "requirement_ids": ["req-lineage"],
                }
            ],
            "core_requirements": [
                {
                    "id": "req-lineage",
                    "description": "梳理发展脉络",
                    "kind": "chronology",
                    "coverage_slots": [
                        {"id": "early", "description": "早期方案", "role": "predecessor"},
                        {"id": "middle", "description": "中间转折", "role": "transition"},
                        {"id": "recent", "description": "近期方案", "role": "recent"},
                    ],
                    "minimum_distinct_sources": 2,
                    "minimum_distinct_periods": 2,
                }
            ],
        },
        question="梳理技术发展脉络",
        candidate_sources=[],
    )

    requirement = contract.requirementSpecs[0]
    assert requirement["kind"] == "chronology"
    assert [item["id"] for item in requirement["coverageSlots"]] == [
        "early",
        "middle",
        "recent",
    ]
    assert contract.targetEvidenceCount >= 3


def test_selected_full_text_availability_is_independent_of_corpus_completeness() -> None:
    diagnostics = EvidenceAvailabilityEvaluator().evaluate(
        [
            {"id": "selected", "content": "full text"},
            {"id": "unparsed", "content": ""},
        ],
        [{"record_id": "selected", "chunk_index": 1}],
        read_full_text=lambda paper: str(paper.get("content") or ""),
    )

    assert diagnostics["corpusFullTextComplete"] is False
    assert diagnostics["relevantFullTextAvailable"] is True
    assert diagnostics["fullTextAvailable"] is True


def test_material_request_policy_uses_structured_core_gaps() -> None:
    policy = MaterialRequestPolicy()
    bounded = policy.decide(
        plan={"requirementSpecs": [{"id": "req-1", "description": "说明主要类别"}]},
        evaluation={
            "answerable": False,
            "requirementAssessments": [{"id": "req-1", "status": "partial"}],
        },
        diagnostics={"evidenceCount": 3, "relevantFullTextAvailable": True},
    )
    requested = policy.decide(
        plan={"requirementSpecs": [{"id": "req-1", "description": "说明主要类别"}]},
        evaluation={
            "answerable": False,
            "requirementAssessments": [
                {"id": "req-1", "status": "unsupported", "missingDetail": "缺少直接分类证据"}
            ],
        },
        diagnostics={"evidenceCount": 0, "relevantFullTextAvailable": False},
    )

    assert bounded.action == "bounded_answer"
    assert requested.action == "request_materials"
    assert requested.required_materials[0]["requirementId"] == "req-1"
    assert "说明主要类别" in requested.required_materials[0]["description"]
    assert "实验设置" not in requested.required_materials[0]["description"]


def test_supported_core_requirement_is_answerable_with_exploratory_gap() -> None:
    completion = Mock(return_value=json.dumps({
        "facets": [
            {"id": "core", "status": "supported", "supporting_refs": ["paper-1:1"]},
            {"id": "extra", "status": "partial", "supporting_refs": ["paper-1:1"]},
        ],
        "requirements": [
            {"id": "req-1", "status": "supported", "supporting_refs": ["paper-1:1"]}
        ],
        "optional_details": [],
    }))
    evaluation, _ = EvidenceEvaluator().evaluate_semantic(
        [{"record_id": "paper-1", "chunk_index": 1, "title": "A", "section": "Overview", "text": "direct evidence"}],
        {
            "standaloneQuestion": "概括主要类别",
            "questionType": "synthesis",
            "retrievalFacets": [
                {"id": "core", "goal": "主要类别", "query": "categories", "role": "required", "requirementIds": ["req-1"]},
                {"id": "extra", "goal": "扩展实现", "query": "implementation", "role": "exploratory", "requirementIds": []},
            ],
            "requirementSpecs": [{"id": "req-1", "description": "说明主要类别", "minimumDirectEvidence": 1}],
        },
        completion=completion,
        model={"model": "test"},
        timeout=30,
    )

    assert evaluation["answerable"] is True
    assert evaluation["blockingMissingFacetIds"] == []
    assert evaluation["exploratoryMissingFacetIds"] == ["extra"]
    assert evaluation["refinementFacets"] == []


def test_semantic_context_contract_validates_mode_and_current_question_basis() -> None:
    classification = SemanticContextContractBuilder().build(
        {"mode": "correction", "basis": ["重新核对"]},
        question="请重新核对这个结论",
        has_history=True,
    )

    assert classification.mode == "correction"
    assert classification.basis == ["重新核对"]
    assert classification.repaired is False


def test_semantic_context_contract_repairs_unverifiable_classification() -> None:
    classification = SemanticContextContractBuilder().build(
        {"mode": "reference", "basis": ["上面两篇"]},
        question="分析当前项目",
        has_history=True,
    )

    assert classification.mode == "followup"
    assert classification.basis == []
    assert classification.invalid_basis == ["上面两篇"]
    assert classification.repaired is True


def test_question_contract_builder_preserves_only_typed_document_requirements() -> None:
    contract = QuestionContractBuilder().build(
        {
            "standalone_question": "介绍已解析全文的文献",
            "document_requirements": {
                "has_pdf": None,
                "has_abstract": "yes",
                "has_parsed_full_text": True,
                "unknown_capability": True,
            },
        },
        question="介绍已解析全文的文献",
        candidate_sources=[],
    )

    assert contract.documentRequirements == {"hasParsedFullText": True}
    assert normalize_document_requirements(contract.documentRequirements) == {"hasParsedFullText": True}


def test_document_structure_indexer_preserves_structure_continuity() -> None:
    indexer = DocumentStructureIndexer(target_tokens=8, max_tokens=12, overlap_tokens=0)

    class Chunk:
        def __init__(self, **values: object) -> None:
            self.__dict__.update(values)

    chunks = indexer.index_paper(
        {
            "id": "paper-a",
            "title": "A",
            "splitChunks": [
                {"content": "step one shared protocol", "structureId": "protocol-1", "structurePartIndex": 1, "structurePartCount": 2},
                {"content": "step two final output", "structureId": "protocol-1", "structurePartIndex": 2, "structurePartCount": 2},
            ],
        },
        chunk_factory=Chunk,
    )

    assert {chunk.structure_id for chunk in chunks} == {"protocol-1"}
    assert chunks[0].is_structure_start is True
    assert chunks[-1].is_structure_end is True


def test_candidate_retriever_returns_wide_ranked_pool_without_assembly() -> None:
    class Chunk:
        def __init__(self, text: str) -> None:
            self.text = text
            self.title = "Paper"
            self.section = "Method"
            self.score = 0.0
            self.token_count = len(text.split())
            self.overlap_token_count = 0
            self.structure_id = ""

    candidates = [Chunk("target mechanism input"), Chunk("target mechanism output"), Chunk("background only")]
    retriever = CandidateRetriever(
        index_paper=lambda _: candidates,
        tokenize=lambda text: text.lower().split(),
        searchable_text=lambda chunk: f"{chunk.title} {chunk.section} {chunk.text}",
        embedding_clients=[],
        vector_store=None,
        bm25_weight=0.45,
        vector_weight=0.55,
    )

    batch = retriever.retrieve("target mechanism", [{}])
    assert len(batch.candidates) == 3
    assert len(batch.ranked) == 2
    assert "evidenceCount" not in batch.diagnostics


def test_retrieval_refiner_only_compensates_unsupported_claims() -> None:
    refinements = RetrievalRefiner().refine(
        {"requirementSpecs": [{"id": "r1", "description": "解释密钥交换", "preferredSectionTypes": ["protocol"]}]},
        {"requirementAssessments": [{"id": "r1", "status": "partial", "missingDetail": "缺少消息步骤", "refinementQuery": "key exchange message steps"}]},
    )
    assert refinements == [{
        "id": "requirement-r1",
        "goal": "解释密钥交换",
        "query": "key exchange message steps",
        "evidenceIntent": "fact",
        "preferredSectionTypes": ["protocol"],
    }]


def test_answer_policy_composer_and_grounding_validator_are_independent() -> None:
    completion = Mock(return_value="机制由两步组成 [1]。")
    answer = AnswerComposer(completion=completion, policy=AnswerPolicy()).compose(
        model={"model": "test"},
        base_prompt="证据：{{evidence}}",
        evidence_context="[1] 第一步；第二步",
        question="机制是什么？",
        resolved_question="论文 A 的机制是什么？",
        answer_requirements=["说明步骤"],
        retrieval_state={"evidenceSufficient": True, "evidenceCount": 1},
        timeout=30,
    )
    result = GroundingValidator().validate(
        answer,
        source_count=1,
        retrieval_state={"requiredCitationGroups": [[1]]},
    )

    assert result.valid is True
    assert result.cited_indices == {1}
    assert "核心覆盖目标" in completion.call_args.args[1][0]["content"]


def test_grounding_validator_requires_inline_evidence_for_strong_claims() -> None:
    invalid = GroundingValidator().validate(
        "该方案首次实现完全同态训练。一般背景见文献 [1]。",
        source_count=1,
        retrieval_state={"enforceClaimConsistency": True},
    )
    valid = GroundingValidator().validate(
        "论文作者声称该方案首次实现其设定下的完全同态训练 [1]。",
        source_count=1,
        retrieval_state={"enforceClaimConsistency": True},
    )

    assert invalid.valid is False
    assert any("强声明" in reason for reason in invalid.reasons)
    assert valid.valid is True
