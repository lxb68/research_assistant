"""验证领域树显式语言与跟随文献语言模式。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.domainTree_agent import DomainTreeAgent, SourceDocument


class DomainTreeLanguageTest(unittest.TestCase):
    """自动检测应按文献主要语言选择对应提示词。"""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.agent = DomainTreeAgent(
            storage_dir=self.directory.name,
            metadata_db_path=Path(self.directory.name) / "missing.db",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_auto_detects_english_literature(self) -> None:
        source = "Secure Multi-Party Computation\nGradient Boosting Decision Trees\nPrivacy Protocol"
        self.assertEqual(self.agent._resolve_analysis_language("auto", source), "English")

    def test_auto_detects_chinese_literature(self) -> None:
        source = "隐私计算与安全多方计算研究\n本文提出一种面向梯度提升树的高效训练协议。"
        self.assertEqual(self.agent._resolve_analysis_language("auto", source), "中文")

    def test_explicit_language_overrides_source_language(self) -> None:
        self.assertEqual(self.agent._resolve_analysis_language("中文", "English paper"), "中文")
        self.assertEqual(self.agent._resolve_analysis_language("English", "中文文献"), "English")

    def test_domain_tree_snapshot_is_visible_before_graph_is_ready(self) -> None:
        """领域树快照应先可读取，并隐藏上一轮可能残留的知识图谱。"""
        document = SourceDocument("paper", "Paper", "", [], None, None, [])
        tags = [{"label": "1 Security", "child": [{"label": "1.1 MPC"}]}]
        generated_at = self.agent.save_domain_tree_snapshot(
            "workspace",
            tags,
            documents=[document],
            catalog_text="Paper catalog",
            action="rebuild",
            language="English",
            requested_language="auto",
        )

        partial = self.agent.get_result("workspace")
        self.assertIsNotNone(partial)
        self.assertEqual(partial["graphStatus"], "building")
        self.assertEqual(partial["knowledgeGraph"], {})
        self.assertEqual(partial["domainTree"], tags)

        self.agent.batch_save_tags(
            "workspace",
            tags,
            {"nodes": [], "edges": []},
            documents=[document],
            catalog_text="Paper catalog",
            action="rebuild",
            language="English",
            requested_language="auto",
            generated_at=generated_at,
        )
        complete = self.agent.get_result("workspace")
        self.assertEqual(complete["graphStatus"], "ready")
        self.assertEqual(complete["generatedAt"], generated_at)


if __name__ == "__main__":
    unittest.main()
