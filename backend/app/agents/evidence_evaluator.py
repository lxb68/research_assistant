"""根据检索计划验证证据覆盖，并生成受约束的补偿检索任务。"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.core.config import settings
from app.prompt_loader import load_prompt
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT
from app.services.retrieval_contracts import (
    flatten_requirement_slots,
    normalize_requirement,
)
from app.services.retrieval_refiner import RetrievalRefiner


class EvidenceEvaluator:
    """使用可解释指标评估证据，不让原始片段数量替代语义完整性。"""

    METHOD_SECTION_TYPES = {"method", "framework", "protocol", "algorithm", "implementation", "overview"}
    SEMANTIC_PROMPT = load_prompt("evidence/evaluator.zh.md")

    def evaluate(
        self,
        diagnostics: dict[str, Any],
        *,
        plan: dict[str, Any] | None = None,
        required_paper_ids: list[str] | None = None,
        required_chunk_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        evidence_count = int(diagnostics.get("evidenceCount") or 0)
        distinct_papers = int(diagnostics.get("distinctPaperCount") or 0)
        query_coverage = float(diagnostics.get("queryCoverage") or 0)
        required_ids = {str(record_id) for record_id in required_paper_ids or [] if str(record_id)}
        if evidence_count < settings.orchestrator_min_evidence:
            reasons.append(f"相关证据片段仅 {evidence_count} 条")
        minimum_distinct_papers = min(2, settings.orchestrator_min_evidence)
        if required_ids:
            minimum_distinct_papers = min(minimum_distinct_papers, len(required_ids))
        if distinct_papers < minimum_distinct_papers:
            reasons.append(f"相关证据仅覆盖 {distinct_papers} 篇文献")
        if query_coverage < settings.orchestrator_min_query_coverage:
            reasons.append(f"问题关键词覆盖率仅 {query_coverage:.0%}")

        selected_ids = {str(record_id) for record_id in diagnostics.get("selectedPaperIds") or [] if str(record_id)}
        missing_ids = sorted(required_ids - selected_ids)
        if missing_ids:
            reasons.append(f"指定文献中有 {len(missing_ids)} 篇未检索到有效证据")
        required_chunks = {
            (str(item.get("record_id") or item.get("recordId") or ""), int(item.get("chunk_index") or item.get("chunkIndex") or 0))
            for item in required_chunk_refs or []
        }
        resolved_chunks = {
            (str(item.get("recordId") or item.get("record_id") or ""), int(item.get("chunkIndex") or item.get("chunk_index") or 0))
            for item in diagnostics.get("resolvedChunkRefs") or []
        }
        missing_chunks = required_chunks - resolved_chunks
        if missing_chunks:
            reasons.append(f"指定片段中有 {len(missing_chunks)} 条无法从本地文献恢复")

        normalized_plan = plan or {}
        complexity = str(normalized_plan.get("complexity") or "simple")
        question_type = str(normalized_plan.get("questionType") or "simple_fact")
        facet_count = int(diagnostics.get("facetCount") or 0)
        retrieval_facet_coverage = float(
            diagnostics.get("retrievalFacetCoverage")
            or diagnostics.get("facetCoverage")
            or (1.0 if facet_count == 0 else 0.0)
        )
        method_evidence_count = int(diagnostics.get("methodEvidenceCount") or 0)
        # retrievalFacetCoverage 只描述检索支路是否返回候选，不能替代语义支持度。
        if complexity == "complex" and question_type == "mechanism" and not normalized_plan.get("requirementSpecs"):
            minimum_method_evidence = min(
                settings.orchestrator_min_method_evidence,
                max(1, int(normalized_plan.get("targetEvidenceCount") or settings.orchestrator_min_method_evidence)),
            )
            if method_evidence_count < minimum_method_evidence:
                reasons.append(f"方法、框架或协议类证据仅 {method_evidence_count} 条")

        return {
            "sufficient": not reasons,
            "reasons": reasons,
            "missingFacetIds": list(diagnostics.get("missingFacetIds") or []),
            "facetCoverage": retrieval_facet_coverage,
            "retrievalFacetCoverage": retrieval_facet_coverage,
            "methodEvidenceCount": method_evidence_count,
        }

    def evaluate_semantic(
        self,
        evidence: list[dict[str, Any]],
        plan: dict[str, Any],
        *,
        completion: Callable[..., str],
        model: dict[str, Any],
        timeout: int,
    ) -> tuple[dict[str, Any], str]:
        """让模型逐项验证真实证据支持度，并严格校验其证据引用。"""
        evidence_payload: list[dict[str, Any]] = []
        known_refs: set[str] = set()
        evidence_metadata: dict[str, dict[str, str]] = {}
        for item in evidence:
            record_id = str(item.get("record_id") or "")
            chunk_index = int(item.get("chunk_index") or 0)
            reference = f"{record_id}:{chunk_index}"
            known_refs.add(reference)
            evidence_metadata[reference] = {
                "recordId": record_id,
                "year": str(item.get("year") or ""),
            }
            evidence_payload.append(
                {
                    "ref": reference,
                    "title": str(item.get("title") or "")[:500],
                    "section": str(item.get("section") or "")[:1000],
                    "text": str(item.get("text") or "")[:3500],
                }
            )
        facets = [
            {
                "id": str(item.get("id") or ""),
                "goal": str(item.get("goal") or "")[:800],
                "query": str(item.get("query") or "")[:1200],
                "role": str(item.get("role") or "required"),
                "requirementIds": [
                    str(value)
                    for value in item.get("requirementIds") or []
                    if str(value)
                ],
            }
            for item in plan.get("retrievalFacets") or []
            if isinstance(item, dict) and str(item.get("id") or "")
        ]
        raw_requirements = plan.get("requirementSpecs") or plan.get("coreRequirements") or plan.get("answerRequirements") or []
        core_requirements = [
            normalized
            for index, value in enumerate(raw_requirements, start=1)
            if (
                normalized := normalize_requirement(
                    value,
                    index,
                    question_type=str(plan.get("questionType") or "simple_fact"),
                )
            ) is not None
            and normalized.get("required")
        ]
        coverage_slots = flatten_requirement_slots(core_requirements)
        optional_details = [
            {"id": f"optional-{index}", "detail": str(value)[:800]}
            for index, value in enumerate(plan.get("optionalDetails") or [], start=1)
            if str(value).strip()
        ]
        raw_response = completion(
            model,
            [
                {"role": "system", "content": f"{self.SEMANTIC_PROMPT}\n\n{SYSTEM_SECURITY_CONSTRAINT}"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": str(plan.get("standaloneQuestion") or ""),
                            "facets": facets,
                            "core_requirements": [
                                {
                                    "id": item["id"],
                                    "requirement": item["description"],
                                    "evidence_intent": item["evidenceIntent"],
                                    "preferred_section_types": item["preferredSectionTypes"],
                                    "minimum_direct_evidence": item["minimumDirectEvidence"],
                                    "kind": item["kind"],
                                    "minimum_distinct_sources": item["minimumDistinctSources"],
                                    "minimum_distinct_periods": item["minimumDistinctPeriods"],
                                }
                                for item in core_requirements
                            ],
                            "coverage_slots": [
                                {
                                    "id": item["id"],
                                    "parent_requirement_id": item["parentRequirementId"],
                                    "description": item["description"],
                                    "role": item["role"],
                                    "minimum_direct_evidence": item["minimumDirectEvidence"],
                                }
                                for item in coverage_slots
                            ],
                            "optional_details": optional_details,
                            "evidence": evidence_payload,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            timeout=timeout,
            response_format={"type": "json_object"},
        )
        try:
            payload = self._parse_semantic_response(raw_response)
        except Exception as error:
            setattr(error, "raw_response", str(raw_response or ""))
            raise
        facet_assessments = self._normalize_assessments(
            payload.get("facets"),
            allowed_ids={item["id"] for item in facets},
            known_refs=known_refs,
        )
        model_requirement_assessments = self._normalize_assessments(
            payload.get("requirements"),
            allowed_ids={item["id"] for item in core_requirements},
            known_refs=known_refs,
        )
        explicit_slot_ids = {
            item["id"]
            for item in coverage_slots
            if item["id"] != item["parentRequirementId"]
        }
        slot_assessments = self._normalize_assessments(
            payload.get("coverage_slots")
            or payload.get("coverageSlots")
            or payload.get("slots"),
            allowed_ids={item["id"] for item in coverage_slots},
            known_refs=known_refs,
        )
        # 旧计划的单槽位 id 与父要求相同，继续接受旧验证器的 requirements 输出。
        if not explicit_slot_ids:
            slot_assessments = model_requirement_assessments
        slot_specs = {item["id"]: item for item in coverage_slots}
        slot_status = {item["id"]: item for item in slot_assessments}
        for slot in coverage_slots:
            slot_status.setdefault(
                slot["id"],
                {
                    "id": slot["id"],
                    "status": "unsupported",
                    "supportingRefs": [],
                    "missingDetail": "验证器未找到该原子覆盖槽位的直接支持证据",
                    "refinementQuery": slot.get("queryHint") or slot["description"],
                },
            )
        slot_assessments = list(slot_status.values())
        for assessment in slot_assessments:
            minimum_refs = int(slot_specs[assessment["id"]]["minimumDirectEvidence"])
            if assessment["status"] == "supported" and len(assessment["supportingRefs"]) < minimum_refs:
                assessment["status"] = "partial"
                assessment["missingDetail"] = (
                    assessment.get("missingDetail")
                    or f"直接支持证据少于要求的 {minimum_refs} 条"
                )
        requirement_assessments = self._aggregate_requirements(
            core_requirements,
            slot_assessments,
            evidence_metadata=evidence_metadata,
        )
        optional_assessments = self._normalize_assessments(
            payload.get("optional_details"),
            allowed_ids={item["id"] for item in optional_details},
            known_refs=known_refs,
        )
        facet_status = {item["id"]: item for item in facet_assessments}
        # 模型漏掉的规划项必须按 unsupported 处理，不能静默算作覆盖。
        for item in facets:
            facet_status.setdefault(
                item["id"],
                {"id": item["id"], "status": "unsupported", "supportingRefs": [], "missingDetail": "验证器未找到直接支持证据", "refinementQuery": item["query"]},
            )
        facet_assessments = list(facet_status.values())
        missing_facets = [item["id"] for item in facet_assessments if item["status"] != "supported"]
        facet_specs = {item["id"]: item for item in facets}
        has_core_requirements = bool(core_requirements)
        # 核心要求是可回答性的唯一语义门槛。检索分面用于组织检索与诊断；
        # 只有旧计划没有核心要求时，required 分面才作为兼容性的阻断条件。
        blocking_missing_facets = (
            []
            if has_core_requirements
            else [
                item_id
                for item_id in missing_facets
                if facet_specs.get(item_id, {}).get("role", "required") == "required"
            ]
        )
        exploratory_missing_facets = [
            item_id for item_id in missing_facets if item_id not in blocking_missing_facets
        ]
        unsupported_requirements = [item for item in requirement_assessments if item["status"] == "unsupported"]
        partial_requirements = [item for item in requirement_assessments if item["status"] == "partial"]
        answerable = (
            not blocking_missing_facets
            and not unsupported_requirements
            and not partial_requirements
        )
        # 兼容旧返回字段，但补偿查询的构造算法由 RetrievalRefiner 独立维护。
        refinement_facets = RetrievalRefiner().refine(
            plan,
            {
                "facetAssessments": facet_assessments,
                "requirementAssessments": requirement_assessments,
                "slotAssessments": slot_assessments,
                "missingFacetIds": missing_facets,
                "blockingMissingFacetIds": blocking_missing_facets,
            },
        )
        return (
            {
                "semanticValidated": True,
                "answerable": answerable,
                "facetAssessments": facet_assessments,
                "requirementAssessments": requirement_assessments,
                "slotAssessments": slot_assessments,
                "optionalAssessments": optional_assessments,
                "missingFacetIds": missing_facets,
                "blockingMissingFacetIds": blocking_missing_facets,
                "exploratoryMissingFacetIds": exploratory_missing_facets,
                "missingRequirementIds": [
                    item["id"] for item in requirement_assessments if item["status"] != "supported"
                ],
                "missingSlotIds": [
                    item["id"] for item in slot_assessments if item["status"] != "supported"
                ],
                "facetCoverage": round(
                    sum(item["status"] == "supported" for item in facet_assessments)
                    / max(1, len(facet_assessments)),
                    4,
                ),
                "refinementFacets": refinement_facets,
            },
            str(raw_response or ""),
        )

    @staticmethod
    def _aggregate_requirements(
        requirements: list[dict[str, Any]],
        slot_assessments: list[dict[str, Any]],
        *,
        evidence_metadata: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        """从原子槽位支持度聚合父要求，并执行独立来源和时间跨度硬约束。"""
        assessments_by_id = {
            str(item.get("id") or ""): item
            for item in slot_assessments
            if isinstance(item, dict)
        }
        result: list[dict[str, Any]] = []
        for requirement in requirements:
            slots = [
                assessments_by_id.get(str(item.get("id") or ""))
                for item in requirement.get("coverageSlots") or []
            ]
            slots = [item for item in slots if item is not None]
            supporting_refs = list(
                dict.fromkeys(
                    reference
                    for item in slots
                    for reference in item.get("supportingRefs") or []
                )
            )
            sources = {
                evidence_metadata.get(reference, {}).get("recordId", "")
                for reference in supporting_refs
                if evidence_metadata.get(reference, {}).get("recordId")
            }
            periods = {
                evidence_metadata.get(reference, {}).get("year", "")
                for reference in supporting_refs
                if evidence_metadata.get(reference, {}).get("year")
            }
            periods.update(
                str(item.get("year") or item.get("timelineRole") or "").strip()
                for item in slots
                if item.get("status") == "supported"
                and str(item.get("year") or item.get("timelineRole") or "").strip()
            )
            missing_slots = [
                item for item in slots if item.get("status") != "supported"
            ]
            sources_sufficient = len(sources) >= int(
                requirement.get("minimumDistinctSources") or 1
            )
            periods_sufficient = len(periods) >= int(
                requirement.get("minimumDistinctPeriods") or 0
            )
            if not missing_slots and sources_sufficient and periods_sufficient:
                status = "supported"
                missing_detail = ""
                refinement_query = ""
            else:
                status = (
                    "partial"
                    if supporting_refs
                    or any(item.get("status") == "partial" for item in slots)
                    else "unsupported"
                )
                details: list[str] = []
                if missing_slots:
                    details.append(
                        "缺少原子槽位："
                        + "、".join(str(item.get("id") or "") for item in missing_slots)
                    )
                if not sources_sufficient:
                    details.append(
                        f"独立来源仅 {len(sources)} 个，要求 "
                        f"{int(requirement.get('minimumDistinctSources') or 1)} 个"
                    )
                if not periods_sufficient:
                    details.append(
                        f"独立时间节点仅 {len(periods)} 个，要求 "
                        f"{int(requirement.get('minimumDistinctPeriods') or 0)} 个"
                    )
                missing_detail = "；".join(details)
                refinement_query = next(
                    (
                        str(item.get("refinementQuery") or "")
                        for item in missing_slots
                        if str(item.get("refinementQuery") or "")
                    ),
                    str(requirement.get("description") or ""),
                )
            result.append(
                {
                    "id": str(requirement.get("id") or ""),
                    "status": status,
                    "supportingRefs": supporting_refs,
                    "missingDetail": missing_detail,
                    "refinementQuery": refinement_query,
                    "coveredSlotIds": [
                        str(item.get("id") or "")
                        for item in slots
                        if item.get("status") == "supported"
                    ],
                    "missingSlotIds": [
                        str(item.get("id") or "")
                        for item in missing_slots
                    ],
                    "distinctSourceCount": len(sources),
                    "distinctPeriodCount": len(periods),
                }
            )
        return result

    @staticmethod
    def _normalize_assessments(
        values: Any,
        *,
        allowed_ids: set[str],
        known_refs: set[str],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id not in allowed_ids or item_id in seen:
                continue
            seen.add(item_id)
            status = str(item.get("status") or "unsupported").strip().lower()
            if status not in {"supported", "partial", "unsupported"}:
                status = "unsupported"
            supporting_refs = [
                str(value)
                for value in item.get("supporting_refs") or item.get("supportingRefs") or []
                if str(value) in known_refs
            ]
            if status == "supported" and not supporting_refs:
                status = "unsupported"
            normalized.append(
                {
                    "id": item_id,
                    "status": status,
                    "supportingRefs": list(dict.fromkeys(supporting_refs)),
                    "missingDetail": str(item.get("missing_detail") or item.get("missingDetail") or "")[:1000],
                    "refinementQuery": str(item.get("refinement_query") or item.get("refinementQuery") or "")[:1600],
                    "timelineRole": str(
                        item.get("timeline_role") or item.get("timelineRole") or ""
                    )[:80],
                    "year": str(item.get("year") or "")[:20],
                    "claims": [
                        str(value)[:500]
                        for value in item.get("claims") or []
                        if str(value).strip()
                    ][:8],
                    "entities": (
                        {
                            str(key)[:80]: (
                                [str(value)[:200] for value in raw_value[:12]]
                                if isinstance(raw_value, list)
                                else str(raw_value)[:500]
                            )
                            for key, raw_value in item.get("entities", {}).items()
                        }
                        if isinstance(item.get("entities"), dict)
                        else {}
                    ),
                }
            )
        return normalized

    @staticmethod
    def _parse_semantic_response(raw_response: str) -> dict[str, Any]:
        text = str(raw_response or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else text
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("模型未返回有效的证据覆盖验证结果")
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError as error:
                raise ValueError("模型未返回有效的证据覆盖验证结果") from error
        if not isinstance(payload, dict):
            raise ValueError("模型返回的证据覆盖验证结构无效")
        return payload

    def refinement_facets(
        self,
        plan: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """兼容旧入口；补偿策略由独立 RetrievalRefiner 维护。"""
        return RetrievalRefiner().refine(plan, evaluation)


__all__ = ["EvidenceEvaluator"]
