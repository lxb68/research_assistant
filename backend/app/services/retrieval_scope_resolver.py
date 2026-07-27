"""校验结构化检索范围契约，隔离历史引用与项目检索范围。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RetrievalScopeResolution:
    """问题契约可安全采用的检索目标及诊断信息。"""

    target_paper_ids: list[str] = field(default_factory=list)
    invalid_target_ids: list[str] = field(default_factory=list)
    scope_mode: str = "unscoped"
    expanded_from_history: bool = False


class RetrievalScopeResolver:
    """只校验规划器声明的范围及其依据，不解释具体领域词语。"""

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())

    def resolve(
        self,
        *,
        question: str,
        requested_scope_mode: Any,
        interaction_mode: str,
        reference_basis: list[Any],
        proposed_target_ids: list[Any],
        explicit_paper_ids: list[str],
        candidate_sources: list[dict[str, Any]],
    ) -> RetrievalScopeResolution:
        """按显式选择、全库意图、历史指代的优先级解析文献范围。"""
        explicit_ids = list(dict.fromkeys(
            str(value).strip()
            for value in explicit_paper_ids
            if str(value).strip()
        ))
        known_ids = {
            str(source.get("record_id") or "").strip()
            for source in candidate_sources
            if str(source.get("record_id") or "").strip()
        }
        allowed_ids = known_ids | set(explicit_ids)
        proposed_ids = list(dict.fromkeys(
            str(value).strip()
            for value in proposed_target_ids
            if str(value).strip()
        ))
        normalized_question = self._normalize_text(question)
        proposed_basis = list(dict.fromkeys(
            str(value).strip()
            for value in reference_basis
            if str(value).strip()
        ))
        valid_basis = [
            value
            for value in proposed_basis
            if self._normalize_text(value) in normalized_question
        ]

        # UI 或调用方显式传入的论文选择具有最高优先级。
        if explicit_ids:
            return RetrievalScopeResolution(
                target_paper_ids=explicit_ids,
                scope_mode="explicit",
            )

        scope_mode = str(requested_scope_mode or "").strip().casefold()
        # 历史来源只有在规划器声明 referenced 且能指出当前问题中的原文依据时，
        # 才能成为硬过滤条件。缺少依据或旧版本规划结果一律安全回退到项目范围。
        if scope_mode != "referenced" or interaction_mode not in {"reference", "correction"} or not valid_basis:
            return RetrievalScopeResolution(
                scope_mode="corpus",
                expanded_from_history=bool(proposed_ids),
            )

        invalid_ids = [value for value in proposed_ids if value not in allowed_ids]
        target_ids = [value for value in proposed_ids if value in allowed_ids]
        return RetrievalScopeResolution(
            target_paper_ids=target_ids,
            invalid_target_ids=invalid_ids,
            scope_mode="referenced" if target_ids else "unscoped",
        )


__all__ = ["RetrievalScopeResolution", "RetrievalScopeResolver"]
