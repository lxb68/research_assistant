"""集中控制研究回答的深度、证据边界和输出风格。"""

from __future__ import annotations

import json
from typing import Any

from app.prompt_loader import load_prompt, render_prompt
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT


class AnswerPolicy:
    """将稳定回答规则编译为模型提示词，不负责调用模型。"""

    def build_prompt(
        self,
        *,
        base_prompt: str,
        evidence_context: str,
        answer_requirements: list[str],
        retrieval_state: dict[str, Any],
        response_context: str = "",
        revision_instruction: str = "",
    ) -> str:
        prompt = base_prompt.replace("{{evidence}}", evidence_context)
        requirements = [str(item).strip()[:500] for item in answer_requirements if str(item).strip()]
        if requirements:
            prompt += "\n\n" + render_prompt(
                "research/requirements_section.zh.md",
                requirements="\n".join(f"- {item}" for item in requirements),
            )
        normalized_state = {
            "fullTextAvailable": bool(retrieval_state.get("fullTextAvailable")),
            "evidenceSufficient": bool(retrieval_state.get("evidenceSufficient")),
            "semanticValidated": bool(retrieval_state.get("semanticValidated")),
            "candidateCoverageValidated": bool(
                retrieval_state.get("candidateCoverageValidated")
            ),
            "evidenceCount": int(retrieval_state.get("evidenceCount") or 0),
            "candidateCount": int(retrieval_state.get("candidateCount") or 0),
            "missingFacetIds": list(retrieval_state.get("missingFacetIds") or [])[:12],
            "missingRequirementIds": list(retrieval_state.get("missingRequirementIds") or [])[:12],
            "missingSlotIds": list(retrieval_state.get("missingSlotIds") or [])[:20],
            "sectionMetadataDegraded": bool(retrieval_state.get("sectionMetadataDegraded")),
            "requirementClaims": list(retrieval_state.get("requirementClaims") or [])[:12],
            "slotClaims": list(retrieval_state.get("slotClaims") or [])[:20],
        }
        prompt += "\n\n" + render_prompt(
            "research/retrieval_state_section.zh.md",
            retrieval_state=json.dumps(normalized_state, ensure_ascii=False),
        )
        if requirements and not normalized_state["semanticValidated"]:
            prompt += "\n" + load_prompt("research/unvalidated_notice.zh.md")
        if response_context.strip():
            prompt += "\n\n" + render_prompt(
                "research/memory_context.zh.md",
                context=response_context[:6000],
            )
        if revision_instruction:
            prompt += "\n\n" + render_prompt(
                "research/revision.zh.md",
                instruction=revision_instruction,
            )
        return f"{prompt}\n\n{SYSTEM_SECURITY_CONSTRAINT}"


__all__ = ["AnswerPolicy"]
