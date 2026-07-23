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
from app.schemas.api import DomainTreeGenerateOptions


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

    def test_heading_counts_trim_and_renumber_generated_tree(self) -> None:
        generated = [
            {"label": "1 密码学", "child": [{"label": "1.1 格密码"}, {"label": "1.2 同态加密"}]},
            {"label": "2 机器学习", "child": [{"label": "2.1 强化学习"}, {"label": "2.2 联邦学习"}]},
            {"label": "3 系统安全", "child": [{"label": "3.1 可信执行环境"}]},
        ]
        documents = [SourceDocument("paper", "格密码与机器学习", "", [], None, None, [])]

        constrained = self.agent._apply_heading_counts(
            generated,
            documents,
            primary_heading_count=2,
            secondary_heading_count=1,
        )

        self.assertEqual([node["label"] for node in constrained], ["1 密码学", "2 机器学习"])
        self.assertEqual([node["label"] for node in constrained[0]["child"]], ["1.1 格密码"])
        self.assertEqual([node["label"] for node in constrained[1]["child"]], ["2.1 强化学习"])

    def test_zero_secondary_heading_count_removes_children(self) -> None:
        generated = [{"label": "1 密码学", "child": [{"label": "1.1 格密码"}]}]
        documents = [SourceDocument("paper", "格密码", "", [], None, None, [])]

        constrained = self.agent._apply_heading_counts(
            generated,
            documents,
            primary_heading_count=1,
            secondary_heading_count=0,
        )

        self.assertNotIn("child", constrained[0])

    def test_heading_count_upper_bound_is_fifty(self) -> None:
        """API 与领域树归一化逻辑都应接受 50，并拒绝更大的请求值。"""
        options = DomainTreeGenerateOptions(
            primary_heading_count=50,
            secondary_heading_count=50,
        )
        self.assertEqual(options.primary_heading_count, 50)
        self.assertEqual(options.secondary_heading_count, 50)

        with self.assertRaises(ValueError):
            DomainTreeGenerateOptions(primary_heading_count=51)

        generated = [
            {
                "label": f"{index} Topic {index}",
                "child": [
                    {"label": f"{index}.{child_index} Detail {child_index}"}
                    for child_index in range(1, 52)
                ],
            }
            for index in range(1, 52)
        ]
        constrained = self.agent._apply_heading_counts(
            generated,
            [],
            primary_heading_count=50,
            secondary_heading_count=50,
        )
        self.assertEqual(len(constrained), 50)
        self.assertEqual(len(constrained[0]["child"]), 50)

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
            primary_heading_count=4,
            secondary_heading_count=3,
        )

        partial = self.agent.get_result("workspace")
        self.assertIsNotNone(partial)
        self.assertEqual(partial["graphStatus"], "building")
        self.assertEqual(partial["knowledgeGraph"], {})
        self.assertEqual(partial["domainTree"][0]["label"], tags[0]["label"])
        self.assertEqual(partial["domainTree"][0]["child"][0]["label"], tags[0]["child"][0]["label"])
        self.assertEqual(partial["headingCounts"], {"primary": 4, "secondary": 3})
        self.assertTrue(partial["domainTree"][0]["id"].startswith("tree:"))
        self.assertTrue(partial["domainTree"][0]["child"][0]["id"].startswith("tree:"))

        self.agent.batch_save_tags(
            "workspace",
            tags,
            {"nodes": [], "edges": []},
            documents=[document],
            catalog_text="Paper catalog",
            action="rebuild",
            language="English",
            requested_language="auto",
            primary_heading_count=4,
            secondary_heading_count=3,
            generated_at=generated_at,
        )
        complete = self.agent.get_result("workspace")
        self.assertEqual(complete["graphStatus"], "ready")
        self.assertEqual(complete["generatedAt"], generated_at)
        self.assertEqual(complete["headingCounts"], {"primary": 4, "secondary": 3})


if __name__ == "__main__":
    unittest.main()
