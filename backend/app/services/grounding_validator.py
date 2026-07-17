"""验证最终回答的声明引用是否满足证据约束。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GroundingValidationResult:
    valid: bool
    reasons: list[str] = field(default_factory=list)
    cited_indices: set[int] = field(default_factory=set)


class GroundingValidator:
    """只校验生成文本与允许引用集合，不负责重写答案。"""

    FULL_TEXT_CONFLICT_PATTERNS = (
        re.compile(r"(?:只能|仅能|只能够).{0,20}(?:摘要|abstract)", re.IGNORECASE),
        re.compile(r"(?:无法|不能|未能).{0,20}(?:获取|访问|读取|检索).{0,12}(?:全文|full[ -]?text)", re.IGNORECASE),
        re.compile(r"(?:全文|full[ -]?text).{0,12}(?:不可用|不存在|unavailable|not available)", re.IGNORECASE),
    )

    def validate(self, answer: str, *, source_count: int, retrieval_state: dict[str, Any]) -> GroundingValidationResult:
        cited = self.extract_citation_indices(answer, source_count)
        reasons: list[str] = []
        if source_count > 0 and not cited:
            reasons.append("回答没有引用任何已提供证据")
        if bool(retrieval_state.get("fullTextAvailable")) and any(pattern.search(answer) for pattern in self.FULL_TEXT_CONFLICT_PATTERNS):
            reasons.append("回答声称全文不可用，但检索状态显示全文已经解析")
        for group in retrieval_state.get("requiredCitationGroups") or []:
            valid_group = {int(value) for value in group if str(value).isdigit() and 1 <= int(value) <= source_count}
            if valid_group and not (valid_group & cited):
                reasons.append(f"回答未引用核心要求对应的直接证据 {sorted(valid_group)}")
        return GroundingValidationResult(valid=not reasons, reasons=reasons, cited_indices=cited)

    @staticmethod
    def extract_citation_indices(answer: str, source_count: int) -> set[int]:
        indices: set[int] = set()
        for content in re.findall(r"\[([0-9,，\-–—\s]+)\]", answer):
            for part in content.replace("，", ",").replace("–", "-").replace("—", "-").split(","):
                value = part.strip()
                if "-" in value:
                    bounds = [item.strip() for item in value.split("-", 1)]
                    if len(bounds) == 2 and all(item.isdigit() for item in bounds):
                        start, end = map(int, bounds)
                        if start <= end and end - start <= 20:
                            indices.update(range(start, end + 1))
                elif value.isdigit():
                    indices.add(int(value))
        return {index for index in indices if 1 <= index <= source_count}


__all__ = ["GroundingValidationResult", "GroundingValidator"]
