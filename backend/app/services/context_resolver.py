"""将对话历史解析为只包含指代对象与候选来源的上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.conversation_context import ConversationContext, ConversationContextProjector


@dataclass(frozen=True, slots=True)
class ResolvedContext:
    """供问题契约构建使用的只读上下文，不承载事实判断。"""

    question: str
    conversation: ConversationContext

    @property
    def candidate_sources(self) -> list[dict[str, Any]]:
        return list(self.conversation.reference_sources)

    def for_planning(self) -> dict[str, Any]:
        return {
            "current_question": self.question,
            **self.conversation.for_query_planning(),
        }


class ContextResolver:
    """只负责规范历史、识别指代使用方式并暴露可引用对象。"""

    def __init__(self, projector: ConversationContextProjector | None = None) -> None:
        self._projector = projector or ConversationContextProjector()

    def resolve(
        self,
        question: str,
        history: list[dict[str, Any]] | None,
    ) -> ResolvedContext:
        normalized_question = str(question or "").strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")
        return ResolvedContext(
            question=normalized_question,
            conversation=self._projector.project(normalized_question, history),
        )


__all__ = ["ContextResolver", "ResolvedContext"]
