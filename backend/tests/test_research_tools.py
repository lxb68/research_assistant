"""验证研究只读工具的注册、执行与编排接入。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.orchestrator_agent import OrchestratorAgent
from app.services.paper_repository import PaperRepository
from app.tools.registry import ToolDefinition, ToolRegistry
from app.tools.research import ResearchReadOnlyTools


class ResearchReadOnlyToolsTest(unittest.TestCase):
    """覆盖工具白名单和主要只读数据边界。"""

    def _build_tools(self, repository: PaperRepository, *, retriever: Mock | None = None) -> ResearchReadOnlyTools:
        ccf = Mock()
        ccf.lookup.return_value = {"ccfLevel": "A", "ccfSource": "conference", "ccfMatchedName": "TestConf"}
        sjr = Mock()
        sjr.lookup.return_value = {"sjr": 2.5, "impactFactor": 2.5, "metricSource": "SJR"}
        return ResearchReadOnlyTools(
            repository=repository,
            retriever=retriever or Mock(),
            domain_store=Mock(),
            external_search=Mock(),
            ccf_catalog=ccf,
            sjr_metrics=sjr,
        )

    @staticmethod
    def _save_paper(repository: PaperRepository, *, record_id: str = "paper-1") -> None:
        repository.save(
            {
                "id": record_id,
                "source": "local",
                "title": "Secure Retrieval",
                "authors": ["Alice"],
                "abstract": "A paper about private information retrieval.",
                "year": "2026",
                "venue": "TestConf",
                "doi": "10.1/test",
                "url": "https://example.test/paper",
                "pdfPath": "",
                "keyword": "privacy",
                "savedAt": "2026-07-16T00:00:00Z",
            }
        )

    def test_registers_all_recommended_read_only_tools(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tools = self._build_tools(PaperRepository(Path(temp_dir) / "papers.sqlite3"))
            names = {item["name"] for item in tools.build_registry().definitions()}

        self.assertEqual(
            names,
            {
                "list_knowledge_base_papers",
                "get_knowledge_base_paper",
                "search_knowledge_base",
                "search_external_papers",
                "get_domain_tree",
                "query_knowledge_graph",
                "get_paper_metrics",
                "get_paper_sections",
            },
        )

    def test_list_and_get_papers_use_repository_truth(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repository = PaperRepository(Path(temp_dir) / "papers.sqlite3")
            self._save_paper(repository)
            registry = self._build_tools(repository).build_registry()

            listed = registry.execute("list_knowledge_base_papers", {"keyword": "Secure", "limit": 10})
            detail = registry.execute("get_knowledge_base_paper", {"record_id": "paper-1"})

        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["recordId"], "paper-1")
        self.assertTrue(listed["items"][0]["hasAbstract"])
        self.assertFalse(listed["items"][0]["hasParsedFullText"])
        self.assertIn("abstract", listed["items"][0]["availableContent"])
        self.assertEqual(detail["paper"]["title"], "Secure Retrieval")

    def test_search_and_sections_delegate_to_retriever(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repository = PaperRepository(Path(temp_dir) / "papers.sqlite3")
            self._save_paper(repository)
            retriever = Mock()
            retriever.retrieve.return_value = [
                {
                    "record_id": "paper-1",
                    "title": "Secure Retrieval",
                    "section": "Method",
                    "chunk_index": 2,
                    "score": 0.8,
                    "text": "Private retrieval evidence.",
                }
            ]
            retriever.last_retrieval_mode = "bm25"
            retriever.last_diagnostics = {"candidateCount": 1}
            retriever.list_paper_sections.return_value = [{"chunkIndex": 2, "section": "Method"}]
            registry = self._build_tools(repository, retriever=retriever).build_registry()

            evidence = registry.execute("search_knowledge_base", {"query": "private retrieval", "limit": 3})
            sections = registry.execute("get_paper_sections", {"record_id": "paper-1", "limit": 5})

        self.assertEqual(evidence["results"][0]["recordId"], "paper-1")
        self.assertEqual(sections["sections"][0]["section"], "Method")
        retriever.retrieve.assert_called_once()
        retriever.list_paper_sections.assert_called_once()

    def test_metrics_are_cache_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repository = PaperRepository(Path(temp_dir) / "papers.sqlite3")
            self._save_paper(repository)
            tools = self._build_tools(repository)

            result = tools.get_paper_metrics({"record_id": "paper-1"})

        self.assertTrue(result["cacheOnly"])
        self.assertEqual(result["metrics"]["ccfLevel"], "A")
        tools.sjr_metrics.lookup.assert_called_once_with("TestConf", refresh_if_empty=False)

    def test_external_search_is_preview_only_and_collects_source_errors(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repository = PaperRepository(Path(temp_dir) / "papers.sqlite3")
            tools = self._build_tools(repository)
            tools.external_search.side_effect = [
                {"results": [{"source": "arxiv", "title": "Preview", "abstract": "Result"}]},
                RuntimeError("upstream unavailable"),
            ]

            result = tools.search_external_papers(
                {"query": "retrieval", "sources": ["arxiv", "crossref"], "limit_per_source": 2}
            )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["title"], "Preview")
        self.assertEqual(result["errors"][0]["source"], "crossref")

    def test_domain_tree_and_graph_query_use_saved_artifacts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repository = PaperRepository(Path(temp_dir) / "papers.sqlite3")
            tools = self._build_tools(repository)
            tools.domain_store.load_result.return_value = {
                "generatedAt": "2026-07-16T00:00:00Z",
                "language": "zh",
                "graphStatus": "ready",
                "documentCount": 2,
                "domainTree": [{"name": "隐私计算"}],
                "manifest": {
                    "documents": [
                        {
                            "recordId": "paper-1",
                            "title": "Secure Retrieval",
                            "tocEntryCount": 7,
                            "markdownPath": "C:/private/full.md",
                            "catalogText": "不应进入工具响应的大段目录",
                        }
                    ]
                },
                "knowledgeGraph": {
                    "nodes": [{"id": "domain:1", "name": "隐私计算"}],
                    "entities": [],
                    "edges": [{"source": "project:test", "target": "domain:1", "relation": "has_domain"}],
                    "semanticRelations": [],
                    "evidence": [{"quote": "隐私计算保护数据"}],
                    "citations": [],
                },
            }

            tree = tools.get_domain_tree({"project_id": "test"})
            graph = tools.query_knowledge_graph({"project_id": "test", "query": "隐私计算", "limit": 10})

        self.assertTrue(tree["found"])
        self.assertEqual(tree["graphSummary"]["nodes"], 1)
        self.assertEqual(
            tree["sourceDocuments"],
            [{"recordId": "paper-1", "title": "Secure Retrieval", "tocEntryCount": 7}],
        )
        self.assertNotIn("markdownPath", tree["sourceDocuments"][0])
        self.assertNotIn("catalogText", tree["sourceDocuments"][0])
        self.assertFalse(tree["graphEmpty"])
        self.assertEqual(
            graph["totalCounts"],
            {"nodes": 1, "entities": 0, "relations": 1, "evidence": 1, "citations": 0},
        )
        self.assertEqual(graph["matchedCounts"], {"nodes": 1, "relations": 1, "evidence": 1})
        self.assertEqual(graph["counts"], {"nodes": 1, "relations": 1, "evidence": 1})
        self.assertFalse(graph["graphEmpty"])

    def test_zero_match_is_not_reported_as_an_empty_graph(self) -> None:
        """关键词零命中不能覆盖知识图谱的真实存量。"""
        with TemporaryDirectory() as temp_dir:
            tools = self._build_tools(PaperRepository(Path(temp_dir) / "papers.sqlite3"))
            tools.domain_store.load_result.return_value = {
                "graphStatus": "ready",
                "knowledgeGraph": {
                    "nodes": [{"id": "domain:1", "name": "隐私计算"}],
                    "entities": [{"id": "entity:1", "name": "同态加密"}],
                    "edges": [],
                    "semanticRelations": [{"source": "entity:1", "target": "entity:2", "relation": "保护"}],
                    "evidence": [{"quote": "同态加密保护查询数据"}],
                    "citations": [{"recordId": "paper-1"}],
                },
            }

            graph = tools.query_knowledge_graph({"query": "完全不存在的关键词", "limit": 10})

        self.assertTrue(graph["graphAvailable"])
        self.assertFalse(graph["graphEmpty"])
        self.assertEqual(graph["matchedCounts"], {"nodes": 0, "relations": 0, "evidence": 0})
        self.assertEqual(
            graph["totalCounts"],
            {"nodes": 1, "entities": 1, "relations": 1, "evidence": 1, "citations": 1},
        )

    def test_explicit_graph_overview_mode_returns_limited_samples(self) -> None:
        """概览行为应由结构化 mode 参数驱动，不依赖自然语言短语匹配。"""
        with TemporaryDirectory() as temp_dir:
            tools = self._build_tools(PaperRepository(Path(temp_dir) / "papers.sqlite3"))
            tools.domain_store.load_result.return_value = {
                "graphStatus": "ready",
                "knowledgeGraph": {
                    "nodes": [{"id": "domain:1", "name": "隐私计算"}],
                    "entities": [{"id": "entity:1", "name": "同态加密"}],
                    "edges": [{"source": "project:test", "target": "domain:1", "relation": "has_domain"}],
                    "semanticRelations": [],
                    "evidence": [{"quote": "证据"}],
                    "citations": [],
                },
            }

            graph = tools.query_knowledge_graph({"mode": "overview", "limit": 1})

        self.assertEqual(graph["queryMode"], "overview")
        self.assertEqual(graph["matchedCounts"], {"nodes": 1, "relations": 1, "evidence": 1})
        self.assertEqual(graph["totalCounts"]["entities"], 1)

    def test_graph_search_mode_requires_a_query(self) -> None:
        """显式搜索模式不能把空关键词悄悄退化成全量查询。"""
        with TemporaryDirectory() as temp_dir:
            tools = self._build_tools(PaperRepository(Path(temp_dir) / "papers.sqlite3"))
            tools.domain_store.load_result.return_value = {
                "graphStatus": "ready",
                "knowledgeGraph": {"nodes": [], "entities": [], "edges": [], "semanticRelations": []},
            }

            with self.assertRaisesRegex(ValueError, "必须提供具体查询关键词"):
                tools.query_knowledge_graph({"mode": "search"})

    def test_tool_descriptions_explain_overview_and_search_boundaries(self) -> None:
        """工具目录应告诉路由模型适用场景和统计字段语义。"""
        with TemporaryDirectory() as temp_dir:
            definitions = self._build_tools(
                PaperRepository(Path(temp_dir) / "papers.sqlite3")
            ).build_registry().definitions()

        descriptions = {item["name"]: item["description"] for item in definitions}
        self.assertIn("完整图谱总量", descriptions["get_domain_tree"])
        self.assertIn("matchedCounts", descriptions["query_knowledge_graph"])
        self.assertIn("totalCounts", descriptions["query_knowledge_graph"])

    def test_registry_rejects_unknown_tool_and_arguments(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "safe_tool",
                "测试工具",
                {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 5}},
                    "required": ["limit"],
                    "additionalProperties": False,
                },
                lambda arguments: {"limit": arguments["limit"]},
            )
        )

        with self.assertRaisesRegex(ValueError, "未注册工具"):
            registry.execute("unsafe_tool", {})
        with self.assertRaisesRegex(ValueError, "未知参数"):
            registry.execute("safe_tool", {"limit": 1, "delete": True})
        with self.assertRaisesRegex(ValueError, "不能大于"):
            registry.execute("safe_tool", {"limit": 10})

    def test_registry_rejects_values_outside_declared_enum(self) -> None:
        """工具枚举参数必须在执行前由注册表拒绝非法值。"""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "mode_tool",
                "测试枚举参数",
                {
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": ["search", "overview"]}},
                    "required": ["mode"],
                    "additionalProperties": False,
                },
                lambda arguments: arguments,
            )
        )

        with self.assertRaisesRegex(ValueError, "只能是"):
            registry.execute("mode_tool", {"mode": "delete"})


class OrchestratorToolIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """验证编排器能够选择、执行并返回注册工具结果。"""

    def _registry(self, handler: Mock) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "list_knowledge_base_papers",
                "列出知识库",
                {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                    "required": [],
                    "additionalProperties": False,
                },
                handler,
            )
        )
        return registry

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_catalog_question_executes_registered_tool(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        build_model_payload.return_value = {"provider": "test", "protocol": "openai_compatible", "model": "test"}
        completion.side_effect = [
            '{"action":"tool","toolName":"list_knowledge_base_papers","arguments":{"limit":100}}',
            '{"action":"final","answer":"知识库中共有 1 篇文献。","limitations":[]}',
        ]
        handler = Mock(return_value={"total": 1, "returned": 1, "items": [{"recordId": "p1", "title": "Paper"}]})
        agent = OrchestratorAgent(tool_registry=self._registry(handler))

        result = await agent.run("现在知识库里有哪些文献？")

        self.assertEqual(result["action"], "tool")
        self.assertEqual(result["result"]["tool"]["name"], "list_knowledge_base_papers")
        self.assertEqual(result["result"]["toolResult"]["total"], 1)
        handler.assert_called_once_with({"limit": 100})
        self.assertEqual(completion.call_count, 2)
        router_prompt = completion.call_args_list[0].args[1][0]["content"]
        self.assertIn("list_knowledge_base_papers", router_prompt)
        self.assertIn("工具目录中的名称、描述和参数 Schema", router_prompt)

    def test_route_parser_accepts_only_registered_tool(self) -> None:
        agent = OrchestratorAgent(tool_registry=self._registry(Mock(return_value={})))

        decision = agent._parse_route_decision(
            '{"action":"tool","toolName":"list_knowledge_base_papers","arguments":{"limit":5}}'
        )

        self.assertEqual(decision["toolName"], "list_knowledge_base_papers")
        with self.assertRaisesRegex(ValueError, "未注册工具"):
            agent._parse_route_decision('{"action":"tool","toolName":"delete_papers","arguments":{}}')

    @patch("app.agents.orchestrator_agent.chat_completion")
    @patch("app.agents.orchestrator_agent.ModelConfigStore.build_model_payload")
    async def test_graph_overview_question_is_selected_from_tool_metadata(
        self,
        build_model_payload: Mock,
        completion: Mock,
    ) -> None:
        """图谱概览工具应由 Agent 根据注册元数据选择和执行。"""
        build_model_payload.return_value = {"provider": "test", "protocol": "openai_compatible", "model": "test"}
        completion.side_effect = [
            '{"action":"tool","toolName":"get_domain_tree","arguments":{}}',
            '{"action":"final","answer":"当前知识图谱包含 1 个节点。","limitations":[]}',
        ]
        handler = Mock(return_value={"found": True, "graphSummary": {"nodes": 1}})
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "get_domain_tree",
                "读取领域树和知识图谱整体概览；适用于询问图谱保留了哪些知识和总量统计。",
                {
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": [],
                    "additionalProperties": False,
                },
                handler,
            )
        )
        agent = OrchestratorAgent(tool_registry=registry)

        result = await agent.run("现在的知识图谱保留了哪些知识")

        self.assertEqual(result["result"]["tool"]["name"], "get_domain_tree")
        handler.assert_called_once_with({})
        router_prompt = completion.call_args_list[0].args[1][0]["content"]
        self.assertIn("适用于询问图谱保留了哪些知识", router_prompt)


if __name__ == "__main__":
    unittest.main()
