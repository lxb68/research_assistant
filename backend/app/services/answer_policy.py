"""集中控制研究回答的深度、证据边界和输出风格。"""

from __future__ import annotations

import json
from typing import Any

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
        revision_instruction: str = "",
    ) -> str:
        prompt = base_prompt.replace("{{evidence}}", evidence_context)
        requirements = [str(item).strip()[:500] for item in answer_requirements if str(item).strip()]
        if requirements:
            prompt += "\n\n# 本次回答的核心覆盖目标\n" + "\n".join(f"- {item}" for item in requirements)
            prompt += "\n这些目标用于组织回答；非核心细节缺失时应说明边界，不能否定已有证据支持的结论。"
        normalized_state = {
            "fullTextAvailable": bool(retrieval_state.get("fullTextAvailable")),
            "evidenceSufficient": bool(retrieval_state.get("evidenceSufficient")),
            "evidenceCount": int(retrieval_state.get("evidenceCount") or 0),
            "candidateCount": int(retrieval_state.get("candidateCount") or 0),
            "missingFacetIds": list(retrieval_state.get("missingFacetIds") or [])[:12],
            "missingRequirementIds": list(retrieval_state.get("missingRequirementIds") or [])[:12],
            "sectionMetadataDegraded": bool(retrieval_state.get("sectionMetadataDegraded")),
            "requirementClaims": list(retrieval_state.get("requirementClaims") or [])[:12],
        }
        prompt += "\n\n# 当前检索状态\n" + json.dumps(normalized_state, ensure_ascii=False)
        prompt += "\n该状态优先于历史判断。证据片段未覆盖某项细节，不代表全文不存在。"
        if revision_instruction:
            prompt += f"\n\n# 修订要求\n{revision_instruction}"
        return f"{prompt}\n\n{SYSTEM_SECURITY_CONSTRAINT}"


__all__ = ["AnswerPolicy"]
