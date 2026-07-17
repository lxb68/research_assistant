"""根据证据评估缺口生成有界的补偿检索任务。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings


class RetrievalRefiner:
    """只把缺口报告转换为下一轮检索分面，不执行检索或评估。"""

    def refine(self, plan: dict[str, Any], evaluation: dict[str, Any]) -> list[dict[str, Any]]:
        semantic = [dict(item) for item in evaluation.get("refinementFacets") or [] if isinstance(item, dict)]
        if semantic:
            return semantic[: settings.query_planner_max_facets]

        facets = [dict(item) for item in plan.get("retrievalFacets") or [] if isinstance(item, dict)]
        assessments = [
            *[item for item in evaluation.get("facetAssessments") or [] if isinstance(item, dict)],
            *[item for item in evaluation.get("requirementAssessments") or [] if isinstance(item, dict)],
        ]
        generated = self._from_assessments(plan, assessments)
        if generated:
            return generated[: settings.query_planner_max_facets]

        missing_ids = {str(value) for value in evaluation.get("missingFacetIds") or [] if str(value)}
        if missing_ids:
            return [item for item in facets if str(item.get("id") or "") in missing_ids]
        if (
            str(plan.get("questionType") or "") == "mechanism"
            and int(evaluation.get("methodEvidenceCount") or 0) < settings.orchestrator_min_method_evidence
        ):
            return [
                {
                    "id": f"refine-method-{index}",
                    "goal": str(item.get("goal") or "补充方法与协议证据"),
                    "query": f"{str(item.get('query') or '')}\n{str(item.get('goal') or '')}\nmethod framework protocol algorithm implementation",
                    "preferredSectionTypes": ["method", "framework", "protocol", "algorithm", "implementation", "overview"],
                }
                for index, item in enumerate(facets[: settings.query_planner_max_facets], 1)
                if str(item.get("query") or "").strip()
            ]
        return []

    @staticmethod
    def _from_assessments(plan: dict[str, Any], assessments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        original_facets = {str(item.get("id") or ""): item for item in plan.get("retrievalFacets") or [] if isinstance(item, dict)}
        requirements = {str(item.get("id") or ""): item for item in plan.get("requirementSpecs") or [] if isinstance(item, dict)}
        result: list[dict[str, Any]] = []
        for assessment in assessments:
            if assessment.get("status") == "supported":
                continue
            item_id = str(assessment.get("id") or "")
            original = original_facets.get(item_id)
            requirement = requirements.get(item_id)
            source = original or requirement or {}
            query = str(assessment.get("refinementQuery") or source.get("query") or source.get("description") or "").strip()
            if not query:
                continue
            result.append({
                "id": item_id if original else f"requirement-{item_id}",
                "goal": str(source.get("goal") or source.get("description") or assessment.get("missingDetail") or query),
                "query": query,
                "evidenceIntent": str(source.get("evidenceIntent") or "fact"),
                "preferredSectionTypes": list(source.get("preferredSectionTypes") or []),
            })
        return result


__all__ = ["RetrievalRefiner"]
