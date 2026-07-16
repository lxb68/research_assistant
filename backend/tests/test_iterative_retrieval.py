"""验证动态查询分解、章节重排和受约束的补偿检索。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.evidence_evaluator import EvidenceEvaluator
from app.agents.orchestrator_agent import OrchestratorAgent
from app.agents.research_chat_agent import ResearchAgentConfig, ResearchChatAgent
from app.services.rag_retriever import EvidenceChunk


class QueryPlanningAndRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ResearchChatAgent.__new__(ResearchChatAgent)
        self.agent.config = ResearchAgentConfig(max_sources=6, max_context_chars=18000)
        self.agent.log_callback = None
        self.agent.hunter = Mock()
        self.agent.retriever = Mock()

    @patch("app.agents.research_chat_agent.chat_completion")
    @patch("app.agents.research_chat_agent.ModelConfigStore.build_model_payload")
    def test_complex_plan_contains_dynamic_facets(self, build_model_payload: Mock, completion: Mock) -> None:
        build_model_payload.return_value = {"model": "test-model"}
        completion.return_value = json.dumps(
            {
                "standalone_question": "目标论文如何实现隐私训练？",
                "question_type": "mechanism",
                "complexity": "complex",
                "target_paper_ids": ["paper-1"],
                "target_chunks": [],
                "retrieval_facets": [
                    {
                        "id": "overview",
                        "goal": "整体流程",
                        "query": "private training framework overview",
                        "preferred_section_types": ["framework", "method"],
                    },
                    {
                        "id": "aggregation",
                        "goal": "聚合协议",
                        "query": "secure aggregation protocol",
                        "preferred_section_types": ["protocol", "algorithm"],
                    },
                ],
                "core_requirements": ["整体流程", "关键协议"],
                "optional_details": ["精确通信轮次"],
                "needs_clarification": False,
                "clarification_question": "",
            },
            ensure_ascii=False,
        )

        plan, _ = self.agent.plan_retrieval(
            "它怎么实现隐私训练？",
            [{"role": "assistant", "content": "论文 [1]", "sources": [{"record_id": "paper-1", "chunk_index": 3}]}],
        )

        self.assertEqual(plan["questionType"], "mechanism")
        self.assertEqual(plan["complexity"], "complex")
        self.assertEqual(len(plan["retrievalFacets"]), 2)
        self.assertTrue(plan["requiresIterativeRetrieval"])
        self.assertGreater(plan["targetEvidenceCount"], 2)
        self.assertEqual(plan["coreRequirements"], ["整体流程", "关键协议"])
        self.assertEqual(plan["optionalDetails"], ["精确通信轮次"])

    def test_putting_everything_together_is_classified_as_overview(self) -> None:
        section_type = self.agent._classify_section_type("Paper Title > 4.6 Putting Everything Together")
        self.assertEqual(section_type, "overview")

    def test_numbered_protocol_overrides_misleading_section_heading(self) -> None:
        """PDF 标题错挂时，正文中的完整步骤仍应识别为算法证据并得到提升。"""
        text = """Require: shares from both parties. Ensure: shared model.
1: locally partition the input.
2: jointly compute partition scores.
3: open the split identifier.
4: locally update sample indicators.
5: jointly compute leaf weights.
Figure 7: Private GBDT Training Framework.
"""
        chunk = EvidenceChunk(
            record_id="paper-1",
            title="Paper",
            section="4.5.2 To Use a Smaller Lattice Dimension",
            text=text,
            score=0,
        )

        self.assertEqual(
            self.agent._classify_evidence_type(chunk.section, chunk.text),
            "algorithm",
        )
        self.assertGreater(
            self.agent._content_intent_adjustment(
                chunk,
                question_type="mechanism",
                preferred_types={"protocol", "algorithm"},
            ),
            0.5,
        )

    def test_workflow_query_expands_with_algorithm_contract_terms(self) -> None:
        """完整流程检索应主动命中 Require/Ensure 和编号步骤块。"""
        expanded = self.agent._expand_facet_query(
            "完整训练流程",
            question_type="mechanism",
            preferred_types={"overview", "framework"},
        )

        self.assertIn("require ensure input output numbered steps", expanded)

    def test_mechanism_fusion_prefers_method_over_experiment(self) -> None:
        method = {
            "record_id": "paper-1",
            "chunk_index": 4,
            "title": "Paper",
            "section": "4 Proposed Framework > Secure Protocol",
            "text": "protocol details",
            "score": 0.72,
        }
        experiment = {
            "record_id": "paper-1",
            "chunk_index": 9,
            "title": "Paper",
            "section": "5 Experiments > Effectiveness Comparison",
            "text": "training configuration",
            "score": 0.83,
        }
        self.agent.hunter.translate_search_query.side_effect = lambda value: value
        self.agent.retriever.retrieve.return_value = [experiment, method]
        self.agent.retriever.last_diagnostics = {
            "retrievalMode": "hybrid_tfidf",
            "embeddingBackend": "tfidf",
            "candidateCount": 10,
            "queryCoverage": 0.8,
        }

        evidence, diagnostics = self.agent._retrieve_facets(
            [{"id": "paper-1"}],
            [
                {
                    "id": "method",
                    "goal": "实现机制",
                    "query": "implementation mechanism",
                    "preferredSectionTypes": ["framework", "protocol"],
                }
            ],
            question_type="mechanism",
            target_evidence_count=2,
            existing_evidence=[],
        )

        self.assertEqual(evidence[0]["chunk_index"], 4)
        self.assertEqual(diagnostics["methodEvidenceCount"], 1)
        self.assertEqual(diagnostics["facetCoverage"], 1.0)

    def test_evaluator_rejects_keyword_only_evidence_for_complex_mechanism(self) -> None:
        evaluation = EvidenceEvaluator().evaluate(
            {
                "evidenceCount": 4,
                "distinctPaperCount": 1,
                "selectedPaperIds": ["paper-1"],
                "queryCoverage": 0.8,
                "facetCount": 3,
                "facetCoverage": 1.0,
                "methodEvidenceCount": 0,
            },
            plan={
                "complexity": "complex",
                "questionType": "mechanism",
                "targetEvidenceCount": 6,
            },
            required_paper_ids=["paper-1"],
        )

        self.assertFalse(evaluation["sufficient"])
        self.assertTrue(any("方法、框架或协议" in reason for reason in evaluation["reasons"]))

    def test_semantic_evaluator_marks_mention_only_evidence_as_partial(self) -> None:
        completion = Mock(
            return_value=json.dumps(
                {
                    "facets": [
                        {
                            "id": "leaf-weight",
                            "status": "partial",
                            "supporting_refs": ["paper-1:2"],
                            "missing_detail": "只说明秘密共享存储，缺少计算步骤",
                            "refinement_query": "private leaf weight computation protocol",
                        }
                    ],
                    "requirements": [
                        {
                            "id": "req-1",
                            "status": "partial",
                            "supporting_refs": ["paper-1:2"],
                            "missing_detail": "缺少计算步骤",
                            "refinement_query": "leaf weight formula prediction update",
                        }
                    ],
                    "optional_details": [],
                },
                ensure_ascii=False,
            )
        )
        semantic, _ = EvidenceEvaluator().evaluate_semantic(
            [{"record_id": "paper-1", "chunk_index": 2, "title": "Paper", "section": "Method", "text": "Leaf weights are secret shared."}],
            {
                "standaloneQuestion": "如何计算叶子权重？",
                "retrievalFacets": [{"id": "leaf-weight", "goal": "叶子权重计算", "query": "leaf weight"}],
                "coreRequirements": ["说明叶子权重的计算步骤"],
            },
            completion=completion,
            model={"model": "test"},
            timeout=30,
        )

        self.assertFalse(semantic["answerable"])
        self.assertEqual(semantic["missingFacetIds"], ["leaf-weight"])
        self.assertTrue(any(item["id"] == "leaf-weight" for item in semantic["refinementFacets"]))
        self.assertTrue(any(item["id"] == "requirement-req-1" for item in semantic["refinementFacets"]))


class IterativeOrchestratorTest(unittest.IsolatedAsyncioTestCase):
    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_semantic_support_overrides_only_lexical_coverage_warning(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """直接证据逐项验证通过后，中英文字面覆盖率不应继续一票否决。"""
        build_model_payload.return_value = {"model": "test-model"}
        completion.return_value = json.dumps(
            {
                "facets": [
                    {
                        "id": "workflow",
                        "status": "supported",
                        "supporting_refs": ["paper-1:3"],
                    }
                ],
                "requirements": [
                    {
                        "id": "req-1",
                        "status": "supported",
                        "supporting_refs": ["paper-1:3"],
                    }
                ],
                "optional_details": [],
            },
            ensure_ascii=False,
        )
        agent = OrchestratorAgent()
        agent.run_logger = Mock()

        evaluation = await agent._evaluate_retrieved_evidence(
            {
                "evidenceCount": 2,
                "distinctPaperCount": 1,
                "selectedPaperIds": ["paper-1"],
                "queryCoverage": 0.1,
                "facetCount": 1,
                "facetCoverage": 1.0,
                "methodEvidenceCount": 2,
            },
            evidence=[
                {
                    "record_id": "paper-1",
                    "chunk_index": 3,
                    "title": "Paper",
                    "section": "Algorithm",
                    "text": "完整训练步骤。",
                }
            ],
            plan={
                "standaloneQuestion": "完整协议是什么？",
                "complexity": "complex",
                "questionType": "mechanism",
                "targetEvidenceCount": 2,
                "retrievalFacets": [{"id": "workflow", "goal": "完整流程", "query": "workflow"}],
                "coreRequirements": ["说明完整步骤"],
            },
            required_paper_ids=["paper-1"],
        )

        self.assertTrue(evaluation["sufficient"])
        self.assertEqual(evaluation["reasons"], [])

    @patch("app.agents.orchestrator_agent.ResearchChatAgent")
    async def test_complex_pipeline_runs_one_refinement_round(self, research_agent_class: Mock) -> None:
        research_agent = research_agent_class.return_value
        plan = {
            "standaloneQuestion": "论文如何实现隐私训练？",
            "questionType": "mechanism",
            "complexity": "complex",
            "targetPaperIds": ["paper-1"],
            "targetChunks": [],
            "retrievalFacets": [
                {"id": "overview", "goal": "整体流程", "query": "framework", "preferredSectionTypes": ["framework"]},
                {"id": "protocol", "goal": "关键协议", "query": "protocol", "preferredSectionTypes": ["protocol"]},
            ],
            "answerRequirements": ["整体流程", "关键协议"],
            "requiresIterativeRetrieval": True,
            "targetEvidenceCount": 6,
            "needsClarification": False,
            "clarificationQuestion": "",
        }
        first_evidence = [{"record_id": "paper-1", "chunk_index": 1, "text": "overview"}]
        final_evidence = [
            {"record_id": "paper-1", "chunk_index": 1, "text": "overview"},
            {"record_id": "paper-1", "chunk_index": 2, "text": "method"},
        ]
        first_diagnostics = {
            "paperCount": 1,
            "evidenceCount": 1,
            "distinctPaperCount": 1,
            "selectedPaperIds": ["paper-1"],
            "queryCoverage": 0.8,
            "facetCount": 2,
            "facetCoverage": 0.5,
            "missingFacetIds": ["protocol"],
            "methodEvidenceCount": 1,
        }
        final_diagnostics = {
            "paperCount": 1,
            "evidenceCount": 2,
            "distinctPaperCount": 1,
            "selectedPaperIds": ["paper-1"],
            "queryCoverage": 0.8,
            "facetCount": 1,
            "facetCoverage": 1.0,
            "missingFacetIds": [],
            "methodEvidenceCount": 2,
        }
        research_agent.plan_retrieval.return_value = (plan, "{}")
        research_agent.retrieve_evidence.side_effect = [
            (first_evidence, first_diagnostics),
            (final_evidence, final_diagnostics),
        ]
        research_agent.run.return_value = {"answer": "完整回答", "sources": []}
        agent = OrchestratorAgent()
        agent.run_logger = Mock()
        agent.recovery.execute = AsyncMock(return_value=(research_agent.run.return_value, []))
        agent._evaluate_retrieved_evidence = AsyncMock(
            side_effect=[
                {
                    "sufficient": False,
                    "reasons": ["关键协议仅部分支持"],
                    "missingFacetIds": ["protocol"],
                    "methodEvidenceCount": 1,
                    "refinementFacets": [plan["retrievalFacets"][1]],
                },
                {
                    "sufficient": True,
                    "reasons": [],
                    "missingFacetIds": [],
                    "methodEvidenceCount": 2,
                },
            ]
        )

        result = await agent._run_research_pipeline("它怎么训练？", {"history": []})

        self.assertEqual(result["action"], "chat")
        self.assertEqual(research_agent.retrieve_evidence.call_count, 2)
        second_call = research_agent.retrieve_evidence.call_args_list[1]
        self.assertEqual([item["id"] for item in second_call.kwargs["retrieval_facets"]], ["protocol"])
        self.assertEqual(second_call.kwargs["existing_evidence"], first_evidence)


if __name__ == "__main__":
    unittest.main()
