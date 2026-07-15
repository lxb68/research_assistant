"""验证全文语义抽取、证据回定位和引用解析。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

# 允许从仓库根目录直接执行 unittest discover。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.semantic_graph import SemanticGraphExtractor, SemanticSourceDocument
from app.agents.domainTree_agent import DomainTreeAgent, SourceDocument


class SemanticGraphExtractorTest(unittest.TestCase):
    """覆盖语义抽取主链路中不依赖真实模型服务的行为。"""

    def test_extracts_entities_relations_evidence_and_citations(self) -> None:
        """实体关系必须携带可回定位证据，引用必须连接正文标记。"""
        markdown = """# Example Paper

## Method

Method A improves Dataset B accuracy to 95%. Prior work is described in [1, 2].

## References

1. Smith, J.: Earlier Method. Journal 10, 1-8 (2020). https://doi.org/10.1000/test

2. Doe, A.: Another Study. Conference (2021)
"""
        model_payload = {
            "entities": [
                {
                    "localId": "e1",
                    "name": "Method A",
                    "canonicalName": "Method A",
                    "type": "方法",
                    "aliases": [],
                    "attributes": [],
                    "evidenceQuote": "Method A improves Dataset B accuracy to 95%.",
                },
                {
                    "localId": "e2",
                    "name": "Dataset B",
                    "canonicalName": "Dataset B",
                    "type": "数据集",
                    "aliases": [],
                    "attributes": [{"name": "accuracy", "value": "95%", "unit": ""}],
                    "evidenceQuote": "Method A improves Dataset B accuracy to 95%.",
                },
            ],
            "relations": [
                {
                    "source": "e1",
                    "target": "e2",
                    "predicate": "提高准确率",
                    "relationType": "experimental",
                    "confidence": 0.93,
                    "evidenceQuote": "Method A improves Dataset B accuracy to 95%.",
                }
            ],
        }

        def fake_chat(*args: object, **kwargs: object) -> str:
            """返回固定 JSON，避免测试访问外部模型。"""
            return json.dumps(model_payload)

        with tempfile.TemporaryDirectory() as directory:
            markdown_path = Path(directory) / "paper.md"
            markdown_path.write_text(markdown, encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test", "base_url": "http://localhost", "provider": "custom"},
                chat_fn=fake_chat,
            )
            result = extractor.extract(
                [SemanticSourceDocument("paper-1", "Example Paper", markdown_path)]
            )

        self.assertEqual(len(result["entities"]), 2)
        self.assertEqual(len(result["semanticRelations"]), 1)
        self.assertEqual(len(result["citations"]), 2)
        self.assertTrue(result["citations"][0]["contexts"])
        self.assertEqual(result["citations"][0]["doi"], "10.1000/test")
        relation = result["semanticRelations"][0]
        self.assertEqual(relation["relationType"], "experimental")
        self.assertTrue(relation["evidenceIds"])
        self.assertIn(relation["evidenceIds"][0], {item["id"] for item in result["evidence"]})

    def test_discards_relation_when_quote_is_not_in_source(self) -> None:
        """模型虚构的证据无法回定位时，不得进入最终关系图。"""
        payload = {
            "entities": [
                {"localId": "a", "name": "A", "canonicalName": "A", "type": "方法"},
                {"localId": "b", "name": "B", "canonicalName": "B", "type": "数据集"},
            ],
            "relations": [
                {
                    "source": "a",
                    "target": "b",
                    "predicate": "导致",
                    "relationType": "causal",
                    "confidence": 1,
                    "evidenceQuote": "这句话并不存在于原文",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Paper\n\n## Result\n\nA is evaluated on B.", encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test"},
                chat_fn=lambda *args, **kwargs: json.dumps(payload),
            )
            result = extractor.extract([SemanticSourceDocument("p", "Paper", path)])

        self.assertEqual(result["semanticRelations"], [])

    def test_expands_citation_ranges(self) -> None:
        """连续引用编号应展开为每一条独立引用。"""
        extractor = SemanticGraphExtractor(None)
        contexts = extractor._find_inline_citation_contexts("Result follows prior work [2-4, 7].")
        self.assertEqual(sorted(contexts), [2, 3, 4, 7])

    def test_parses_unnumbered_author_year_references(self) -> None:
        """无编号的作者—年份制参考文献也应建立正文上下文。"""
        extractor = SemanticGraphExtractor(None)
        body = "## Related Work\n\nSmith et al. (2020) introduced the baseline."
        references = "Smith, J. (2020). A Useful Baseline. Journal of Tests."
        citations = extractor.parse_citations(
            SemanticSourceDocument("paper", "Paper", None),
            body,
            references,
            reference_start_line=5,
            local_titles={},
        )
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["year"], 2020)
        self.assertTrue(citations[0]["contexts"])

    def test_retries_timeout_and_reports_chunk_progress(self) -> None:
        """语义分块超时应有限重试，并持续上报可观察进度。"""
        calls = 0
        updates: list[dict] = []

        def flaky_chat(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise requests.ReadTimeout("temporary timeout")
            return json.dumps({"entities": [], "relations": []})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Method\n\nA short research method description.", encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test"},
                chat_fn=flaky_chat,
                progress_callback=updates.append,
            )
            with (
                patch("app.services.semantic_graph.settings.domain_tree_retry_attempts", 2),
                patch("app.services.semantic_graph.settings.domain_tree_retry_base_delay_seconds", 0),
            ):
                result = extractor.extract([SemanticSourceDocument("p", "Paper", path)])

        self.assertEqual(calls, 2)
        self.assertEqual(result["extraction"]["processedChunkCount"], 1)
        self.assertEqual(updates[-1]["completedChunks"], 1)

    def test_cancelled_extraction_stops_before_model_call(self) -> None:
        """已取消任务不得继续启动新的模型请求。"""
        cancel_event = threading.Event()
        cancel_event.set()
        extractor = SemanticGraphExtractor({"model": "test"}, cancel_event=cancel_event)

        from app.services.task_control import DomainTreeGenerationCancelled

        with self.assertRaises(DomainTreeGenerationCancelled):
            extractor.extract([])


class DomainTreeSemanticIntegrationTest(unittest.TestCase):
    """验证全文语义结果会被并入原有领域知识图谱。"""

    @patch("app.agents.domainTree_agent.SemanticGraphExtractor.extract")
    def test_merges_semantic_nodes_and_edges(self, extract: object) -> None:
        """实体、语义关系和引用应生成兼容现有前端的节点与边。"""
        extract.return_value = {
            "entities": [
                {
                    "id": "entity:a",
                    "name": "方法 A",
                    "type": "方法",
                    "aliases": [],
                    "attributes": [],
                    "evidenceIds": ["evidence:1"],
                    "documentIds": ["paper-1"],
                },
                {
                    "id": "entity:b",
                    "name": "数据集 B",
                    "type": "数据集",
                    "aliases": [],
                    "attributes": [],
                    "evidenceIds": ["evidence:1"],
                    "documentIds": ["paper-1"],
                },
            ],
            "semanticRelations": [
                {
                    "id": "relation:1",
                    "source": "entity:a",
                    "target": "entity:b",
                    "predicate": "评测于",
                    "relationType": "experimental",
                    "confidence": 0.9,
                    "evidenceIds": ["evidence:1"],
                    "documentIds": ["paper-1"],
                }
            ],
            "citations": [
                {
                    "id": "citation:paper-1:1",
                    "documentId": "paper-1",
                    "referenceNumber": 1,
                    "title": "参考论文",
                    "rawReference": "Author: 参考论文 (2020)",
                    "contexts": [],
                }
            ],
            "evidence": [{"id": "evidence:1", "documentId": "paper-1", "quote": "证据"}],
            "extraction": {"entityCount": 2, "semanticRelationCount": 1, "citationCount": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory, metadata_db_path=Path(directory) / "missing.db")
            graph = agent._build_knowledge_graph(
                project_id="workspace",
                documents=[SourceDocument("paper-1", "论文", "", [], None, None, [])],
                tags=[{"label": "1 测试领域", "child": [{"label": "1.1 测试方向"}]}],
                catalog_text="",
                project={},
                model_runtime={},
            )

        node_types = {node["type"] for node in graph["nodes"]}
        relations = {edge["relation"] for edge in graph["edges"]}
        self.assertIn("entity", node_types)
        self.assertIn("reference", node_types)
        self.assertIn("mentions_entity", relations)
        self.assertIn("semantic_relation", relations)
        self.assertIn("cites", relations)


if __name__ == "__main__":
    unittest.main()
