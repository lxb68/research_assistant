"""判定候选证据组对核心回答要求的直接、部分或不支持关系。"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.services.evidence_groups import evidence_group_key, group_evidence
from app.prompt_loader import load_prompt
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT_ZH
from app.services.retrieval_contracts import (
    flatten_requirement_slots,
    normalize_requirement,
)


CompletionCallable = Callable[..., str]


class CandidateCoverageEvaluator:
    """在最终选择前建立证据组到核心要求的语义支持矩阵。"""

    SYSTEM_PROMPT = load_prompt("evidence/candidate_coverage.zh.md")
    REPAIR_PROMPT = load_prompt("evidence/candidate_coverage_repair.zh.md")

    def __init__(self, *, batch_size: int = 6) -> None:
        self.batch_size = max(1, min(int(batch_size), 20))

    def evaluate(
        self,
        evidence: list[dict[str, Any]],
        requirement_specs: list[dict[str, Any]],
        *,
        question: str,
        question_type: str,
        completion: CompletionCallable,
        model: dict[str, Any],
        timeout: int,
    ) -> tuple[dict[str, dict[str, dict[str, Any]]], str]:
        requirements = [
            item
            for index, value in enumerate(requirement_specs, 1)
            if (
                item := normalize_requirement(value, index, question_type=question_type)
            )
            is not None
            and item.get("required")
        ]
        if not requirements or not evidence:
            return {}, ""
        slots = flatten_requirement_slots(requirements)

        evidence_payload: list[dict[str, Any]] = []
        known_refs: set[str] = set()
        for group in group_evidence(evidence):
            key = evidence_group_key(group[0])
            reference = self._reference(key)
            known_refs.add(reference)
            combined_text = "\n".join(str(item.get("text") or "") for item in group)
            evidence_payload.append(
                {
                    "evidence_ref": reference,
                    "title": str(group[0].get("title") or "")[:500],
                    "record_id": str(group[0].get("record_id") or "")[:200],
                    "year": str(group[0].get("year") or "")[:20],
                    "section": str(group[0].get("section") or "")[:1000],
                    "text": combined_text[:3000],
                }
            )

        requirement_payload = [
            {
                "id": item["id"],
                "parent_requirement_id": item["parentRequirementId"],
                "description": item["description"],
                "role": item["role"],
                "requirement_kind": item["requirementKind"],
                "evidence_intent": item["evidenceIntent"],
            }
            for item in slots
        ]
        assessments: list[dict[str, Any]] = []
        raw_responses: list[str] = []
        for offset in range(0, len(evidence_payload), self.batch_size):
            batch = evidence_payload[offset : offset + self.batch_size]
            payload, batch_responses = self._evaluate_batch(
                question=question,
                requirements=requirement_payload,
                evidence_groups=batch,
                completion=completion,
                model=model,
                timeout=timeout,
            )
            raw_responses.extend(batch_responses)
            assessments.extend(
                item
                for item in payload.get("assessments", [])
                if isinstance(item, dict)
            )
        allowed_requirements = {str(item["id"]) for item in slots}
        matrix: dict[str, dict[str, dict[str, Any]]] = {}
        for item in assessments:
            if not isinstance(item, dict):
                continue
            reference = str(item.get("evidence_ref") or item.get("evidenceRef") or "")
            requirement_id = str(item.get("requirement_id") or item.get("requirementId") or "")
            if reference not in known_refs or requirement_id not in allowed_requirements:
                continue
            status = str(item.get("status") or "unsupported").strip().casefold()
            if status not in {"direct", "partial", "unsupported"}:
                status = "unsupported"
            try:
                confidence = float(item.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            matrix.setdefault(reference, {})[requirement_id] = {
                "status": status,
                "confidence": max(0.0, min(confidence, 1.0)),
                "timelineRole": str(
                    item.get("timeline_role") or item.get("timelineRole") or ""
                )[:80],
                "year": str(item.get("year") or "")[:20],
                "claims": [
                    str(value)[:500]
                    for value in item.get("claims") or []
                    if str(value).strip()
                ][:8],
                "entities": (
                    {
                        str(key)[:80]: (
                            [str(value)[:200] for value in raw_value[:12]]
                            if isinstance(raw_value, list)
                            else str(raw_value)[:500]
                        )
                        for key, raw_value in item.get("entities", {}).items()
                    }
                    if isinstance(item.get("entities"), dict)
                    else {}
                ),
            }

        # 模型漏掉的组合必须显式视为 unsupported，不能静默当作覆盖。
        for reference in known_refs:
            for requirement_id in allowed_requirements:
                matrix.setdefault(reference, {}).setdefault(
                    requirement_id,
                    {"status": "unsupported", "confidence": 0.0},
                )
        return matrix, "\n".join(raw_responses)

    def _evaluate_batch(
        self,
        *,
        question: str,
        requirements: list[dict[str, Any]],
        evidence_groups: list[dict[str, Any]],
        completion: CompletionCallable,
        model: dict[str, Any],
        timeout: int,
    ) -> tuple[dict[str, Any], list[str]]:
        raw_response = completion(
            model,
            [
                {
                    "role": "system",
                    "content": f"{self.SYSTEM_PROMPT}\n\n{SYSTEM_SECURITY_CONSTRAINT_ZH}",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": str(question or "")[:2000],
                            "requirements": requirements,
                            "evidence_groups": evidence_groups,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            timeout=timeout,
            response_format={"type": "json_object"},
        )
        responses = [str(raw_response or "")]
        try:
            return self._parse_response(raw_response), responses
        except (ValueError, json.JSONDecodeError):
            repaired = completion(
                model,
                [
                    {
                        "role": "system",
                        "content": f"{self.REPAIR_PROMPT}\n\n{SYSTEM_SECURITY_CONSTRAINT_ZH}",
                    },
                    {
                        "role": "user",
                        "content": str(raw_response or "")[:30000],
                    },
                ],
                temperature=0,
                timeout=timeout,
                response_format={"type": "json_object"},
            )
            responses.append(str(repaired or ""))
            return self._parse_response(repaired), responses

    @staticmethod
    def annotate(
        evidence: list[dict[str, Any]],
        matrix: dict[str, dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        annotated: list[dict[str, Any]] = []
        status_rank = {"unsupported": 0, "partial": 1, "direct": 2}
        for group in group_evidence(evidence):
            reference = CandidateCoverageEvaluator._reference(evidence_group_key(group[0]))
            support = {
                str(requirement_id): dict(assessment)
                for requirement_id, assessment in matrix.get(reference, {}).items()
            }
            # 补偿检索必须保留旧轮次已经确认的更强支持关系，避免模型重评波动造成证据回退。
            for raw_item in group:
                for requirement_id, raw_assessment in (
                    raw_item.get("requirement_support") or {}
                ).items():
                    assessment = (
                        dict(raw_assessment)
                        if isinstance(raw_assessment, dict)
                        else {}
                    )
                    current = support.get(str(requirement_id))
                    if current is None or status_rank.get(
                        str(assessment.get("status") or "unsupported"), 0
                    ) > status_rank.get(
                        str(current.get("status") or "unsupported"), 0
                    ):
                        support[str(requirement_id)] = assessment
            for raw_item in group:
                item = dict(raw_item)
                item["requirement_support"] = {
                    str(requirement_id): dict(assessment)
                    for requirement_id, assessment in support.items()
                }
                item["coverage_evidence_ref"] = reference
                annotated.append(item)
        return annotated

    @staticmethod
    def _reference(key: tuple[str, str, str | int]) -> str:
        return f"{key[0]}:{key[1]}:{key[2]}"

    @staticmethod
    def _parse_response(raw_response: str) -> dict[str, Any]:
        text = str(raw_response or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else text
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("模型未返回有效的候选证据覆盖结果")
            payload = json.loads(text[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("候选证据覆盖结果不是 JSON 对象")
        return payload


__all__ = ["CandidateCoverageEvaluator"]
