"""在上下文预算内按核心要求覆盖、相关性、来源多样性和冗余选择证据。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.services.evidence_budget_policy import EvidenceBudget
from app.services.evidence_groups import flatten_evidence_groups, group_evidence


@dataclass(slots=True, frozen=True)
class EvidenceSelectionWeights:
    coverage: float = settings.evidence_selection_coverage_weight
    partial: float = settings.evidence_selection_partial_weight
    relevance: float = settings.evidence_selection_relevance_weight
    source_diversity: float = settings.evidence_selection_source_diversity_weight
    temporal_diversity: float = settings.evidence_selection_temporal_diversity_weight
    redundancy: float = settings.evidence_selection_redundancy_weight


@dataclass(slots=True)
class EvidenceSelectionResult:
    evidence: list[dict[str, Any]]
    diagnostics: dict[str, Any]


class CoverageAwareEvidenceSelector:
    """执行可解释的预算化多重覆盖贪心选择。"""

    def __init__(self, *, weights: EvidenceSelectionWeights | None = None) -> None:
        self.weights = weights or EvidenceSelectionWeights()

    def select(
        self,
        evidence: list[dict[str, Any]],
        *,
        budget: EvidenceBudget,
    ) -> EvidenceSelectionResult:
        groups = group_evidence(evidence)
        requirements = dict(budget.required_direct_evidence)
        if not groups:
            return EvidenceSelectionResult([], self._diagnostics([], groups, requirements, budget))

        selected: list[list[dict[str, Any]]] = []
        selected_keys: set[tuple[str, str, str | int]] = set()
        selected_sources: set[str] = set()
        direct_counts = {requirement_id: 0 for requirement_id in requirements}
        parent_sources = {
            requirement_id: set()
            for requirement_id in budget.required_distinct_sources
        }
        parent_periods = {
            requirement_id: set()
            for requirement_id in budget.required_distinct_periods
        }
        context_chars = 0

        # 补偿检索时，已经验证为 direct 的旧证据先锁定，避免下一轮全局排序把它洗掉。
        for group in groups:
            if not self._is_locked(group):
                continue
            if not self._fits(group, context_chars, budget):
                continue
            self._append_group(
                group,
                selected,
                selected_keys,
                selected_sources,
                direct_counts,
                budget.slot_parent_requirements,
                parent_sources,
                parent_periods,
            )
            context_chars += self._char_cost(group)
            if len(selected) >= budget.maximum_groups:
                break

        while len(selected) < budget.maximum_groups:
            all_covered = self._all_covered(
                direct_counts,
                requirements,
                parent_sources,
                parent_periods,
                budget,
            )
            if all_covered and len(selected) >= budget.minimum_groups:
                break
            # target_groups 是根据回答合同估算出的正常容量；maximum_groups 仅是安全上限。
            # 当候选语义信号不足以补齐硬覆盖时，不应继续把安全上限机械填满。
            if requirements and len(selected) >= budget.target_groups:
                break
            best_group: list[dict[str, Any]] | None = None
            best_score = float("-inf")
            for group in groups:
                if self._group_key(group) in selected_keys:
                    continue
                if not self._fits(group, context_chars, budget):
                    continue
                score = self._marginal_score(
                    group,
                    selected,
                    selected_sources,
                    direct_counts,
                    requirements,
                    budget,
                    parent_sources,
                    parent_periods,
                )
                if score > best_score:
                    best_score = score
                    best_group = group
            if best_group is None:
                break
            # 覆盖已经无法增长时，仅补足最小证据量；不能为了达到目标数机械塞入弱证据。
            if best_score <= 0 and len(selected) >= budget.minimum_groups:
                break
            self._append_group(
                best_group,
                selected,
                selected_keys,
                selected_sources,
                direct_counts,
                budget.slot_parent_requirements,
                parent_sources,
                parent_periods,
            )
            context_chars += self._char_cost(best_group)

        # 没有核心要求的兼容路径：按相关性在目标预算内选择。
        if not requirements:
            while len(selected) < budget.target_groups:
                candidates = [
                    group
                    for group in groups
                    if self._group_key(group) not in selected_keys
                    and self._fits(group, context_chars, budget)
                ]
                if not candidates:
                    break
                best_group = max(
                    candidates,
                    key=lambda group: self._marginal_score(
                        group,
                        selected,
                        selected_sources,
                        direct_counts,
                        requirements,
                        budget,
                        parent_sources,
                        parent_periods,
                    ),
                )
                self._append_group(
                    best_group,
                    selected,
                    selected_keys,
                    selected_sources,
                    direct_counts,
                    budget.slot_parent_requirements,
                    parent_sources,
                    parent_periods,
                )
                context_chars += self._char_cost(best_group)

        diagnostics = self._diagnostics(selected, groups, requirements, budget)
        diagnostics["selectedContextChars"] = context_chars
        diagnostics["selectedDirectEvidenceByRequirement"] = dict(direct_counts)
        missing_slots = [
            requirement_id
            for requirement_id, required_count in requirements.items()
            if direct_counts.get(requirement_id, 0) < required_count
        ]
        diversity_missing_parents = {
            requirement_id
            for requirement_id, required_count in budget.required_distinct_sources.items()
            if len(parent_sources.get(requirement_id, set())) < required_count
        } | {
            requirement_id
            for requirement_id, required_count in budget.required_distinct_periods.items()
            if len(parent_periods.get(requirement_id, set())) < required_count
        }
        diagnostics["missingCoverageSlotIds"] = missing_slots
        diagnostics["unsupportedRequirementIds"] = sorted(
            {
                budget.slot_parent_requirements.get(slot_id, slot_id)
                for slot_id in missing_slots
            }
            | diversity_missing_parents
        )
        diagnostics["selectedDistinctSourcesByRequirement"] = {
            requirement_id: len(values)
            for requirement_id, values in parent_sources.items()
        }
        diagnostics["selectedDistinctPeriodsByRequirement"] = {
            requirement_id: len(values)
            for requirement_id, values in parent_periods.items()
        }
        diagnostics["coverageSignalsAvailable"] = any(
            self._support(group) for group in groups
        )
        return EvidenceSelectionResult(flatten_evidence_groups(selected), diagnostics)

    def _marginal_score(
        self,
        group: list[dict[str, Any]],
        selected: list[list[dict[str, Any]]],
        selected_sources: set[str],
        direct_counts: dict[str, int],
        requirements: dict[str, int],
        budget: EvidenceBudget,
        parent_sources: dict[str, set[str]],
        parent_periods: dict[str, set[str]],
    ) -> float:
        support = self._support(group)
        direct_gain = sum(
            1
            for requirement_id, required_count in requirements.items()
            if direct_counts.get(requirement_id, 0) < required_count
            and support.get(requirement_id, {}).get("status") == "direct"
        )
        partial_gain = sum(
            float(assessment.get("confidence") or 0)
            for requirement_id, assessment in support.items()
            if requirement_id in requirements
            and direct_counts.get(requirement_id, 0) < requirements[requirement_id]
            and assessment.get("status") == "partial"
        )
        record_id = str(group[0].get("record_id") or "")
        diversity_source_parents: set[str] = set()
        diversity_period_parents: set[str] = set()
        for slot_id, assessment in support.items():
            if assessment.get("status") != "direct":
                continue
            parent_id = budget.slot_parent_requirements.get(slot_id, slot_id)
            if (
                len(parent_sources.get(parent_id, set()))
                < budget.required_distinct_sources.get(parent_id, 1)
                and record_id
                and record_id not in parent_sources.get(parent_id, set())
            ):
                diversity_source_parents.add(parent_id)
            period = self._period(group, assessment)
            if (
                len(parent_periods.get(parent_id, set()))
                < budget.required_distinct_periods.get(parent_id, 1)
                and period
                and period not in parent_periods.get(parent_id, set())
            ):
                diversity_period_parents.add(parent_id)
        relevance = math.log1p(max(0.0, self._relevance(group)))
        source_gain = 1.0 if record_id and record_id not in selected_sources else 0.0
        redundancy = max(
            (self._similarity(group, existing) for existing in selected),
            default=0.0,
        )
        raw_score = (
            self.weights.coverage * direct_gain
            + self.weights.partial * partial_gain
            + self.weights.relevance * relevance
            + self.weights.source_diversity
            * (source_gain + len(diversity_source_parents))
            + self.weights.temporal_diversity * len(diversity_period_parents)
            - self.weights.redundancy * redundancy
        )
        # 长证据只施加温和成本，避免极短但无信息的表题因分母过小获胜。
        normalized_cost = math.sqrt(max(1.0, self._char_cost(group) / 1000))
        return raw_score / normalized_cost

    @staticmethod
    def _support(group: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        rank = {"unsupported": 0, "partial": 1, "direct": 2}
        for item in group:
            for requirement_id, raw_assessment in (
                item.get("requirement_support") or {}
            ).items():
                assessment = dict(raw_assessment) if isinstance(raw_assessment, dict) else {}
                current = merged.get(str(requirement_id))
                if current is None or rank.get(str(assessment.get("status")), 0) > rank.get(
                    str(current.get("status")), 0
                ):
                    merged[str(requirement_id)] = assessment
                elif current and assessment.get("status") == current.get("status"):
                    current["confidence"] = max(
                        float(current.get("confidence") or 0),
                        float(assessment.get("confidence") or 0),
                    )
        return merged

    @classmethod
    def _is_locked(cls, group: list[dict[str, Any]]) -> bool:
        return any(item.get("selection_locked") for item in group) or any(
            assessment.get("status") == "direct"
            for assessment in cls._support(group).values()
            if any(item.get("existing_evidence") for item in group)
        )

    @classmethod
    def _append_group(
        cls,
        group: list[dict[str, Any]],
        selected: list[list[dict[str, Any]]],
        selected_keys: set[tuple[str, str, str | int]],
        selected_sources: set[str],
        direct_counts: dict[str, int],
        slot_parent_requirements: dict[str, str],
        parent_sources: dict[str, set[str]],
        parent_periods: dict[str, set[str]],
    ) -> None:
        selected.append(group)
        selected_keys.add(cls._group_key(group))
        selected_sources.add(str(group[0].get("record_id") or ""))
        for requirement_id, assessment in cls._support(group).items():
            if requirement_id in direct_counts and assessment.get("status") == "direct":
                direct_counts[requirement_id] += 1
                parent_id = slot_parent_requirements.get(requirement_id, requirement_id)
                record_id = str(group[0].get("record_id") or "")
                if record_id:
                    parent_sources.setdefault(parent_id, set()).add(record_id)
                period = cls._period(group, assessment)
                if period:
                    parent_periods.setdefault(parent_id, set()).add(period)

    @staticmethod
    def _group_key(group: list[dict[str, Any]]) -> tuple[str, str, str | int]:
        item = group[0]
        structure_id = str(item.get("structure_id") or "")
        return (
            str(item.get("record_id") or ""),
            "structure" if structure_id else "chunk",
            structure_id or int(item.get("chunk_index") or 0),
        )

    @staticmethod
    def _char_cost(group: list[dict[str, Any]]) -> int:
        return sum(len(str(item.get("text") or "")) for item in group)

    @classmethod
    def _fits(
        cls,
        group: list[dict[str, Any]],
        context_chars: int,
        budget: EvidenceBudget,
    ) -> bool:
        return context_chars + cls._char_cost(group) <= budget.maximum_context_chars

    @staticmethod
    def _relevance(group: list[dict[str, Any]]) -> float:
        return max(
            float(
                item.get("fusion_score")
                or item.get("hybrid_fusion_score")
                or item.get("score")
                or 0
            )
            for item in group
        )

    @classmethod
    def _similarity(
        cls,
        first: list[dict[str, Any]],
        second: list[dict[str, Any]],
    ) -> float:
        first_tokens = cls._tokens(first)
        second_tokens = cls._tokens(second)
        if not first_tokens or not second_tokens:
            return 0.0
        return len(first_tokens & second_tokens) / len(first_tokens | second_tokens)

    @staticmethod
    def _tokens(group: list[dict[str, Any]]) -> set[str]:
        text = " ".join(str(item.get("text") or "") for item in group).casefold()
        return set(re.findall(r"[\w\-]+", text, re.UNICODE))

    @staticmethod
    def _all_covered(
        direct_counts: dict[str, int],
        requirements: dict[str, int],
        parent_sources: dict[str, set[str]],
        parent_periods: dict[str, set[str]],
        budget: EvidenceBudget,
    ) -> bool:
        return (
            all(
            direct_counts.get(requirement_id, 0) >= required_count
            for requirement_id, required_count in requirements.items()
            )
            and all(
                len(parent_sources.get(requirement_id, set())) >= required_count
                for requirement_id, required_count in budget.required_distinct_sources.items()
            )
            and all(
                len(parent_periods.get(requirement_id, set())) >= required_count
                for requirement_id, required_count in budget.required_distinct_periods.items()
            )
        )

    @staticmethod
    def _period(
        group: list[dict[str, Any]],
        assessment: dict[str, Any],
    ) -> str:
        return str(
            assessment.get("year")
            or group[0].get("year")
            or assessment.get("timelineRole")
            or ""
        ).strip()

    @staticmethod
    def _diagnostics(
        selected: list[list[dict[str, Any]]],
        candidates: list[list[dict[str, Any]]],
        requirements: dict[str, int],
        budget: EvidenceBudget,
    ) -> dict[str, Any]:
        return {
            "selectionStrategy": "coverage_aware_budgeted_greedy",
            "candidateEvidenceGroupCount": len(candidates),
            "selectedEvidenceGroupCount": len(selected),
            "minimumEvidenceGroups": budget.minimum_groups,
            "targetEvidenceGroups": budget.target_groups,
            "maximumEvidenceGroups": budget.maximum_groups,
            "maximumEvidenceContextChars": budget.maximum_context_chars,
            "requiredDirectEvidenceByRequirement": dict(requirements),
            "requiredDistinctSourcesByRequirement": dict(
                budget.required_distinct_sources
            ),
            "requiredDistinctPeriodsByRequirement": dict(
                budget.required_distinct_periods
            ),
        }


__all__ = [
    "CoverageAwareEvidenceSelector",
    "EvidenceSelectionResult",
    "EvidenceSelectionWeights",
]
