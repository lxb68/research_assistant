"""定义研究检索各阶段共享的语义契约与查询编译规则。"""

from __future__ import annotations

import re
from typing import Any


SECTION_TYPES = {
    "abstract",
    "introduction",
    "contribution",
    "background",
    "method",
    "framework",
    "protocol",
    "algorithm",
    "implementation",
    "overview",
    "experiment",
    "result",
    "evaluation",
    "comparison",
    "discussion",
    "conclusion",
}

SECTION_TYPE_ALIASES = {
    "intro": "introduction",
    "related_work": "background",
    "related work": "background",
    "methods": "method",
    "methodology": "method",
    "architecture": "framework",
    "experiments": "experiment",
    "results": "result",
    "contributions": "contribution",
}

EVIDENCE_INTENTS = {
    "fact",
    "mechanism",
    "comparison",
    "evaluation",
    "synthesis",
}

REQUIREMENT_KINDS = {
    "point",
    "chronology",
    "comparison",
    "catalog",
    "mechanism",
    "evaluation",
    "synthesis",
}

QUESTION_EVIDENCE_INTENT = {
    "simple_fact": "fact",
    "mechanism": "mechanism",
    "comparison": "comparison",
    "evaluation": "evaluation",
    "synthesis": "synthesis",
}

SEMANTIC_QUESTION_TYPES = {"mechanism", "comparison", "evaluation", "synthesis"}
BOOLEAN_OPERATOR = re.compile(r"(?i)(?<![\w-])(?:AND|OR|NOT)(?![\w-])")
TOKEN = re.compile(r"[\w\-]+", re.UNICODE)


def normalize_section_types(values: Any) -> list[str]:
    """把模型返回的章节名称归一为稳定的跨模块枚举。"""
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        section_type = str(value or "").strip().casefold().replace("-", "_")
        section_type = SECTION_TYPE_ALIASES.get(section_type, section_type)
        if section_type in SECTION_TYPES and section_type not in normalized:
            normalized.append(section_type)
    return normalized


def normalize_requirement(value: Any, index: int, *, question_type: str) -> dict[str, Any] | None:
    """兼容旧字符串要求，并生成可验证、可用于补检索的结构化要求。"""
    item = value if isinstance(value, dict) else {"description": value}
    description = str(
        item.get("description") or item.get("requirement") or item.get("goal") or ""
    ).strip()
    if not description:
        return None
    evidence_intent = str(
        item.get("evidence_intent")
        or item.get("evidenceIntent")
        or QUESTION_EVIDENCE_INTENT.get(question_type, "fact")
    ).strip().casefold()
    if evidence_intent not in EVIDENCE_INTENTS:
        evidence_intent = QUESTION_EVIDENCE_INTENT.get(question_type, "fact")
    try:
        minimum_direct_evidence = int(
            item.get("minimum_direct_evidence") or item.get("minimumDirectEvidence") or 1
        )
    except (TypeError, ValueError):
        minimum_direct_evidence = 1
    requirement_id = str(item.get("id") or f"req-{index}")[:80]
    kind = str(
        item.get("kind")
        or item.get("requirement_kind")
        or item.get("requirementKind")
        or "point"
    ).strip().casefold()
    if kind not in REQUIREMENT_KINDS:
        kind = "point"
    preferred_section_types = normalize_section_types(
        item.get("preferred_section_types") or item.get("preferredSectionTypes") or []
    )
    coverage_slots: list[dict[str, Any]] = []
    raw_slots = item.get("coverage_slots") or item.get("coverageSlots") or []
    for slot_index, raw_slot in enumerate(
        (raw_slots if isinstance(raw_slots, list) else [])[:6],
        1,
    ):
        if not isinstance(raw_slot, dict):
            continue
        slot_description = str(
            raw_slot.get("description")
            or raw_slot.get("goal")
            or raw_slot.get("role")
            or ""
        ).strip()
        if not slot_description:
            continue
        try:
            slot_minimum = int(
                raw_slot.get("minimum_direct_evidence")
                or raw_slot.get("minimumDirectEvidence")
                or 1
            )
        except (TypeError, ValueError):
            slot_minimum = 1
        coverage_slots.append(
            {
                "id": str(
                    raw_slot.get("id") or f"{requirement_id}-slot-{slot_index}"
                )[:100],
                "parentRequirementId": requirement_id,
                "description": slot_description[:800],
                "role": str(raw_slot.get("role") or "evidence")[:80],
                "minimumDirectEvidence": max(1, min(slot_minimum, 4)),
                "preferredSectionTypes": normalize_section_types(
                    raw_slot.get("preferred_section_types")
                    or raw_slot.get("preferredSectionTypes")
                    or preferred_section_types
                ),
                "queryHint": str(
                    raw_slot.get("query_hint") or raw_slot.get("queryHint") or ""
                )[:1000],
            }
        )
    if not coverage_slots:
        coverage_slots = [
            {
                "id": requirement_id,
                "parentRequirementId": requirement_id,
                "description": description[:800],
                "role": "evidence",
                "minimumDirectEvidence": max(1, min(minimum_direct_evidence, 4)),
                "preferredSectionTypes": preferred_section_types,
                "queryHint": "",
            }
        ]
    try:
        minimum_distinct_sources = int(
            item.get("minimum_distinct_sources")
            or item.get("minimumDistinctSources")
            or (min(2, len(coverage_slots)) if kind == "chronology" else 1)
        )
    except (TypeError, ValueError):
        minimum_distinct_sources = 1
    try:
        minimum_distinct_periods = int(
            item.get("minimum_distinct_periods")
            or item.get("minimumDistinctPeriods")
            or (min(2, len(coverage_slots)) if kind == "chronology" else 0)
        )
    except (TypeError, ValueError):
        minimum_distinct_periods = 0
    return {
        "id": requirement_id,
        "description": description[:800],
        "kind": kind,
        "evidenceIntent": evidence_intent,
        "preferredSectionTypes": preferred_section_types,
        "minimumDirectEvidence": max(1, min(minimum_direct_evidence, 4)),
        "coverageSlots": coverage_slots,
        "minimumDistinctSources": max(
            1,
            min(minimum_distinct_sources, len(coverage_slots), 6),
        ),
        "minimumDistinctPeriods": max(
            0,
            min(minimum_distinct_periods, len(coverage_slots), 6),
        ),
        "required": item.get("required") is not False,
    }


def flatten_requirement_slots(
    requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把父级回答要求展开为可独立验证和补偿检索的原子覆盖槽位。"""
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for requirement in requirements:
        parent_id = str(requirement.get("id") or "")
        for raw_slot in requirement.get("coverageSlots") or []:
            if not isinstance(raw_slot, dict):
                continue
            slot_id = str(raw_slot.get("id") or "")
            if not slot_id or slot_id in seen:
                continue
            seen.add(slot_id)
            slots.append(
                {
                    **raw_slot,
                    "id": slot_id,
                    "parentRequirementId": parent_id,
                    "requirementKind": str(requirement.get("kind") or "point"),
                    "evidenceIntent": str(
                        requirement.get("evidenceIntent") or "fact"
                    ),
                }
            )
    return slots


def requires_semantic_validation(plan: dict[str, Any]) -> bool:
    """根据回答契约而非 simple/complex 标签决定是否验证语义支持。"""
    if plan.get("requirementSpecs") or plan.get("coreRequirements") or plan.get("answerRequirements"):
        return True
    if str(plan.get("questionType") or "simple_fact") in SEMANTIC_QUESTION_TYPES:
        return True
    return len(plan.get("retrievalFacets") or []) > 1


def normalize_execution_complexity(
    model_complexity: str,
    *,
    question_type: str,
    facet_count: int,
    requirement_count: int,
    broad_evidence_required: bool = False,
) -> str:
    """修正规划字段内部矛盾，避免综合任务被当作无需校验的简单事实。"""
    if (
        str(model_complexity).casefold() == "complex"
        or question_type in SEMANTIC_QUESTION_TYPES
        or facet_count > 1
        or requirement_count > 1
        or broad_evidence_required
    ):
        return "complex"
    return "simple"


def compile_tfidf_query(facet: dict[str, Any]) -> str:
    """把结构化 facet 编译成 TF-IDF 可消费的无布尔操作符文本。"""
    parts: list[str] = []
    for key in ("query", "goal"):
        value = str(facet.get(key) or "").strip()
        if value:
            parts.append(value)
    for key in ("phrases", "concepts"):
        values = facet.get(key)
        if isinstance(values, list):
            parts.extend(str(value).strip() for value in values if str(value).strip())
    cleaned = BOOLEAN_OPERATOR.sub(" ", "\n".join(parts))
    tokens: list[str] = []
    seen: set[str] = set()
    for token in TOKEN.findall(cleaned):
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return " ".join(tokens)


__all__ = [
    "EVIDENCE_INTENTS",
    "SECTION_TYPES",
    "compile_tfidf_query",
    "flatten_requirement_slots",
    "normalize_execution_complexity",
    "normalize_requirement",
    "normalize_section_types",
    "requires_semantic_validation",
]
