"""校验模型生成的对话语义分类，不在代码中维护意图关键词。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SemanticContextClassification:
    """当前问题相对历史对话的结构化语义类别。"""

    mode: str
    basis: list[str] = field(default_factory=list)
    invalid_basis: list[str] = field(default_factory=list)
    repaired: bool = False


class SemanticContextContractBuilder:
    """只验证分类枚举和原文依据，不推测用户使用了哪些表达。"""

    ALLOWED_MODES = {"new_topic", "followup", "reference", "correction", "transform"}
    EVIDENCE_REQUIRED_MODES = {"reference", "correction", "transform"}

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())

    def build(
        self,
        payload: Any,
        *,
        question: str,
        has_history: bool,
    ) -> SemanticContextClassification:
        item = payload if isinstance(payload, dict) else {}
        proposed_mode = str(item.get("mode") or "").strip().casefold()
        raw_basis = item.get("basis")
        proposed_basis = list(dict.fromkeys(
            str(value).strip()
            for value in (raw_basis if isinstance(raw_basis, list) else [])
            if str(value).strip()
        ))
        normalized_question = self._normalize_text(question)
        valid_basis = [
            value
            for value in proposed_basis
            if self._normalize_text(value) in normalized_question
        ]
        invalid_basis = [value for value in proposed_basis if value not in valid_basis]

        fallback_mode = "followup" if has_history else "new_topic"
        repaired = proposed_mode not in self.ALLOWED_MODES
        mode = proposed_mode if proposed_mode in self.ALLOWED_MODES else fallback_mode
        if mode == "followup" and not has_history:
            mode = "new_topic"
            repaired = True
        if mode in self.EVIDENCE_REQUIRED_MODES and (not has_history or not valid_basis):
            mode = fallback_mode
            repaired = True
        if mode not in self.EVIDENCE_REQUIRED_MODES:
            valid_basis = []

        return SemanticContextClassification(
            mode=mode,
            basis=valid_basis,
            invalid_basis=invalid_basis,
            repaired=repaired,
        )


__all__ = ["SemanticContextClassification", "SemanticContextContractBuilder"]
