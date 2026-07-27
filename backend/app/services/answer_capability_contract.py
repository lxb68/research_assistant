"""约束回答所需证据能力，并决定编排循环能否安全结束。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CAPABILITY_RANK = {
    "none": 0,
    "metadata": 1,
    "content_excerpt": 2,
    "semantic_validation": 3,
}
ANSWER_MODES = {"conversation", "catalog", "document_summary", "research_synthesis"}


@dataclass(frozen=True, slots=True)
class AnswerCapabilityContract:
    """描述本轮回答类型及其最低证据能力。"""

    mode: str = "conversation"
    required_capability: str = "none"

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "requiredCapability": self.required_capability,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        fallback: AnswerCapabilityContract | None = None,
    ) -> AnswerCapabilityContract:
        previous = fallback or cls()
        if not isinstance(payload, dict):
            return previous
        mode = str(payload.get("mode") or previous.mode).strip().lower()
        capability = str(
            payload.get("requiredCapability")
            or payload.get("required_capability")
            or previous.required_capability
        ).strip().lower()
        if mode not in ANSWER_MODES:
            mode = previous.mode
        if capability not in CAPABILITY_RANK:
            capability = previous.required_capability
        # 后续路由只能维持或提高证据要求，不能在观察后把要求降级。
        if CAPABILITY_RANK[capability] < CAPABILITY_RANK[previous.required_capability]:
            capability = previous.required_capability
        return cls(mode=mode, required_capability=capability)

    def elevate(self, capability: str, *, mode: str | None = None) -> AnswerCapabilityContract:
        normalized = capability if capability in CAPABILITY_RANK else self.required_capability
        required = (
            normalized
            if CAPABILITY_RANK[normalized] > CAPABILITY_RANK[self.required_capability]
            else self.required_capability
        )
        normalized_mode = str(mode or self.mode).strip().lower()
        if normalized_mode not in ANSWER_MODES:
            normalized_mode = self.mode
        return AnswerCapabilityContract(normalized_mode, required)


@dataclass(frozen=True, slots=True)
class FinalizationDecision:
    """终态门禁结果。"""

    allowed: bool
    required_action: str = ""
    reason_code: str = ""
    available_capability: str = "none"


class FinalizationPolicy:
    """只比较结构化能力，不读取或匹配用户问题中的领域词语。"""

    def decide(
        self,
        contract: AnswerCapabilityContract,
        observations: list[dict[str, Any]],
    ) -> FinalizationDecision:
        available = "none"
        requires_semantic_validation = False
        for item in observations:
            if not item.get("ok"):
                continue
            capability = str(item.get("resultCapability") or "none")
            if capability in CAPABILITY_RANK and CAPABILITY_RANK[capability] > CAPABILITY_RANK[available]:
                available = capability
            requires_semantic_validation = (
                requires_semantic_validation
                or bool(item.get("requiresSemanticValidation"))
            )

        required = contract.required_capability
        if requires_semantic_validation:
            required = "semantic_validation"
        if CAPABILITY_RANK[available] >= CAPABILITY_RANK[required]:
            return FinalizationDecision(True, available_capability=available)
        if required == "semantic_validation":
            return FinalizationDecision(
                False,
                required_action="research_chat",
                reason_code="semantic_validation_required",
                available_capability=available,
            )
        return FinalizationDecision(
            False,
            required_action="continue_observation",
            reason_code="insufficient_observation_capability",
            available_capability=available,
        )


__all__ = [
    "AnswerCapabilityContract",
    "FinalizationDecision",
    "FinalizationPolicy",
]
