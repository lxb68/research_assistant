"""验证研究对话会先判断是否需要调用研究 Agent。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.orchestrator_agent import OrchestratorAgent
from app.services.run_logger import RunLogger
from app.tools.registry import ToolDefinition, ToolRegistry


class OrchestratorRoutingTest(unittest.IsolatedAsyncioTestCase):
    """覆盖直接回答和研究 Agent 两条路由。"""

    model = {
        "provider": "test",
        "protocol": "openai_compatible",
        "base_url": "http://model.test/v1",
        "api_key": "",
        "model": "test-model",
    }

    @patch("app.agents.orchestrator_agent.ResearchChatAgent")
    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_greeting_is_answered_without_research_agent(
        self,
        build_model_payload: Mock,
        completion: Mock,
        research_agent: Mock,
    ) -> None:
        """普通寒暄应直接回答，不能触发论文检索或下载流程。"""
        build_model_payload.return_value = self.model
        completion.side_effect = [
            '{"action":"direct"}',
            "你好！有什么我可以帮你的吗？",
        ]

        result = await OrchestratorAgent().run("你好", action="auto")

        self.assertEqual(result["action"], "direct")
        self.assertEqual(result["result"]["answer"], "你好！有什么我可以帮你的吗？")
        self.assertEqual(result["result"]["sources"], [])
        self.assertEqual(completion.call_count, 2)
        self.assertEqual(completion.call_args_list[0].kwargs["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", completion.call_args_list[1].kwargs)
        research_agent.assert_not_called()

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_research_question_is_delegated_to_research_pipeline(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """需要论文证据的问题应进入研究流水线。"""
        build_model_payload.return_value = self.model
        completion.return_value = '{"action":"chat","answer":""}'
        agent = OrchestratorAgent()
        agent._run_research_pipeline = AsyncMock(return_value={"action": "chat", "result": {"answer": "研究回答"}})

        result = await agent.run("请基于论文比较 RAG 与微调", action="auto")

        self.assertEqual(result["action"], "chat")
        agent._run_research_pipeline.assert_awaited_once()

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_auto_loop_can_move_from_tool_observation_to_research_agent(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """工具只负责产生观察，下一轮应能转交研究 Agent，而不是被锁死在工具循环。"""
        build_model_payload.return_value = self.model
        completion.side_effect = [
            '{"action":"tool","toolName":"get_knowledge_base_paper",'
            '"arguments":{"record_id":"paper-1"}}',
            '{"action":"chat","arguments":{"paper_ids":["paper-1"]}}',
        ]
        detail_handler = Mock(
            return_value={
                "paper": {
                    "recordId": "paper-1",
                    "title": "Composite Polynomial Comparison",
                    "hasPdf": True,
                    "hasAbstract": True,
                    "hasParsedFullText": False,
                }
            }
        )
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "get_knowledge_base_paper",
                "读取论文详情和全文状态。",
                {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                    "required": ["record_id"],
                    "additionalProperties": False,
                },
                detail_handler,
            )
        )
        agent = OrchestratorAgent(tool_registry=registry)
        agent._run_research_pipeline = AsyncMock(
            return_value={"action": "chat", "result": {"answer": "全文研究回答"}}
        )

        result = await agent.run("请详细说明这个方法", action="auto", arguments={"history": []})

        self.assertEqual(result["action"], "chat")
        detail_handler.assert_called_once_with({"record_id": "paper-1"})
        pipeline_args = agent._run_research_pipeline.await_args.args[1]
        self.assertEqual(pipeline_args["paper_ids"], ["paper-1"])
        second_router_messages = completion.call_args_list[1].args[1]
        self.assertIn("hasParsedFullText", second_router_messages[-2]["content"])

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_auto_loop_recovers_after_router_protocol_error_without_fixing_final_route(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """路由修复即使先选择工具，也应在观察后重新规划，而非把工具当作终点。"""
        build_model_payload.return_value = self.model
        completion.side_effect = [
            "这是一段越权生成的摘要回答。",
            '{"action":"tool","toolName":"get_knowledge_base_paper",'
            '"arguments":{"record_id":"paper-1"}}',
            '{"action":"chat","arguments":{"paper_ids":["paper-1"]}}',
        ]
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "get_knowledge_base_paper",
                "读取论文详情。",
                {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                    "required": ["record_id"],
                    "additionalProperties": False,
                },
                Mock(return_value={"paper": {"recordId": "paper-1", "hasParsedFullText": True}}),
            )
        )
        agent = OrchestratorAgent(tool_registry=registry)
        agent._run_research_pipeline = AsyncMock(
            return_value={"action": "chat", "result": {"answer": "研究回答"}}
        )

        result = await agent.run("详细说明本文方法", action="auto")

        self.assertEqual(result["action"], "chat")
        self.assertEqual(completion.call_count, 3)
        agent._run_research_pipeline.assert_awaited_once()

    @patch("app.agents.orchestrator_agent.HunterAgent")
    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_auto_loop_can_index_local_pdf_then_continue_to_research_agent(
        self,
        build_model_payload: Mock,
        completion: Mock,
        hunter_agent_class: Mock,
    ) -> None:
        """全文不可检索但 PDF 存在时，规划器可调用索引 Agent，并在观察后继续研究。"""
        build_model_payload.return_value = self.model
        completion.side_effect = [
            '{"action":"agent","agentName":"local_pdf_indexer",'
            '"arguments":{"record_id":"paper-1"}}',
            '{"action":"agent","agentName":"research_chat",'
            '"arguments":{"paper_ids":["paper-1"]}}',
        ]
        hunter_agent_class.return_value.index_saved_pdf_text.return_value = {
            "id": "paper-1",
            "title": "Composite Polynomial Comparison",
            "markdownPath": "C:/managed/paper-1/full.md",
            "splitChunkCount": 12,
            "fullTextIndexedBy": "pymupdf",
        }
        agent = OrchestratorAgent()
        agent._run_research_pipeline = AsyncMock(
            return_value={"action": "chat", "result": {"answer": "基于全文的研究回答"}}
        )

        result = await agent.run("请详细解释论文方法", action="auto")

        self.assertEqual(result["action"], "chat")
        hunter_agent_class.return_value.index_saved_pdf_text.assert_called_once()
        pipeline_args = agent._run_research_pipeline.await_args.args[1]
        self.assertEqual(pipeline_args["paper_ids"], ["paper-1"])
        second_router_messages = completion.call_args_list[1].args[1]
        self.assertIn("splitChunkCount", second_router_messages[-2]["content"])

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_router_repairs_natural_language_answer_for_research_followup(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """路由器越权回答研究追问时应只修复一次，并继续进入 chat 流程。"""
        build_model_payload.return_value = self.model
        completion.side_effect = [
            "根据现有知识库片段，Squirrel 使用安全两方计算，MPlookup 使用多方查找论证。",
            '{"action":"chat"}',
        ]
        history = [
            {
                "role": "assistant",
                "content": "Squirrel 与 MPlookup 都属于安全计算领域，但技术路线不同。",
                "sources": [
                    {"index": 1, "record_id": "paper-squirrel", "chunk_index": 23},
                    {"index": 2, "record_id": "paper-mplookup", "chunk_index": 41},
                ],
            }
        ]
        agent = OrchestratorAgent()
        agent.run_logger = Mock()

        decision = await agent._route_task("两者分别是什么技术路线", {"history": history})

        self.assertEqual(decision["action"], "chat")
        self.assertEqual(completion.call_count, 2)
        self.assertEqual(completion.call_args_list[0].kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(completion.call_args_list[1].kwargs["response_format"], {"type": "json_object"})
        router_messages = completion.call_args_list[0].args[1]
        self.assertFalse(any(message["role"] == "assistant" for message in router_messages))
        self.assertEqual(router_messages[-2]["role"], "user")
        self.assertIn("unverified_prior_answer", router_messages[-2]["content"])
        self.assertIn('"priorAnswersAreEvidence": false', router_messages[-2]["content"])
        repair_payload = completion.call_args_list[1].args[1][1]["content"]
        self.assertIn("两者分别是什么技术路线", repair_payload)
        self.assertIn("unverified_prior_answer", repair_payload)
        events = [call.kwargs.get("event") for call in agent.run_logger.log.call_args_list]
        self.assertIn("intent_routing_parse_error", events)
        self.assertIn("intent_routing_repair_raw_response", events)
        self.assertIn("intent_routing", events)

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_direct_answer_receives_prior_answer_as_unverified_structured_context(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """普通直答可读取待变换文本，但不能把旧回答回放成可信 assistant 消息。"""
        build_model_payload.return_value = self.model
        completion.return_value = "Translated answer"

        answer = await OrchestratorAgent()._answer_direct(
            "把刚才的回答翻译成英文",
            {
                "history": [
                    {"role": "user", "content": "请解释这个概念"},
                    {"role": "assistant", "content": "未经核验的旧回答"},
                ]
            },
        )

        self.assertEqual(answer, "Translated answer")
        messages = completion.call_args.args[1]
        self.assertFalse(any(message["role"] == "assistant" for message in messages))
        self.assertEqual(messages[-2]["role"], "user")
        self.assertIn("未经核验的旧回答", messages[-2]["content"])
        self.assertIn('"usageMode": "transform"', messages[-2]["content"])
        self.assertIn('"priorAnswersAreEvidence": false', messages[-2]["content"])

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_router_prompt_only_classifies_and_never_answers(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """路由 prompt 必须保持单一分类职责。"""
        build_model_payload.return_value = self.model
        completion.return_value = '{"action":"chat"}'

        await OrchestratorAgent()._route_task("研究问题", {})

        system_prompt = completion.call_args.args[1][0]["content"]
        self.assertIn("只负责选择动作", system_prompt)
        self.assertIn("不负责回答用户问题", system_prompt)
        self.assertNotIn("请直接给出自然", system_prompt)

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_router_repairs_empty_json_mode_response_once(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """JSON Output 返回空内容时应进入同一次修复路径。"""
        build_model_payload.return_value = self.model
        completion.side_effect = [RuntimeError("模型返回了空回答"), '{"action":"chat"}']
        agent = OrchestratorAgent()
        agent.run_logger = Mock()

        decision = await agent._route_task("研究问题", {})

        self.assertEqual(decision["action"], "chat")
        self.assertEqual(completion.call_count, 2)
        events = [call.kwargs.get("event") for call in agent.run_logger.log.call_args_list]
        self.assertIn("intent_routing_empty_response", events)
        self.assertIn("intent_routing_repair_raw_response", events)

    @patch("app.agents.orchestrator_agent.ResearchChatAgent")
    async def test_pipeline_uses_context_plan_for_targeted_retrieval(self, research_agent_class: Mock) -> None:
        """编排器应把独立问题、目标论文和目标片段交给检索器并复用证据。"""
        research_agent = research_agent_class.return_value
        plan = {
            "standaloneQuestion": "Squirrel 实验部分的通信开销如何？",
            "targetPaperIds": ["paper-3"],
            "targetChunks": [{"record_id": "paper-3", "chunk_index": 7}],
            "needsClarification": False,
            "clarificationQuestion": "",
        }
        evidence = [
            {"record_id": "paper-3", "chunk_index": 7, "title": "Squirrel", "text": "target", "score": 1.0},
            {"record_id": "paper-3", "chunk_index": 8, "title": "Squirrel", "text": "context", "score": 0.8},
        ]
        diagnostics = {
            "paperCount": 1,
            "evidenceCount": 2,
            "distinctPaperCount": 1,
            "selectedPaperIds": ["paper-3"],
            "requestedChunkRefs": [{"recordId": "paper-3", "chunkIndex": 7}],
            "resolvedChunkRefs": [{"recordId": "paper-3", "chunkIndex": 7}],
            "queryCoverage": 0.8,
        }
        research_agent.plan_retrieval.return_value = (plan, '{"standalone_question":"..."}')
        research_agent.retrieve_evidence.return_value = (evidence, diagnostics)
        research_agent.run.return_value = {"answer": "回答 [1]", "sources": []}
        agent = OrchestratorAgent()
        agent.run_logger = Mock()

        result = await agent._run_research_pipeline(
            "这个片段的开销呢？",
            {
                "history": [
                    {
                        "role": "assistant",
                        "content": "实验结果 [1]",
                        "sources": [{"index": 1, "record_id": "paper-3", "chunk_index": 7}],
                    }
                ]
            },
        )

        self.assertEqual(result["action"], "chat")
        research_agent.retrieve_evidence.assert_called_once_with(
            "这个片段的开销呢？",
            history=agent._clean_history(
                [
                    {
                        "role": "assistant",
                        "content": "实验结果 [1]",
                        "sources": [{"index": 1, "record_id": "paper-3", "chunk_index": 7}],
                    }
                ]
            ),
            paper_ids=["paper-3"],
            retrieval_query="Squirrel 实验部分的通信开销如何？",
            target_chunks=[{"record_id": "paper-3", "chunk_index": 7}],
            retrieval_facets=[],
            question_type="simple_fact",
            target_evidence_count=2,
            graph_project_id="workspace-domain-tree",
        )
        self.assertEqual(research_agent.run.call_args.kwargs["evidence"], evidence)

    @patch("app.agents.orchestrator_agent.ResearchChatAgent")
    async def test_pipeline_filters_capability_scope_before_evidence_validation(
        self,
        research_agent_class: Mock,
    ) -> None:
        """项目授权范围不能被误当成需要逐篇覆盖的证据目标。"""
        research_agent = research_agent_class.return_value
        research_agent.plan_retrieval.return_value = (
            {
                "standaloneQuestion": "介绍有完整全文并解析的文献",
                "targetPaperIds": ["parsed-a", "unparsed", "parsed-b"],
                "targetChunks": [],
                "documentRequirements": {"hasParsedFullText": True},
                "needsClarification": False,
            },
            "{}",
        )
        research_agent.resolve_document_scope.return_value = (
            ["parsed-a", "parsed-b"],
            {
                "requirements": {"hasParsedFullText": True},
                "candidatePaperCount": 3,
                "matchedPaperCount": 2,
                "matchedPaperIds": ["parsed-a", "parsed-b"],
            },
        )
        evidence = [
            {"record_id": "parsed-a", "chunk_index": 1, "title": "A", "text": "A", "score": 1.0},
            {"record_id": "parsed-b", "chunk_index": 1, "title": "B", "text": "B", "score": 1.0},
        ]
        research_agent.retrieve_evidence.return_value = (
            evidence,
            {"paperCount": 2, "evidenceCount": 2, "selectedPaperIds": ["parsed-a", "parsed-b"]},
        )
        research_agent.run.return_value = {"answer": "回答 [1][2]", "sources": []}
        agent = OrchestratorAgent()
        agent.run_logger = Mock()
        agent._evaluate_retrieved_evidence = AsyncMock(return_value={"sufficient": True, "reasons": []})

        result = await agent._run_research_pipeline(
            "介绍有完整全文并解析的文献",
            {"project_paper_ids": ["parsed-a", "unparsed", "parsed-b"], "allow_external_search": False},
        )

        self.assertEqual(result["action"], "chat")
        self.assertEqual(research_agent.retrieve_evidence.call_args.kwargs["paper_ids"], ["parsed-a", "parsed-b"])
        self.assertEqual(
            agent._evaluate_retrieved_evidence.call_args.kwargs["required_paper_ids"],
            ["parsed-a", "parsed-b"],
        )
        self.assertEqual(
            agent._evaluate_retrieved_evidence.call_args.kwargs["plan"]["targetPaperIds"],
            ["parsed-a", "parsed-b"],
        )

    @patch("app.agents.orchestrator_agent.ResearchChatAgent")
    async def test_pipeline_stops_when_context_plan_needs_clarification(self, research_agent_class: Mock) -> None:
        """指代不唯一时应请求澄清，不能退化为全库盲检索。"""
        research_agent = research_agent_class.return_value
        research_agent.plan_retrieval.return_value = (
            {
                "standaloneQuestion": "它的方法有什么局限？",
                "targetPaperIds": [],
                "targetChunks": [],
                "needsClarification": True,
                "clarificationQuestion": "你指的是 MPlookup 还是 Squirrel？",
            },
            "{}",
        )
        agent = OrchestratorAgent()
        agent.run_logger = Mock()

        result = await agent._run_research_pipeline("它的方法有什么局限？", {"history": []})

        self.assertEqual(result["action"], "request_user_action")
        self.assertEqual(result["result"]["message"], "你指的是 MPlookup 还是 Squirrel？")
        research_agent.retrieve_evidence.assert_not_called()

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_router_logs_truncated_raw_response_before_parsing(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """路由器应在解析前记录经过长度限制的模型原始响应。"""
        build_model_payload.return_value = self.model
        raw_response = '{"action":"direct","answer":"' + ("x" * 2100) + '"}'
        completion.return_value = raw_response
        agent = OrchestratorAgent()
        agent.run_logger = Mock()

        decision = await agent._route_task("测试路由日志", {})

        self.assertEqual(decision["action"], "direct")
        raw_log = next(
            call for call in agent.run_logger.log.call_args_list
            if call.kwargs.get("event") == "intent_routing_raw_response"
        )
        self.assertEqual(len(raw_log.kwargs["data"]["rawResponsePreview"]), agent.ROUTER_RAW_LOG_LIMIT)
        self.assertEqual(raw_log.kwargs["data"]["responseLength"], len(raw_response))
        self.assertTrue(raw_log.kwargs["data"]["truncated"])
        events = [call.kwargs.get("event") for call in agent.run_logger.log.call_args_list]
        self.assertLess(events.index("intent_routing_raw_response"), events.index("intent_routing"))

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_router_logs_parse_error_and_preserves_exception(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """模型响应无法解析时应记录错误上下文，并继续抛出原异常。"""
        build_model_payload.return_value = self.model
        completion.return_value = "这不是 JSON"
        agent = OrchestratorAgent()
        agent.run_logger = Mock()

        with self.assertRaisesRegex(ValueError, "有效的意图路由结果"):
            await agent._route_task("测试错误日志", {})

        error_log = next(
            call for call in agent.run_logger.log.call_args_list
            if call.kwargs.get("event") == "intent_routing_parse_error"
        )
        self.assertEqual(error_log.kwargs["data"]["errorType"], "ValueError")
        self.assertEqual(error_log.kwargs["data"]["rawResponsePreview"], "这不是 JSON")
        self.assertFalse(error_log.kwargs["data"]["truncated"])
        self.assertEqual(completion.call_count, 2)
        events = [call.kwargs.get("event") for call in agent.run_logger.log.call_args_list]
        self.assertIn("intent_routing_repair_error", events)

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_explicit_direct_action_only_calls_answer_model(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """显式 direct 不需要路由调用，只执行独立回答阶段。"""
        build_model_payload.return_value = self.model
        completion.return_value = "直接回答"

        result = await OrchestratorAgent().run("你好", action="direct")

        self.assertEqual(result["result"]["answer"], "直接回答")
        self.assertEqual(completion.call_count, 1)
        self.assertNotIn("response_format", completion.call_args.kwargs)

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_router_raw_response_log_is_redacted_on_disk(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """模型原始响应中的常见凭据格式不得原样写入日志文件。"""
        build_model_payload.return_value = self.model
        secret = "sk-sensitive-route-token"
        completion.return_value = f"无效响应 {secret}"

        with TemporaryDirectory() as log_dir:
            agent = OrchestratorAgent()
            agent.run_logger = RunLogger(log_dir)
            with self.assertRaisesRegex(ValueError, "有效的意图路由结果"):
                await agent._route_task("测试日志脱敏", {})
            log_content = agent.run_logger.jsonl_path.read_text(encoding="utf-8")

        self.assertNotIn(secret, log_content)
        self.assertIn("sk-***", log_content)

    def test_router_rejects_unregistered_action(self) -> None:
        """模型不能借由路由结果调用未注册工具。"""
        with self.assertRaisesRegex(ValueError, "未注册"):
            OrchestratorAgent()._parse_route_decision('{"action":"shell","answer":""}')

    def test_evidence_assessment_requires_every_selected_paper(self) -> None:
        """指定文献缺少任意一篇证据时不得判定为充分。"""
        diagnostics = {
            "evidenceCount": 3,
            "distinctPaperCount": 2,
            "queryCoverage": 0.8,
            "selectedPaperIds": ["paper-1", "paper-2"],
        }

        sufficient, reasons = OrchestratorAgent()._assess_evidence(
            diagnostics,
            required_paper_ids=["paper-1", "paper-3"],
        )

        self.assertFalse(sufficient)
        self.assertTrue(any("1 篇未检索到有效证据" in reason for reason in reasons))

    def test_clean_history_removes_transient_frontend_errors(self) -> None:
        """前端失败占位消息不得进入后续 Agent 上下文。"""
        history = [
            {"role": "user", "content": "第一次提问"},
            {"role": "assistant", "content": "请求失败：模型未返回有效结果"},
            {"role": "user", "content": "第二次提问"},
            {
                "role": "assistant",
                "content": "正常回答",
                "sources": [{"index": 1, "record_id": "paper-1", "chunk_index": 2}],
            },
        ]

        cleaned = OrchestratorAgent()._clean_history(history)

        self.assertEqual(
            cleaned,
            [
                {"role": "user", "content": "第二次提问"},
                {
                    "role": "assistant",
                    "content": "正常回答",
                    "sources": [{"index": 1, "record_id": "paper-1", "chunk_index": 2}],
                },
            ],
        )

    def test_run_logger_preserves_non_secret_token_metrics(self) -> None:
        """Token 统计和检索关键词不应被当作凭据字段脱敏。"""
        logger = RunLogger.__new__(RunLogger)
        redacted = logger._redact_value(
            {
                "averageChunkTokens": 512,
                "maxChunkTokens": 700,
                "tokenCount": 128,
                "searchKeyword": "secure computation",
                "accessToken": "sensitive",
            }
        )

        self.assertEqual(redacted["averageChunkTokens"], 512)
        self.assertEqual(redacted["maxChunkTokens"], 700)
        self.assertEqual(redacted["tokenCount"], 128)
        self.assertEqual(redacted["searchKeyword"], "secure computation")
        self.assertEqual(redacted["accessToken"], "***")


if __name__ == "__main__":
    unittest.main()
