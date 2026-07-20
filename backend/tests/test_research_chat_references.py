"""验证研究追问的上下文规划、来源约束与证据复用。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.research_chat_agent import ResearchAgentConfig, ResearchChatAgent


class ResearchChatReferenceTest(unittest.TestCase):
    """覆盖语义追问改写、来源白名单和无污染检索。"""

    def setUp(self) -> None:
        self.agent = ResearchChatAgent.__new__(ResearchChatAgent)
        self.agent.config = ResearchAgentConfig(max_papers=20)
        self.agent.log_callback = None
        self.agent.hunter = Mock()
        self.agent.retriever = Mock()
        self.agent.graph_retriever = Mock()
        self.agent.graph_retriever.retrieve.return_value = ([], {"enabled": False, "attempted": False})
        self.agent.graph_retriever.merge_evidence.side_effect = (
            lambda text_evidence, graph_evidence, limit: [*text_evidence, *graph_evidence][:limit]
        )

    @patch("app.agents.research_chat_agent.chat_completion")
    @patch("app.agents.research_chat_agent.ModelConfigStore.build_model_payload")
    def test_planner_resolves_semantic_followup_to_structured_source(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """“这个片段”应由模型结合来源元数据解析，而不是依赖编号正则。"""
        build_model_payload.return_value = {"model": "test-model"}
        completion.return_value = json.dumps(
            {
                "standalone_question": "Squirrel 论文实验部分的通信开销如何？",
                "target_paper_ids": ["paper-3"],
                "target_chunks": [{"record_id": "paper-3", "chunk_index": 7}],
                "needs_clarification": False,
                "clarification_question": "",
            },
            ensure_ascii=False,
        )
        history = [
            {"role": "user", "content": "Squirrel 的实验结果是什么？"},
            {
                "role": "assistant",
                "content": "它在通信开销方面有明显改进 [1]。",
                "sources": [
                    {
                        "index": 1,
                        "record_id": "paper-3",
                        "title": "Squirrel",
                        "section": "Experiments",
                        "chunk_index": 7,
                        "excerpt": "Communication cost...",
                    }
                ],
            },
        ]

        plan, _ = self.agent.plan_retrieval("这个片段的实验设置可靠吗？", history)

        self.assertEqual(plan["standaloneQuestion"], "Squirrel 论文实验部分的通信开销如何？")
        self.assertEqual(plan["targetPaperIds"], ["paper-3"])
        self.assertEqual(plan["targetChunks"], [{"record_id": "paper-3", "chunk_index": 7}])
        planner_payload = json.loads(completion.call_args.args[1][1]["content"])
        self.assertEqual(planner_payload["candidate_sources"][0]["record_id"], "paper-3")
        self.assertNotIn("history", planner_payload)
        self.assertEqual(planner_payload["historical_user_intents"][0]["content"], "Squirrel 的实验结果是什么？")
        self.assertEqual(planner_payload["prior_answers"][0]["trust"], "unverified_prior_answer")
        self.assertFalse(planner_payload["prior_answers"][0]["allowed_as_evidence"])

    @patch("app.agents.research_chat_agent.chat_completion")
    @patch("app.agents.research_chat_agent.ModelConfigStore.build_model_payload")
    def test_planner_rejects_hallucinated_source_ids(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """模型编造的记录 ID 必须被白名单拒绝并转为澄清请求。"""
        build_model_payload.return_value = {"model": "test-model"}
        completion.return_value = (
            '{"standalone_question":"追问","target_paper_ids":["invented"],'
            '"target_chunks":[],"needs_clarification":false,"clarification_question":""}'
        )
        history = [
            {
                "role": "assistant",
                "content": "来源 [1]",
                "sources": [{"index": 1, "record_id": "paper-1", "title": "Paper One", "chunk_index": 2}],
            }
        ]

        plan, _ = self.agent.plan_retrieval("它有什么局限？", history)

        self.assertEqual(plan["targetPaperIds"], [])
        self.assertTrue(plan["needsClarification"])
        self.assertEqual(plan["invalidTargetIds"], ["invented"])

    @patch("app.agents.research_chat_agent.chat_completion")
    @patch("app.agents.research_chat_agent.ModelConfigStore.build_model_payload")
    def test_planner_keeps_self_contained_question_without_history_concatenation(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """独立问题可利用历史判断语境，但检索文本不能机械拼接旧问题。"""
        build_model_payload.return_value = {"model": "test-model"}
        completion.return_value = (
            '{"standalone_question":"比较 RAG 与微调的适用场景",'
            '"target_paper_ids":[],"target_chunks":[],"needs_clarification":false,"clarification_question":""}'
        )

        plan, _ = self.agent.plan_retrieval(
            "比较 RAG 与微调的适用场景",
            [{"role": "user", "content": "你有哪些功能"}],
        )

        self.assertEqual(plan["standaloneQuestion"], "比较 RAG 与微调的适用场景")
        self.assertNotIn("你有哪些功能", plan["standaloneQuestion"])

    def test_document_scope_uses_real_parsed_files_in_authorized_candidates(self) -> None:
        """能力约束必须依据真实文件，并且不能越过调用方给定的候选范围。"""
        with TemporaryDirectory() as directory:
            parsed_path = Path(directory) / "parsed.md"
            parsed_path.write_text("正文", encoding="utf-8")
            papers = {
                "parsed": {"id": "parsed", "markdownPath": str(parsed_path)},
                "missing": {"id": "missing", "markdownPath": str(Path(directory) / "missing.md")},
                "outside": {"id": "outside", "markdownPath": str(parsed_path)},
            }
            self.agent.hunter.get_saved_paper.side_effect = papers.get

            paper_ids, diagnostics = self.agent.resolve_document_scope(
                ["parsed", "missing"],
                {"hasParsedFullText": True},
            )

        self.assertEqual(paper_ids, ["parsed"])
        self.assertEqual(diagnostics["candidatePaperCount"], 2)
        self.assertEqual(diagnostics["matchedPaperIds"], ["parsed"])

    def test_retrieve_evidence_uses_standalone_query_and_restores_target_chunk(self) -> None:
        """检索器应使用规划后的问题，并优先合并指定片段。"""
        papers = [{"id": "paper-1", "title": "Paper One"}]
        target = {"record_id": "paper-1", "chunk_index": 4, "title": "Paper One", "text": "target"}
        regular = {"record_id": "paper-1", "chunk_index": 2, "title": "Paper One", "text": "regular"}
        self.agent.hunter.list_saved_papers.return_value = papers
        self.agent.hunter.translate_search_query.side_effect = lambda value: value
        self.agent.retriever.retrieve.return_value = [regular]
        self.agent.retriever.resolve_chunk_references.return_value = [target]
        self.agent.retriever.last_diagnostics = {"queryCoverage": 0.8}

        evidence, diagnostics = self.agent.retrieve_evidence(
            "原始追问",
            history=[{"role": "user", "content": "无关旧问题"}],
            retrieval_query="独立检索问题",
            target_chunks=[{"record_id": "paper-1", "chunk_index": 4}],
        )

        self.agent.hunter.translate_search_query.assert_called_once_with("独立检索问题")
        self.assertEqual([item["chunk_index"] for item in evidence], [4, 2])
        self.assertEqual(diagnostics["resolvedChunkRefs"], [{"recordId": "paper-1", "chunkIndex": 4}])

    @patch("app.agents.research_chat_agent.ModelConfigStore.build_model_payload")
    def test_run_reuses_preselected_evidence(self, build_model_payload: Mock) -> None:
        """编排器传入证据时回答阶段不得再次执行检索。"""
        build_model_payload.return_value = {"model": "test-model"}
        evidence = [
            {
                "record_id": "paper-1",
                "title": "Paper One",
                "text": "evidence",
                "score": 1.0,
                "graph_backed": True,
                "retrieval_channels": ["graph_navigation", "original_text"],
                "graph_evidence_ids": ["evidence-1"],
                "graph_relation_ids": ["relation-1"],
                "graph_navigation_claims": ["方法 A 改善了视角鲁棒性"],
                "graph_quotes": ["原文中的图谱证据"],
            }
        ]
        self.agent._complete = Mock(return_value="结论 [1]")
        self.agent.retriever.last_retrieval_mode = "hybrid_tfidf"
        self.agent.retriever.last_diagnostics = {"selectedPaperIds": ["paper-1"]}

        result = self.agent.run("当前问题", evidence=evidence, retrieval_query="独立检索问题")

        self.agent.retriever.retrieve.assert_not_called()
        self.assertEqual(result["answer"], "结论 [1]")
        self.assertEqual(result["sources"][0]["recordId"], "paper-1")
        self.assertTrue(result["sources"][0]["graphBacked"])
        self.assertEqual(result["sources"][0]["graphQuotes"], ["原文中的图谱证据"])

    @patch("app.agents.research_chat_agent.chat_completion")
    def test_answer_generation_treats_resolved_old_claim_as_question_not_evidence(self, completion: Mock) -> None:
        """消解后的问题可以包含待核验旧命题，但最终事实只能来自本轮证据。"""
        completion.return_value = "本轮证据显示准确率为 91% [1]。"
        self.agent.retriever.build_context.return_value = "[1] 实验表格报告准确率为 91%。"
        self.agent._load_prompt = Mock(
            return_value=(BACKEND_DIR / "src" / "prompt" / "research_agent" / "zh.md").read_text(encoding="utf-8")
        )

        answer = self.agent._complete(
            model={"model": "test-model"},
            question="你之前说 95%，请重新核对",
            resolved_question="重新验证论文准确率是否为 95%",
            evidence=[{"record_id": "paper-1", "text": "准确率为 91%"}],
            answer_requirements=[],
            retrieval_state={"evidenceSufficient": True},
        )

        self.assertIn("91%", answer)
        messages = completion.call_args.args[1]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("旧回答结论，也必须由本轮知识库证据重新验证", messages[0]["content"])
        self.assertIn("重新验证论文准确率是否为 95%", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
