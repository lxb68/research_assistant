"""校验检索 facet 与核心回答要求之间的结构化绑定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class FacetBindingResult:
    """绑定后的 facet 及无效 requirement 引用。"""

    facets: list[dict[str, Any]] = field(default_factory=list)
    invalid_requirement_ids: dict[str, list[str]] = field(default_factory=dict)


class FacetRequirementBinder:
    """只处理 facet 的必需性和 requirement 归属，不解释查询文本。"""

    def bind(
        self,
        facets: list[dict[str, Any]],
        requirements: list[dict[str, Any]],
    ) -> FacetBindingResult:
        known_requirement_ids = {
            str(item.get("id") or "")
            for item in requirements
            if str(item.get("id") or "")
        }
        has_requirements = bool(known_requirement_ids)
        bound: list[dict[str, Any]] = []
        invalid: dict[str, list[str]] = {}

        for raw_facet in facets:
            facet = dict(raw_facet)
            facet_id = str(facet.get("id") or "")
            proposed_ids = list(dict.fromkeys(
                str(value).strip()
                for value in facet.get("requirementIds") or []
                if str(value).strip()
            ))
            valid_ids = [value for value in proposed_ids if value in known_requirement_ids]
            invalid_ids = [value for value in proposed_ids if value not in known_requirement_ids]
            if invalid_ids:
                invalid[facet_id] = invalid_ids

            # 存在核心要求时，只有完成有效绑定的 facet 才能成为阻塞项。
            # 没有核心要求的旧计划保留原行为，所有 facet 都是回答所需的检索方向。
            role = "required" if valid_ids or not has_requirements else "exploratory"
            facet["role"] = role
            facet["requirementIds"] = valid_ids
            bound.append(facet)

        return FacetBindingResult(
            facets=bound,
            invalid_requirement_ids=invalid,
        )


__all__ = ["FacetBindingResult", "FacetRequirementBinder"]
