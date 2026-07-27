"""根据核心证据缺口与真实文档能力决定是否请求补充材料。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MaterialRequestDecision:
    """回答、有限回答或请求材料的稳定策略结果。"""

    action: str
    reason_codes: list[str] = field(default_factory=list)
    required_materials: list[dict[str, Any]] = field(default_factory=list)


class MaterialRequestPolicy:
    """只根据结构化缺口决策，不检查用户问题中的领域词语。"""

    def decide(
        self,
        *,
        plan: dict[str, Any],
        evaluation: dict[str, Any],
        diagnostics: dict[str, Any],
        required_paper_ids: list[str] | None = None,
    ) -> MaterialRequestDecision:
        requirement_specs = {
            str(item.get("id") or ""): item
            for item in plan.get("requirementSpecs") or []
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        missing_assessments = [
            item
            for item in evaluation.get("requirementAssessments") or []
            if isinstance(item, dict) and item.get("status") != "supported"
        ]
        unsupported = [item for item in missing_assessments if item.get("status") == "unsupported"]
        partial = [item for item in missing_assessments if item.get("status") == "partial"]
        evidence_count = int(diagnostics.get("evidenceCount") or 0)
        relevant_full_text_available = bool(
            diagnostics.get("relevantFullTextAvailable")
            if "relevantFullTextAvailable" in diagnostics
            else diagnostics.get("fullTextAvailable")
        )
        full_text_ids = {
            str(value)
            for value in diagnostics.get("fullTextPaperIds") or []
            if str(value)
        }
        missing_required_papers = (
            [
                str(paper_id)
                for paper_id in required_paper_ids or []
                if str(paper_id) and str(paper_id) not in full_text_ids
            ]
            if "fullTextPaperIds" in diagnostics
            else []
        )

        if missing_required_papers:
            return MaterialRequestDecision(
                action="request_materials",
                reason_codes=["required_scope_full_text_missing"],
                required_materials=[
                    {
                        "type": "document_capability",
                        "paperIds": missing_required_papers,
                        "requiredCapability": "parsed_full_text",
                        "description": "请补充或解析指定文献的全文后重试。",
                    }
                ],
            )

        if bool(evaluation.get("answerable")):
            return MaterialRequestDecision(action="answer")

        if evidence_count > 0 and relevant_full_text_available:
            return MaterialRequestDecision(
                action="bounded_answer",
                reason_codes=[
                    *([] if not partial else ["partial_core_requirements"]),
                    *([] if not unsupported else ["unsupported_core_requirements"]),
                ],
            )

        materials: list[dict[str, Any]] = []
        for assessment in missing_assessments:
            requirement_id = str(assessment.get("id") or "")
            spec = requirement_specs.get(requirement_id, {})
            description = str(
                spec.get("description")
                or assessment.get("missingDetail")
                or requirement_id
                or "核心回答要求"
            )
            materials.append(
                {
                    "type": "core_requirement",
                    "requirementId": requirement_id,
                    "requiredCapability": "parsed_full_text",
                    "preferredEvidenceSections": list(spec.get("preferredSectionTypes") or []),
                    "description": f"请补充能够直接支持“{description[:500]}”的可解析全文材料。",
                }
            )
        if not materials:
            materials.append(
                {
                    "type": "knowledge_base",
                    "requiredCapability": "parsed_full_text",
                    "description": "当前相关范围没有可用于回答核心要求的解析全文，请补充或解析相关文献。",
                }
            )
        return MaterialRequestDecision(
            action="request_materials",
            reason_codes=["no_relevant_full_text_evidence"],
            required_materials=materials,
        )


__all__ = ["MaterialRequestDecision", "MaterialRequestPolicy"]
