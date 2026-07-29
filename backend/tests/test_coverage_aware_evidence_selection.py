"""验证动态预算、候选语义支持矩阵和覆盖感知证据选择。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.candidate_coverage_evaluator import CandidateCoverageEvaluator
from app.services.answer_policy import AnswerPolicy
from app.services.coverage_aware_evidence_selector import CoverageAwareEvidenceSelector
from app.services.evidence_budget_policy import EvidenceBudgetPolicy
from app.services.retrieval_contracts import normalize_requirement


class EvidenceBudgetPolicyTest(unittest.TestCase):
    def test_requirement_capacity_can_raise_target_above_legacy_six(self) -> None:
        budget = EvidenceBudgetPolicy().resolve(
            question_type="synthesis",
            requirement_specs=[
                {
                    "id": f"req-{index}",
                    "description": f"要求 {index}",
                    "minimumDirectEvidence": 2,
                }
                for index in range(1, 5)
            ],
            requested_target=6,
            maximum_context_chars=18000,
            maximum_groups=12,
        )

        self.assertEqual(budget.target_groups, 8)
        self.assertEqual(budget.maximum_groups, 12)
        self.assertEqual(
            budget.required_direct_evidence,
            {"req-1": 2, "req-2": 2, "req-3": 2, "req-4": 2},
        )

    def test_chronology_budget_uses_atomic_slots_and_diversity_constraints(self) -> None:
        budget = EvidenceBudgetPolicy().resolve(
            question_type="synthesis",
            requirement_specs=[
                {
                    "id": "req-lineage",
                    "description": "说明发展脉络",
                    "kind": "chronology",
                    "coverageSlots": [
                        {"id": "slot-early", "description": "前序工作", "role": "predecessor"},
                        {"id": "slot-middle", "description": "中间转折", "role": "transition"},
                        {"id": "slot-recent", "description": "近期方案", "role": "recent"},
                    ],
                    "minimumDistinctSources": 2,
                    "minimumDistinctPeriods": 2,
                }
            ],
            requested_target=2,
            maximum_context_chars=18000,
            maximum_groups=12,
        )

        self.assertEqual(
            budget.required_direct_evidence,
            {"slot-early": 1, "slot-middle": 1, "slot-recent": 1},
        )
        self.assertEqual(budget.target_groups, 3)
        self.assertEqual(budget.required_distinct_sources["req-lineage"], 2)
        self.assertEqual(budget.required_distinct_periods["req-lineage"], 2)

    def test_chronology_defaults_are_generic_not_domain_specific(self) -> None:
        requirement = normalize_requirement(
            {
                "id": "req-history",
                "description": "梳理任意主题的发展过程",
                "kind": "chronology",
                "coverageSlots": [
                    {"id": "early", "description": "早期节点"},
                    {"id": "recent", "description": "近期节点"},
                ],
            },
            1,
            question_type="synthesis",
        )

        self.assertEqual(requirement["minimumDistinctSources"], 2)
        self.assertEqual(requirement["minimumDistinctPeriods"], 2)


class CandidateCoverageEvaluatorTest(unittest.TestCase):
    def test_invalid_json_after_repair_fails_closed(self) -> None:
        completion = Mock(side_effect=["not-json", "still-not-json"])

        with self.assertRaises((ValueError, json.JSONDecodeError)):
            CandidateCoverageEvaluator().evaluate(
                [{"record_id": "paper-a", "chunk_index": 1, "text": "候选内容"}],
                [{"id": "req-a", "description": "说明方法"}],
                question="方法是什么？",
                question_type="mechanism",
                completion=completion,
                model={"model": "test"},
                timeout=30,
            )

        self.assertEqual(completion.call_count, 2)

    def test_invalid_batch_json_is_repaired_once(self) -> None:
        completion = Mock(
            side_effect=[
                '{"assessments":[{"evidence_ref":"paper-a:chunk:1"',
                json.dumps(
                    {
                        "assessments": [
                            {
                                "evidence_ref": "paper-a:chunk:1",
                                "requirement_id": "req-a",
                                "status": "direct",
                                "confidence": 0.9,
                            }
                        ]
                    }
                ),
            ]
        )

        matrix, raw = CandidateCoverageEvaluator().evaluate(
            [{"record_id": "paper-a", "chunk_index": 1, "text": "直接证据"}],
            [{"id": "req-a", "description": "说明方法"}],
            question="方法是什么？",
            question_type="mechanism",
            completion=completion,
            model={"model": "test"},
            timeout=30,
        )

        self.assertEqual(completion.call_count, 2)
        self.assertEqual(matrix["paper-a:chunk:1"]["req-a"]["status"], "direct")
        self.assertIn("paper-a:chunk:1", raw)

    def test_answer_policy_blocks_complete_synthesis_without_semantic_validation(self) -> None:
        prompt = AnswerPolicy().build_prompt(
            base_prompt="证据：{{evidence}}",
            evidence_context="[1] 局部证据",
            answer_requirements=["完整发展脉络"],
            retrieval_state={
                "semanticValidated": False,
                "candidateCoverageValidated": False,
                "evidenceSufficient": False,
            },
        )

        self.assertIn("不得声称已经形成完整脉络", prompt)

    def test_coverage_evaluator_keeps_direct_and_partial_separate(self) -> None:
        completion = Mock(
            return_value=json.dumps(
                {
                    "assessments": [
                        {
                            "evidence_ref": "paper-a:chunk:1",
                            "requirement_id": "req-a",
                            "status": "direct",
                            "confidence": 0.94,
                        },
                        {
                            "evidence_ref": "paper-b:chunk:2",
                            "requirement_id": "req-a",
                            "status": "partial",
                            "confidence": 0.8,
                        },
                    ]
                }
            )
        )
        evaluator = CandidateCoverageEvaluator()

        matrix, _ = evaluator.evaluate(
            [
                {
                    "record_id": "paper-a",
                    "chunk_index": 1,
                    "title": "A",
                    "section": "Method",
                    "text": "完整方法。",
                },
                {
                    "record_id": "paper-b",
                    "chunk_index": 2,
                    "title": "B",
                    "section": "Background",
                    "text": "只进行背景提及。",
                },
            ],
            [{"id": "req-a", "description": "说明完整方法"}],
            question="方法是什么？",
            question_type="mechanism",
            completion=completion,
            model={"model": "test"},
            timeout=30,
        )

        self.assertEqual(matrix["paper-a:chunk:1"]["req-a"]["status"], "direct")
        self.assertEqual(matrix["paper-b:chunk:2"]["req-a"]["status"], "partial")

    def test_annotation_does_not_downgrade_verified_existing_support(self) -> None:
        evidence = [
            {
                "record_id": "paper-a",
                "chunk_index": 1,
                "text": "已验证证据",
                "requirement_support": {
                    "req-a": {"status": "direct", "confidence": 0.9}
                },
            }
        ]

        annotated = CandidateCoverageEvaluator.annotate(
            evidence,
            {
                "paper-a:chunk:1": {
                    "req-a": {"status": "partial", "confidence": 0.7}
                }
            },
        )

        self.assertEqual(
            annotated[0]["requirement_support"]["req-a"]["status"],
            "direct",
        )

    def test_evaluator_assesses_atomic_slots_instead_of_broad_parent(self) -> None:
        def completion(_model, messages, **_kwargs):
            payload = json.loads(messages[1]["content"])
            self.assertEqual(
                [item["id"] for item in payload["requirements"]],
                ["slot-predecessor", "slot-recent"],
            )
            return json.dumps(
                {
                    "assessments": [
                        {
                            "evidence_ref": "paper-a:chunk:1",
                            "requirement_id": "slot-recent",
                            "status": "direct",
                            "confidence": 0.95,
                            "timeline_role": "recent",
                            "year": "2024",
                            "claims": ["提出近期方案"],
                            "entities": {"method": "Method A"},
                        }
                    ]
                }
            )

        matrix, _ = CandidateCoverageEvaluator().evaluate(
            [
                {
                    "record_id": "paper-a",
                    "chunk_index": 1,
                    "title": "A",
                    "year": "2024",
                    "text": "近期方案",
                }
            ],
            [
                {
                    "id": "req-lineage",
                    "description": "说明发展脉络",
                    "kind": "chronology",
                    "coverageSlots": [
                        {"id": "slot-predecessor", "description": "前序工作"},
                        {"id": "slot-recent", "description": "近期工作"},
                    ],
                }
            ],
            question="发展脉络是什么？",
            question_type="synthesis",
            completion=completion,
            model={"model": "test"},
            timeout=30,
        )

        self.assertEqual(matrix["paper-a:chunk:1"]["slot-recent"]["year"], "2024")
        self.assertEqual(
            matrix["paper-a:chunk:1"]["slot-predecessor"]["status"],
            "unsupported",
        )


class CoverageAwareEvidenceSelectorTest(unittest.TestCase):
    @staticmethod
    def _item(
        paper_id: str,
        index: int,
        *,
        score: float,
        support: dict[str, str],
        text: str,
    ) -> dict:
        return {
            "record_id": paper_id,
            "chunk_index": index,
            "title": paper_id,
            "section": "Method",
            "text": text,
            "fusion_score": score,
            "requirement_support": {
                requirement_id: {"status": status, "confidence": 0.9}
                for requirement_id, status in support.items()
            },
        }

    def test_lower_ranked_missing_dimension_is_kept_over_redundant_high_scores(self) -> None:
        budget = EvidenceBudgetPolicy().resolve(
            question_type="synthesis",
            requirement_specs=[
                {
                    "id": "req-inference",
                    "description": "推理路线",
                    "minimumDirectEvidence": 1,
                },
                {
                    "id": "req-training",
                    "description": "训练路线",
                    "minimumDirectEvidence": 1,
                },
            ],
            requested_target=6,
            maximum_context_chars=18000,
            maximum_groups=12,
        )
        evidence = [
            self._item(
                "inference-high-1",
                1,
                score=20,
                support={"req-inference": "direct", "req-training": "unsupported"},
                text="高分推理证据一",
            ),
            self._item(
                "inference-high-2",
                2,
                score=19,
                support={"req-inference": "direct", "req-training": "unsupported"},
                text="高分推理证据二",
            ),
            self._item(
                "training-low",
                3,
                score=1,
                support={"req-inference": "unsupported", "req-training": "direct"},
                text="较低检索分但直接支持训练路线",
            ),
        ]

        result = CoverageAwareEvidenceSelector().select(evidence, budget=budget)
        selected_ids = {item["record_id"] for item in result.evidence}

        self.assertIn("training-low", selected_ids)
        self.assertIn("inference-high-1", selected_ids)
        self.assertEqual(result.diagnostics["unsupportedRequirementIds"], [])

    def test_two_partial_items_do_not_satisfy_one_direct_requirement(self) -> None:
        budget = EvidenceBudgetPolicy().resolve(
            question_type="mechanism",
            requirement_specs=[
                {
                    "id": "req-method",
                    "description": "完整方法",
                    "minimumDirectEvidence": 1,
                }
            ],
            requested_target=2,
            maximum_context_chars=18000,
            maximum_groups=6,
        )
        evidence = [
            self._item(
                "partial-a",
                1,
                score=3,
                support={"req-method": "partial"},
                text="部分信息一",
            ),
            self._item(
                "partial-b",
                2,
                score=2,
                support={"req-method": "partial"},
                text="部分信息二",
            ),
        ]

        result = CoverageAwareEvidenceSelector().select(evidence, budget=budget)

        self.assertEqual(
            result.diagnostics["selectedDirectEvidenceByRequirement"]["req-method"],
            0,
        )
        self.assertEqual(
            result.diagnostics["unsupportedRequirementIds"],
            ["req-method"],
        )

    def test_missing_semantic_signals_stops_at_dynamic_target(self) -> None:
        budget = EvidenceBudgetPolicy().resolve(
            question_type="mechanism",
            requirement_specs=[
                {
                    "id": "req-method",
                    "description": "完整方法",
                    "minimumDirectEvidence": 1,
                }
            ],
            requested_target=3,
            maximum_context_chars=18000,
            maximum_groups=8,
        )
        evidence = [
            self._item(
                f"paper-{index}",
                index,
                score=float(10 - index),
                support={},
                text=f"候选证据 {index}",
            )
            for index in range(1, 7)
        ]

        result = CoverageAwareEvidenceSelector().select(evidence, budget=budget)

        self.assertEqual(len(result.evidence), 3)
        self.assertFalse(result.diagnostics["coverageSignalsAvailable"])

    def test_existing_direct_evidence_is_locked_across_refinement(self) -> None:
        budget = EvidenceBudgetPolicy().resolve(
            question_type="comparison",
            requirement_specs=[
                {"id": "req-a", "description": "要求 A"},
                {"id": "req-b", "description": "要求 B"},
            ],
            requested_target=2,
            maximum_context_chars=18000,
            maximum_groups=4,
        )
        existing = self._item(
            "existing",
            1,
            score=0.1,
            support={"req-a": "direct", "req-b": "unsupported"},
            text="旧轮次已经验证的直接证据",
        )
        existing["existing_evidence"] = True
        evidence = [
            existing,
            self._item(
                "new",
                2,
                score=10,
                support={"req-a": "unsupported", "req-b": "direct"},
                text="新轮次补足要求 B",
            ),
        ]

        result = CoverageAwareEvidenceSelector().select(evidence, budget=budget)

        self.assertEqual(
            {item["record_id"] for item in result.evidence},
            {"existing", "new"},
        )

    def test_one_recent_paper_cannot_complete_a_multi_period_lineage(self) -> None:
        budget = EvidenceBudgetPolicy().resolve(
            question_type="synthesis",
            requirement_specs=[
                {
                    "id": "req-lineage",
                    "description": "说明发展脉络",
                    "kind": "chronology",
                    "coverageSlots": [
                        {"id": "slot-early", "description": "前序工作"},
                        {"id": "slot-transition", "description": "转折工作"},
                        {"id": "slot-recent", "description": "近期工作"},
                    ],
                    "minimumDistinctSources": 2,
                    "minimumDistinctPeriods": 2,
                }
            ],
            requested_target=3,
            maximum_context_chars=18000,
            maximum_groups=8,
        )
        recent = self._item(
            "recent-paper",
            1,
            score=10,
            support={
                "slot-early": "direct",
                "slot-transition": "direct",
                "slot-recent": "direct",
            },
            text="近期论文中的相关工作和新方案",
        )
        recent["year"] = "2024"

        incomplete = CoverageAwareEvidenceSelector().select([recent], budget=budget)

        self.assertEqual(
            incomplete.diagnostics["unsupportedRequirementIds"],
            ["req-lineage"],
        )
        self.assertEqual(
            incomplete.diagnostics["selectedDistinctSourcesByRequirement"]["req-lineage"],
            1,
        )

        predecessor = self._item(
            "predecessor-paper",
            2,
            score=1,
            support={"slot-early": "direct"},
            text="较早论文直接提出前序方法",
        )
        predecessor["year"] = "2020"
        complete = CoverageAwareEvidenceSelector().select(
            [recent, predecessor],
            budget=budget,
        )

        self.assertEqual(complete.diagnostics["unsupportedRequirementIds"], [])
        self.assertEqual(
            {item["record_id"] for item in complete.evidence},
            {"recent-paper", "predecessor-paper"},
        )


if __name__ == "__main__":
    unittest.main()
