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
        response_context: str = "",
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
        prompt += "\n\n# 当前检索状态\n" + json.dumps(normalized_state, ensure_ascii=False)
        prompt += (
            "\n该状态优先于历史判断。证据片段未覆盖某项细节，不代表全文不存在。"
            "\n只有 status=supported 且带有 citationIndices 的原子槽位可以写成确定结论；"
            "缺失槽位必须明确作为边界，不得用同一篇近期论文补写完整历史。"
            "\n涉及作者、方法名称、年份、比较对象和数值时必须逐字保持证据中的实体关系；"
            "“首次、最快、全面优于”等强声明只能在对应直接证据明确支持时使用，并应表述为论文作者的声明。"
        )
        if requirements and not normalized_state["semanticValidated"]:
            prompt += (
                "\n本轮核心要求没有完成语义覆盖验证。不得声称已经形成完整脉络、全面分类或完整比较；"
                "只能陈述证据片段直接表达且可逐项引用的局部观察，并明确说明尚未验证的覆盖边界。"
            )
        if response_context.strip():
            prompt += (
                "\n\n# 用户确认的偏好与项目记忆\n"
                "偏好只影响称呼和表达；项目记忆可帮助组织回答，但不能替代检索证据或生成无引用事实。\n"
                + response_context[:6000]
            )
        if revision_instruction:
            prompt += f"\n\n# 修订要求\n{revision_instruction}"
        return f"{prompt}\n\n{SYSTEM_SECURITY_CONSTRAINT}"


__all__ = ["AnswerPolicy"]
