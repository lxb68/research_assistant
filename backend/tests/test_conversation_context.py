"""验证历史用户意图、旧回答结论与来源引用被明确隔离。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.conversation_context import ConversationContextProjector


class ConversationContextProjectorTest(unittest.TestCase):
    def test_separates_user_intents_from_unverified_prior_answers(self) -> None:
        context = ConversationContextProjector().project(
            "不对，请重新核对准确率",
            [
                {"role": "user", "content": "这篇论文准确率是多少？"},
                {
                    "role": "assistant",
                    "content": "准确率为 95% [1]",
                    "sources": [{"index": 1, "record_id": "paper-1", "chunk_index": 4}],
                },
            ],
        )

        self.assertEqual(context.usage_mode, "correction")
        self.assertEqual(context.user_intents[0]["content"], "这篇论文准确率是多少？")
        self.assertEqual(context.prior_answers[0]["trust"], "unverified_prior_answer")
        self.assertFalse(context.prior_answers[0]["allowed_as_evidence"])
        self.assertEqual(context.reference_sources[0]["record_id"], "paper-1")

    def test_model_view_never_replays_prior_answer_as_conversation_role(self) -> None:
        context = ConversationContextProjector().project(
            "这个结论可靠吗？",
            [{"role": "assistant", "content": "旧结论"}],
        )

        view = context.for_model_context()

        self.assertEqual(view["priorAnswers"][0]["content"], "旧结论")
        self.assertFalse(view["contextPolicy"]["priorAnswersAreEvidence"])
        self.assertNotIn("role", view["priorAnswers"][0])

    def test_removes_failed_turn_before_projection(self) -> None:
        context = ConversationContextProjector().project(
            "继续",
            [
                {"role": "user", "content": "失败问题"},
                {"role": "assistant", "content": "请求失败：超时"},
                {"role": "user", "content": "有效问题"},
            ],
        )

        self.assertEqual([item["content"] for item in context.user_intents], ["有效问题"])
        self.assertEqual(context.prior_answers, [])


if __name__ == "__main__":
    unittest.main()
