"""判定候选证据组对核心回答要求的直接、部分或不支持关系。"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from app.core.config import settings
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

    def __init__(
        self,
        *,
        batch_size: int = 4,
        max_concurrency: int | None = None,
        max_matrix_cells: int = 32,
        max_input_chars: int | None = None,
    ) -> None:
        self.batch_size = max(1, min(int(batch_size), 20))
        self.max_concurrency = max(
            1,
            min(
                int(max_concurrency or settings.agent_model_max_concurrency),
                settings.agent_model_max_concurrency,
            ),
        )
        self.max_matrix_cells = max(1, int(max_matrix_cells))
        self.max_input_chars = max(
            1000,
            int(max_input_chars or settings.research_semantic_max_context_chars),
        )

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
        diagnostics: dict[str, Any] | None = None,
    ) -> tuple[dict[str, dict[str, dict[str, Any]]], str]:
        coverage_diagnostics = diagnostics if diagnostics is not None else {}
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
            coverage_diagnostics.update(
                {
                    "candidateCoverageBatchCount": 0,
                    "candidateCoverageMatrixCellCount": 0,
                }
            )
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
        batches = self._build_batches(
            evidence_payload,
            requirement_count=len(requirement_payload),
            base_input_chars=(
                len(self.SYSTEM_PROMPT)
                + len(SYSTEM_SECURITY_CONSTRAINT_ZH)
                + len(str(question or "")[:2000])
                + len(json.dumps(requirement_payload, ensure_ascii=False))
            ),
        )
        worker_count = min(self.max_concurrency, len(batches))
        coverage_diagnostics.update(
            {
                "candidateCoverageBatchCount": len(batches),
                "candidateCoverageMaxConcurrency": worker_count,
                "candidateCoverageMatrixCellCount": (
                    len(evidence_payload) * len(requirement_payload)
                ),
                "candidateCoverageBatchGroupCounts": [
                    len(batch) for batch in batches
                ],
                "candidateCoverageBatchInputChars": [
                    len(self.SYSTEM_PROMPT)
                    + len(SYSTEM_SECURITY_CONSTRAINT_ZH)
                    + len(
                        json.dumps(
                            {
                                "question": str(question or "")[:2000],
                                "requirements": requirement_payload,
                                "evidence_groups": batch,
                            },
                            ensure_ascii=False,
                        )
                    )
                    for batch in batches
                ],
            }
        )
        batch_results: dict[int, tuple[dict[str, Any], list[str], float]] = {}
        batch_errors: dict[int, Exception] = {}
        active_batches = 0
        peak_batches = 0
        active_lock = threading.Lock()

        def run_batch(
            batch: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], list[str], float]:
            nonlocal active_batches, peak_batches
            started_at = time.perf_counter()
            with active_lock:
                active_batches += 1
                peak_batches = max(peak_batches, active_batches)
            try:
                payload, responses = self._evaluate_batch(
                    question=question,
                    requirements=requirement_payload,
                    evidence_groups=batch,
                    completion=completion,
                    model=model,
                    timeout=timeout,
                )
                return (
                    payload,
                    responses,
                    round((time.perf_counter() - started_at) * 1000, 2),
                )
            except Exception as error:
                setattr(
                    error,
                    "coverage_duration_ms",
                    round((time.perf_counter() - started_at) * 1000, 2),
                )
                raise
            finally:
                with active_lock:
                    active_batches -= 1

        # 候选批次相互独立；模型客户端还会执行进程级全局限流。
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="候选证据覆盖",
        ) as executor:
            futures = {
                executor.submit(
                    run_batch,
                    batch,
                ): index
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    batch_results[index] = future.result()
                except Exception as error:
                    batch_errors[index] = error

        coverage_diagnostics["candidateCoverageConcurrentBatchPeak"] = peak_batches
        coverage_diagnostics["candidateCoverageFailedBatchCount"] = len(batch_errors)
        coverage_diagnostics["candidateCoverageBatchDurationMs"] = [
            (
                batch_results[index][2]
                if index in batch_results
                else getattr(batch_errors[index], "coverage_duration_ms", None)
            )
            for index in range(len(batches))
        ]
        coverage_diagnostics["candidateCoverageRepairBatchCount"] = sum(
            len(batch_results[index][1]) > 1 for index in batch_results
        ) + sum(
            int(getattr(error, "coverage_response_count", 1)) > 1
            for error in batch_errors.values()
        )
        if batch_errors:
            first_index = min(batch_errors)
            raise batch_errors[first_index]

        assessments: list[dict[str, Any]] = []
        raw_responses: list[str] = []
        for index in range(len(batches)):
            payload, batch_responses, _ = batch_results[index]
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

    def _build_batches(
        self,
        evidence_payload: list[dict[str, Any]],
        *,
        requirement_count: int,
        base_input_chars: int,
    ) -> list[list[dict[str, Any]]]:
        """按组数、判定单元和输入字符预算稳定拆分候选证据。"""
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = max(0, int(base_input_chars))
        cells_per_group = max(1, int(requirement_count))

        for item in evidence_payload:
            item_chars = len(json.dumps(item, ensure_ascii=False))
            exceeds_budget = bool(current) and (
                len(current) >= self.batch_size
                or (len(current) + 1) * cells_per_group > self.max_matrix_cells
                or current_chars + item_chars > self.max_input_chars
            )
            if exceeds_budget:
                batches.append(current)
                current = []
                current_chars = max(0, int(base_input_chars))
            current.append(item)
            current_chars += item_chars

        if current:
            batches.append(current)
        return batches

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
            max_output_tokens=settings.research_semantic_max_output_tokens,
            thinking=False,
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
                max_output_tokens=settings.research_semantic_max_output_tokens,
                thinking=False,
            )
            responses.append(str(repaired or ""))
            try:
                return self._parse_response(repaired), responses
            except (ValueError, json.JSONDecodeError) as error:
                setattr(error, "coverage_response_count", len(responses))
                raise

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
