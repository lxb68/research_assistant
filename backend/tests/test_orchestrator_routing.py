"""验证研究对话会先判断是否需要调用研究 Agent。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.orchestrator_agent import OrchestratorAgent


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

    def test_router_rejects_unregistered_action(self) -> None:
        """模型不能借由路由结果调用未注册工具。"""
        with self.assertRaisesRegex(ValueError, "未注册"):
            OrchestratorAgent()._parse_route_decision('{"action":"shell","answer":""}')


if __name__ == "__main__":
    unittest.main()
