"""根据证据包和回答策略生成用户答案。"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.services.answer_policy import AnswerPolicy


class AnswerComposer:
    """只执行答案生成；策略编译和证据验证由独立组件负责。"""

    def __init__(self, *, completion: Callable[..., str], policy: AnswerPolicy | None = None) -> None:
        self._completion = completion
        self._policy = policy or AnswerPolicy()

    def compose(
        self,
        *,
        model: dict[str, Any],
        base_prompt: str,
        evidence_context: str,
        question: str,
        resolved_question: str,
        answer_requirements: list[str],
        retrieval_state: dict[str, Any],
        timeout: int,
        revision_instruction: str = "",
    ) -> str:
        prompt = self._policy.build_prompt(
            base_prompt=base_prompt,
            evidence_context=evidence_context,
            answer_requirements=answer_requirements,
            retrieval_state=retrieval_state,
            revision_instruction=revision_instruction,
        )
        return self._completion(
            model,
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"currentQuestion": question, "resolvedResearchQuestion": resolved_question}, ensure_ascii=False)},
            ],
            temperature=0.2,
            timeout=timeout,
        )


__all__ = ["AnswerComposer"]
