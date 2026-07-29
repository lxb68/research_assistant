"""把模型规划结果约束为稳定的用户问题契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.config import settings
from app.services.retrieval_contracts import (
    flatten_requirement_slots,
    normalize_execution_complexity,
    normalize_requirement,
    normalize_section_types,
)
from app.services.document_capabilities import normalize_document_requirements
from app.services.facet_requirement_binder import FacetRequirementBinder
from app.services.retrieval_scope_resolver import RetrievalScopeResolver
from app.services.semantic_context_contract import SemanticContextContractBuilder


@dataclass(slots=True)
class QuestionContract:
    """跨检索阶段传递的稳定问题范围。"""

    standaloneQuestion: str
    questionType: str
    complexity: str
    targetPaperIds: list[str] = field(default_factory=list)
    targetChunks: list[dict[str, Any]] = field(default_factory=list)
    documentRequirements: dict[str, bool] = field(default_factory=dict)
    retrievalFacets: list[dict[str, Any]] = field(default_factory=list)
    coreRequirements: list[str] = field(default_factory=list)
    requirementSpecs: list[dict[str, Any]] = field(default_factory=list)
    optionalDetails: list[str] = field(default_factory=list)
    answerRequirements: list[str] = field(default_factory=list)
    requiresIterativeRetrieval: bool = False
    targetEvidenceCount: int = 1
    needsClarification: bool = False
    clarificationQuestion: str = ""
    candidateSourceCount: int = 0
    invalidTargetIds: list[str] = field(default_factory=list)
    invalidTargetChunks: list[dict[str, Any]] = field(default_factory=list)
    retrievalScopeMode: str = "unscoped"
    scopeExpandedFromHistory: bool = False
    evidenceBreadth: str = "narrow"
    interactionMode: str = "new_topic"
    interactionBasis: list[str] = field(default_factory=list)
    invalidInteractionBasis: list[str] = field(default_factory=list)
    interactionClassificationRepaired: bool = False
    invalidFacetRequirementIds: dict[str, list[str]] = field(default_factory=dict)
    scopeAnchorIds: list[str] = field(default_factory=list)
    invalidScopeAnchorIds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QuestionContractBuilder:
    """只维护问题范围、核心声明要求和允许检索的对象。"""

    ALLOWED_QUESTION_TYPES = {"simple_fact", "mechanism", "comparison", "evaluation", "synthesis"}
    ALLOWED_COMPLEXITIES = {"simple", "complex"}

    def __init__(self, *, max_facets: int | None = None) -> None:
        self.max_facets = max(1, min(int(max_facets or settings.query_planner_max_facets), 8))
        self.scope_resolver = RetrievalScopeResolver()
        self.semantic_context_builder = SemanticContextContractBuilder()
        self.facet_requirement_binder = FacetRequirementBinder()

    def build(
        self,
        payload: dict[str, Any],
        *,
        question: str,
        candidate_sources: list[dict[str, Any]],
        explicit_paper_ids: list[str] | None = None,
        has_history: bool = False,
        available_scope_anchors: list[dict[str, Any]] | None = None,
    ) -> QuestionContract:
        explicit_ids = [str(value) for value in explicit_paper_ids or [] if str(value)]
        known_anchor_ids = {
            str(item.get("id") or "").strip()
            for item in available_scope_anchors or []
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        raw_anchor_ids = payload.get("scope_anchor_ids") or payload.get("scopeAnchorIds")
        proposed_anchor_ids = list(dict.fromkeys(
            str(value).strip()
            for value in (raw_anchor_ids if isinstance(raw_anchor_ids, list) else [])
            if str(value).strip()
        ))
        scope_anchor_ids = [
            value for value in proposed_anchor_ids if value in known_anchor_ids
        ]
        invalid_scope_anchor_ids = [
            value for value in proposed_anchor_ids if value not in known_anchor_ids
        ]
        proposed = payload.get("target_paper_ids")
        proposed = proposed if isinstance(proposed, list) else []

        known_chunks = {(str(source["record_id"]), int(source["chunk_index"])) for source in candidate_sources}
        target_chunks: list[dict[str, Any]] = []
        invalid_chunks: list[dict[str, Any]] = []
        raw_chunks = payload.get("target_chunks")
        for item in raw_chunks if isinstance(raw_chunks, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                chunk_index = int(item.get("chunk_index") or 0)
            except (TypeError, ValueError):
                chunk_index = -1
            reference = {"record_id": str(item.get("record_id") or "").strip(), "chunk_index": chunk_index}
            if (reference["record_id"], chunk_index) in known_chunks:
                if reference not in target_chunks:
                    target_chunks.append(reference)
            else:
                invalid_chunks.append(reference)

        interaction = self.semantic_context_builder.build(
            payload.get("interaction_context") or payload.get("interactionContext"),
            question=question,
            has_history=has_history,
        )
        scope_resolution = self.scope_resolver.resolve(
            question=question,
            requested_scope_mode=payload.get("scope_mode") or payload.get("scopeMode"),
            interaction_mode=interaction.mode,
            reference_basis=interaction.basis,
            proposed_target_ids=proposed,
            explicit_paper_ids=explicit_ids,
            candidate_sources=candidate_sources,
        )
        if scope_resolution.scope_mode == "corpus":
            target_chunks = []
            invalid_chunks = []
        target_ids = scope_resolution.target_paper_ids
        invalid_ids = scope_resolution.invalid_target_ids
        for reference in target_chunks:
            if reference["record_id"] not in target_ids:
                target_ids.append(reference["record_id"])

        document_requirements = normalize_document_requirements(
            payload.get("document_requirements") or payload.get("documentRequirements")
        )

        standalone = str(payload.get("standalone_question") or question).strip()
        question_type = str(payload.get("question_type") or "simple_fact").strip().lower()
        if question_type not in self.ALLOWED_QUESTION_TYPES:
            question_type = "simple_fact"

        facets: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        raw_facets = payload.get("retrieval_facets")
        for index, item in enumerate((raw_facets if isinstance(raw_facets, list) else [])[: self.max_facets], 1):
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or "").strip()
            if not query or query.casefold() in seen_queries:
                continue
            seen_queries.add(query.casefold())
            facets.append({
                "id": str(item.get("id") or f"facet-{index}")[:80],
                "goal": str(item.get("goal") or query)[:500],
                "query": query[:2000],
                "concepts": [str(v).strip()[:200] for v in item.get("concepts") or [] if str(v).strip()][:16],
                "phrases": [str(v).strip()[:300] for v in item.get("phrases") or [] if str(v).strip()][:12],
                "preferredSectionTypes": normalize_section_types(item.get("preferred_section_types") or item.get("preferredSectionTypes") or []),
                "requirementIds": [
                    str(value).strip()[:80]
                    for value in item.get("requirement_ids") or item.get("requirementIds") or []
                    if str(value).strip()
                ][:8],
            })
        if not facets:
            facets = [{"id": "facet-1", "goal": standalone, "query": standalone, "preferredSectionTypes": []}]

        raw_requirements = payload.get("core_requirements")
        if not isinstance(raw_requirements, list):
            raw_requirements = payload.get("answer_requirements")
        requirement_specs = [
            normalized
            for index, value in enumerate((raw_requirements if isinstance(raw_requirements, list) else [])[:8], 1)
            if (normalized := normalize_requirement(value, index, question_type=question_type)) is not None
        ]
        binding = self.facet_requirement_binder.bind(facets, requirement_specs)
        facets = binding.facets
        core_requirements = [item["description"] for item in requirement_specs if item.get("required")]
        raw_optional = payload.get("optional_details")
        optional_details = [str(v).strip()[:500] for v in raw_optional if str(v).strip()][:8] if isinstance(raw_optional, list) else []
        needs_clarification = payload.get("needs_clarification") is True
        if (invalid_ids and not target_ids) or (invalid_chunks and not target_chunks):
            needs_clarification = True
        clarification = str(payload.get("clarification_question") or "").strip()
        if needs_clarification and not clarification:
            clarification = "我无法唯一确定你指的是哪篇文献或哪个片段，请补充论文标题、章节或引用内容。"

        complexity = normalize_execution_complexity(
            str(payload.get("complexity") or "simple").strip().lower(),
            question_type=question_type,
            facet_count=len(facets),
            requirement_count=len(core_requirements),
            broad_evidence_required=(
                str(payload.get("evidence_breadth") or payload.get("evidenceBreadth") or "")
                .strip()
                .casefold()
                == "broad"
            ),
        )
        evidence_breadth = (
            "broad"
            if complexity == "complex"
            else "narrow"
        )
        is_complex = complexity == "complex"
        required_evidence_capacity = sum(
            int(item.get("minimumDirectEvidence") or 1)
            for item in flatten_requirement_slots(
                [item for item in requirement_specs if item.get("required")]
            )
        )
        target_count = (
            max(
                settings.orchestrator_min_evidence,
                settings.rag_complex_target_evidence,
                required_evidence_capacity,
            )
            if is_complex
            else max(settings.orchestrator_min_evidence, required_evidence_capacity)
        )
        return QuestionContract(
            standaloneQuestion=standalone,
            questionType=question_type,
            complexity=complexity,
            targetPaperIds=target_ids,
            targetChunks=target_chunks,
            documentRequirements=document_requirements,
            retrievalFacets=facets,
            coreRequirements=core_requirements,
            requirementSpecs=requirement_specs,
            optionalDetails=optional_details,
            answerRequirements=core_requirements,
            requiresIterativeRetrieval=is_complex and bool(facets or requirement_specs),
            targetEvidenceCount=max(
                1,
                min(int(target_count), settings.research_agent_max_evidence_groups),
            ),
            needsClarification=needs_clarification,
            clarificationQuestion=clarification,
            candidateSourceCount=len(candidate_sources),
            invalidTargetIds=invalid_ids,
            invalidTargetChunks=invalid_chunks,
            retrievalScopeMode=scope_resolution.scope_mode,
            scopeExpandedFromHistory=scope_resolution.expanded_from_history,
            evidenceBreadth=evidence_breadth,
            interactionMode=interaction.mode,
            interactionBasis=interaction.basis,
            invalidInteractionBasis=interaction.invalid_basis,
            interactionClassificationRepaired=interaction.repaired,
            invalidFacetRequirementIds=binding.invalid_requirement_ids,
            scopeAnchorIds=scope_anchor_ids,
            invalidScopeAnchorIds=invalid_scope_anchor_ids,
        )


__all__ = ["QuestionContract", "QuestionContractBuilder"]
