"""验证只读工具的有界行动—观察循环。"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.tool_loop_agent import ObservationReducer, ToolLoopAgent
from app.services.task_control import TaskCancelled
from app.tools.registry import ToolDefinition, ToolRegistry


class ToolLoopAgentTest(unittest.IsolatedAsyncioTestCase):
    """覆盖多步工具衔接、重复调用保护和观察压缩。"""

    model = {"provider": "test", "protocol": "openai_compatible", "model": "test"}

    @staticmethod
    def _paper_registry(list_handler: Mock, detail_handler: Mock) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "list_knowledge_base_papers",
                "按标题定位论文并返回 recordId；列表不返回摘要，找到目标后继续读取详情。",
                {
                    "type": "object",
                    "properties": {"keyword": {"type": "string"}},
                    "required": ["keyword"],
                    "additionalProperties": False,
                },
                list_handler,
            )
        )
        registry.register(
            ToolDefinition(
                "get_knowledge_base_paper",
                "按 recordId 获取摘要、PDF 和解析全文状态。",
                {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                    "required": ["record_id"],
                    "additionalProperties": False,
                },
                detail_handler,
            )
        )
        return registry

    async def test_uses_observation_to_call_a_second_tool(self) -> None:
        """获得 recordId 后应继续读取详情，再基于摘要完成回答。"""
        list_handler = Mock(
            return_value={
                "total": 1,
                "items": [
                    {
                        "recordId": "paper-1",
                        "title": "Efficient Homomorphic Comparison",
                        "hasAbstract": True,
                        "hasParsedFullText": False,
                    }
                ],
            }
        )
        detail_handler = Mock(
            return_value={
                "paper": {
                    "recordId": "paper-1",
                    "abstract": "The paper proposes composite polynomial comparison methods.",
                    "hasAbstract": True,
                    "hasParsedFullText": False,
                }
            }
        )
        completion = Mock(
            side_effect=[
                '{"action":"tool","toolName":"get_knowledge_base_paper",'
                '"arguments":{"record_id":"paper-1"},"reason":"需要摘要"}',
                '{"action":"final","answer":"论文提出复合多项式同态比较方法。",'
                '"limitations":["依据摘要，尚无解析全文"]}',
            ]
        )
        loop = ToolLoopAgent(
            self._paper_registry(list_handler, detail_handler),
            model=self.model,
            completion=completion,
        )

        result = await loop.run(
            "这篇论文讲了什么",
            initial_tool_name="list_knowledge_base_papers",
            initial_arguments={"keyword": "Efficient Homomorphic Comparison"},
        )

        self.assertTrue(result["answer"].startswith("论文提出复合多项式同态比较方法。"))
        self.assertIn("依据摘要，尚无解析全文", result["answer"])
        self.assertEqual(result["steps"], 2)
        self.assertEqual([item["toolName"] for item in result["toolTrace"]], [
            "list_knowledge_base_papers",
            "get_knowledge_base_paper",
        ])
        list_handler.assert_called_once()
        detail_handler.assert_called_once_with({"record_id": "paper-1"})

    async def test_blocks_repeated_identical_tool_call(self) -> None:
        """相同工具和参数不得重复执行，但模型仍能根据错误观察结束回答。"""
        handler = Mock(return_value={"total": 0, "items": []})
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "list_knowledge_base_papers",
                "列出论文",
                {
                    "type": "object",
                    "properties": {"keyword": {"type": "string"}},
                    "required": ["keyword"],
                    "additionalProperties": False,
                },
                handler,
            )
        )
        repeated = (
            '{"action":"tool","toolName":"list_knowledge_base_papers",'
            '"arguments":{"keyword":"missing"},"reason":"再次确认"}'
        )
        completion = Mock(
            side_effect=[
                repeated,
                '{"action":"final","answer":"知识库中未找到该论文。","limitations":[]}',
            ]
        )
        loop = ToolLoopAgent(registry, model=self.model, completion=completion)

        result = await loop.run(
            "查找论文",
            initial_tool_name="list_knowledge_base_papers",
            initial_arguments={"keyword": "missing"},
        )

        self.assertEqual(handler.call_count, 1)
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["toolTrace"][1]["errorType"], "RepeatedToolCall")

    async def test_forces_final_answer_after_max_steps(self) -> None:
        """达到工具步数上限后必须停止行动并基于已有观察回答。"""
        handler = Mock(return_value={"value": 1})
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "read_value",
                "读取值",
                {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
                handler,
            )
        )
        completion = Mock(
            side_effect=[
                '{"action":"tool","toolName":"read_value","arguments":{"key":"next"}}',
                '{"action":"final","answer":"目前只确认值为 1。","limitations":["达到工具调用上限"]}',
            ]
        )
        loop = ToolLoopAgent(registry, model=self.model, completion=completion, max_steps=1)

        result = await loop.run(
            "读取值",
            initial_tool_name="read_value",
            initial_arguments={"key": "first"},
        )

        self.assertEqual(handler.call_count, 1)
        self.assertEqual(result["stopReason"], "max_steps")
        self.assertEqual(result["steps"], 1)

    async def test_honors_cancellation_before_tool_execution(self) -> None:
        """取消信号必须在执行任何工具前终止循环。"""
        handler = Mock(return_value={})
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "safe_tool",
                "只读工具",
                {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                handler,
            )
        )
        cancel_event = threading.Event()
        cancel_event.set()
        loop = ToolLoopAgent(registry, model=self.model, completion=Mock())

        with self.assertRaises(TaskCancelled):
            await loop.run(
                "读取数据",
                initial_tool_name="safe_tool",
                initial_arguments={},
                cancel_event=cancel_event,
            )

        handler.assert_not_called()

    def test_observation_reducer_limits_long_tool_results(self) -> None:
        """观察压缩应限制长文本和大列表，同时保留截断诊断。"""
        reducer = ObservationReducer(max_items=2, max_string_chars=100)

        reduced = reducer.reduce({"items": [1, 2, 3], "abstract": "x" * 150})

        self.assertEqual(reduced["items"][-1], {"_truncatedItems": 1})
        self.assertIn("长文本已截断", reduced["abstract"])


if __name__ == "__main__":
    unittest.main()
