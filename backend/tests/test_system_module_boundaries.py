"""验证研究问答管线的组件边界与稳定契约。"""

from __future__ import annotations

from unittest.mock import Mock

from app.services.answer_composer import AnswerComposer
from app.services.answer_policy import AnswerPolicy
from app.services.candidate_retriever import CandidateRetriever
from app.services.context_resolver import ContextResolver
from app.services.document_structure_indexer import DocumentStructureIndexer
from app.services.grounding_validator import GroundingValidator
from app.services.question_contract_builder import QuestionContractBuilder
from app.services.retrieval_refiner import RetrievalRefiner


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
    assert planning["usage_mode"] == "reference"
    assert planning["prior_answers"][0]["allowed_as_evidence"] is False
    assert resolved.candidate_sources[0]["record_id"] == "paper-a"


def test_question_contract_builder_rejects_unknown_scope() -> None:
    contract = QuestionContractBuilder(max_facets=2).build(
        {
            "standalone_question": "论文 A 的机制是什么？",
            "question_type": "mechanism",
            "complexity": "complex",
            "target_paper_ids": ["invented"],
            "retrieval_facets": [{"id": "f1", "query": "mechanism", "preferred_section_types": ["methods"]}],
            "core_requirements": [{"id": "r1", "description": "解释机制", "evidence_intent": "mechanism"}],
        },
        question="它的机制是什么？",
        candidate_sources=[{"record_id": "paper-a", "chunk_index": 2}],
    )

    assert contract.needsClarification is True
    assert contract.targetPaperIds == []
    assert contract.invalidTargetIds == ["invented"]
    assert contract.retrievalFacets[0]["preferredSectionTypes"] == ["method"]
    assert contract.requirementSpecs[0]["evidenceIntent"] == "mechanism"


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
