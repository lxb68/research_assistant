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
        completion.return_value = '{"action":"direct","answer":"你好！有什么我可以帮你的吗？"}'

        result = await OrchestratorAgent().run("你好", action="auto")

        self.assertEqual(result["action"], "direct")
        self.assertEqual(result["result"]["answer"], "你好！有什么我可以帮你的吗？")
        self.assertEqual(result["result"]["sources"], [])
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


if __name__ == "__main__":
    unittest.main()
