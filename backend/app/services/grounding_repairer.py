"""对引用校验失败的局部强声明执行受限修订。"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.core.config import settings
from app.prompt_loader import load_prompt
from app.services.grounding_validator import GroundingValidator
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT_ZH


class GroundingRepairer:
    """只修订可精确定位的句子；无法安全修订时返回原答案。"""

    PROMPT = load_prompt("research/claim_repair.zh.md")

    def __init__(self, *, completion: Callable[..., str]) -> None:
        self._completion = completion

    def repair_strong_claims(
        self,
        answer: str,
        *,
        evidence_context: str,
        source_count: int,
        model: dict[str, Any],
        timeout: int,
    ) -> str:
        sentences = GroundingValidator.uncited_strong_claim_sentences(
            answer,
            source_count,
        )
        if not sentences:
            return answer
        raw = self._completion(
            model,
            [
                {
                    "role": "system",
                    "content": f"{self.PROMPT}\n\n{SYSTEM_SECURITY_CONSTRAINT_ZH}",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "sentences": sentences,
                            "evidence": evidence_context,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            timeout=timeout,
            response_format={"type": "json_object"},
            max_output_tokens=min(
                settings.research_semantic_max_output_tokens,
                max(768, len(sentences) * 512),
            ),
            thinking=False,
        )
        payload = json.loads(str(raw or "").strip())
        repairs = payload.get("repairs") if isinstance(payload, dict) else []
        repaired = answer
        expected = set(sentences)
        replaced: set[str] = set()
        for item in repairs or []:
            if not isinstance(item, dict):
                continue
            original = str(item.get("original") or "")
            replacement = str(item.get("replacement") or "").strip()
            if original not in expected or not replacement:
                continue
            repaired = repaired.replace(original, replacement, 1)
            replaced.add(original)
        return repaired if replaced == expected else answer


__all__ = ["GroundingRepairer"]
