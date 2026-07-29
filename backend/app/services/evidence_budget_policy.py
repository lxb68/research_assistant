"""根据回答合同和上下文容量计算证据预算，不解释领域词或检索内容。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.services.retrieval_contracts import (
    flatten_requirement_slots,
    normalize_requirement,
)


@dataclass(slots=True, frozen=True)
class EvidenceBudget:
    """候选检索与最终证据选择共享的运行时预算。"""

    minimum_groups: int
    target_groups: int
    maximum_groups: int
    maximum_context_chars: int
    required_direct_evidence: dict[str, int]
    slot_parent_requirements: dict[str, str]
    required_distinct_sources: dict[str, int]
    required_distinct_periods: dict[str, int]


class EvidenceBudgetPolicy:
    """只根据结构化回答要求分配预算，避免固定领域或论文规则。"""

    def resolve(
        self,
        *,
        question_type: str,
        requirement_specs: list[dict[str, Any]] | None,
        requested_target: int | None,
        maximum_context_chars: int,
        maximum_groups: int | None = None,
    ) -> EvidenceBudget:
        requirements = [
            item
            for index, value in enumerate(requirement_specs or [], 1)
            if (
                item := normalize_requirement(
                    value,
                    index,
                    question_type=question_type,
                )
            )
            is not None
            and item.get("required")
        ]
        slots = flatten_requirement_slots(requirements)
        required_direct_evidence = {
            str(item["id"]): int(item["minimumDirectEvidence"])
            for item in slots
        }
        slot_parent_requirements = {
            str(item["id"]): str(item["parentRequirementId"])
            for item in slots
        }
        required_distinct_sources = {
            str(item["id"]): int(item.get("minimumDistinctSources") or 1)
            for item in requirements
        }
        required_distinct_periods = {
            str(item["id"]): int(item.get("minimumDistinctPeriods") or 0)
            for item in requirements
        }
        # 求和是候选容量的保守估计，不是最终必须选择的数量；同一证据可以覆盖多个要求。
        coverage_capacity = sum(required_direct_evidence.values())
        minimum_groups = max(1, int(settings.orchestrator_min_evidence))
        target_groups = max(
            minimum_groups,
            int(requested_target or 0),
            coverage_capacity,
        )
        safety_limit = max(
            minimum_groups,
            int(maximum_groups or settings.research_agent_max_evidence_groups),
        )
        return EvidenceBudget(
            minimum_groups=min(minimum_groups, safety_limit),
            target_groups=min(target_groups, safety_limit),
            maximum_groups=safety_limit,
            maximum_context_chars=max(1000, int(maximum_context_chars)),
            required_direct_evidence=required_direct_evidence,
            slot_parent_requirements=slot_parent_requirements,
            required_distinct_sources=required_distinct_sources,
            required_distinct_periods=required_distinct_periods,
        )


__all__ = ["EvidenceBudget", "EvidenceBudgetPolicy"]
