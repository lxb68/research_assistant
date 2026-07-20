"""面向研究 Agent 的只读工具实现。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from app.core.config import settings
from app.services.ccf_catalog import CcfCatalog
from app.services.domain_tree_store import DomainTreeStore
from app.services.document_capabilities import paper_capabilities
from app.services.paper_repository import PaperRepository
from app.services.paper_search import SUPPORTED_SOURCES, search_papers
from app.services.rag_factory import build_default_rag_retriever
from app.services.rag_retriever import RAGRetriever
from app.services.sjr_metrics import SjrMetrics
from app.tools.registry import ToolDefinition, ToolRegistry


ExternalSearch = Callable[[str, str, int], dict[str, Any]]


class ResearchReadOnlyTools:
    """组合文献仓储、全文检索和领域分析的只读能力。"""

    def __init__(
        self,
        *,
        repository: PaperRepository | None = None,
        retriever: RAGRetriever | None = None,
        domain_store: DomainTreeStore | None = None,
        external_search: ExternalSearch = search_papers,
        ccf_catalog: CcfCatalog | None = None,
        sjr_metrics: SjrMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._retriever = retriever
        self.domain_store = domain_store or DomainTreeStore()
        self.external_search = external_search
        self._ccf_catalog = ccf_catalog
        self._sjr_metrics = sjr_metrics
        self.domain_root = Path(settings.backend_storage_dir).resolve() / "domain_tree"

    @property
    def repository(self) -> PaperRepository:
        if self._repository is None:
            self._repository = PaperRepository(settings.hunter_metadata_db)
        return self._repository

    @property
    def retriever(self) -> RAGRetriever:
        if self._retriever is None:
            self._retriever = build_default_rag_retriever(max_chunks=10)
        return self._retriever

    @property
    def ccf_catalog(self) -> CcfCatalog:
        if self._ccf_catalog is None:
            self._ccf_catalog = CcfCatalog()
        return self._ccf_catalog

    @property
    def sjr_metrics(self) -> SjrMetrics:
        if self._sjr_metrics is None:
            self._sjr_metrics = SjrMetrics()
        return self._sjr_metrics

    def build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for definition in self.definitions():
            registry.register(definition)
        return registry

    def definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                "list_knowledge_base_papers",
                (
                    "按标题或入库关键词定位知识库论文，并返回候选项及 recordId。"
                    "适用于询问知识库有哪些论文或为后续操作定位论文；"
                    "列表不返回摘要和正文，不能单独用于回答论文讲了什么。"
                    "找到目标后应继续调用 get_knowledge_base_paper；需要具体正文证据时调用 search_knowledge_base。"
                ),
                self._schema({
                    "keyword": {"type": "string", "description": "可选的标题或入库关键词"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }),
                self.list_knowledge_base_papers,
            ),
            ToolDefinition(
                "get_knowledge_base_paper",
                (
                    "按 record_id 获取单篇论文详情，包括摘要、PDF 状态和解析全文状态。"
                    "摘要存在时可用于概述论文，但必须说明依据是摘要；"
                    "询问具体方法、实验或结论且摘要不足时，应继续调用 search_knowledge_base 并传入该 recordId。"
                ),
                self._schema({"record_id": {"type": "string"}}, required=["record_id"]),
                self.get_knowledge_base_paper,
            ),
            ToolDefinition(
                "search_knowledge_base",
                (
                    "在本地知识库论文的解析全文或摘要中检索证据片段，不访问外部网络。"
                    "已知目标论文时必须通过 paper_ids 限定范围，适用于回答具体方法、实验、结果或结论。"
                ),
                self._schema({
                    "query": {"type": "string"},
                    "paper_ids": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                }, required=["query"]),
                self.search_knowledge_base,
            ),
            ToolDefinition(
                "search_external_papers",
                "只读搜索 arXiv、PubMed、Crossref、IEEE 或开放获取平台，不下载也不写入知识库。",
                self._schema({
                    "query": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "limit_per_source": {"type": "integer", "minimum": 1, "maximum": 20},
                }, required=["query"]),
                self.search_external_papers,
            ),
            ToolDefinition(
                "get_domain_tree",
                (
                    "读取已有领域树和知识图谱的整体概览，不生成或修改数据。"
                    "适用于询问图谱保留了哪些知识、整体结构、总量统计和来源文献；"
                    "查询具体实体、关系或证据时应使用 query_knowledge_graph。"
                    "返回的 graphSummary 表示完整图谱总量。"
                ),
                self._schema({"project_id": {"type": "string"}}),
                self.get_domain_tree,
            ),
            ToolDefinition(
                "query_knowledge_graph",
                (
                    "按关键词查询已有知识图谱中的具体实体、节点、关系和证据。"
                    "适用于查找某个概念或实体关系，不适用于询问完整图谱有什么或总量；"
                    "概览问题应优先使用 get_domain_tree。"
                    "返回的 matchedCounts 是本次命中量，totalCounts 才是完整图谱总量。"
                ),
                self._schema({
                    "query": {"type": "string", "description": "具体关键词；概览模式可省略"},
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "mode": {
                        "type": "string",
                        "enum": ["search", "overview"],
                        "description": "search 关键词检索；overview 返回有限样本和完整总量",
                    },
                }),
                self.query_knowledge_graph,
            ),
            ToolDefinition(
                "get_paper_metrics",
                "读取单篇文献已保存的指标，并使用本地 CCF、SJR 缓存匹配；不会联网刷新指标库。",
                self._schema({"record_id": {"type": "string"}}, required=["record_id"]),
                self.get_paper_metrics,
            ),
            ToolDefinition(
                "get_paper_sections",
                "读取单篇论文的章节与可引用片段，用于了解目录或定位具体章节。",
                self._schema({
                    "record_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                }, required=["record_id"]),
                self.get_paper_sections,
            ),
        ]

    def list_knowledge_base_papers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        keyword = str(arguments.get("keyword") or "").strip() or None
        limit = self._bounded_int(arguments.get("limit"), default=50, minimum=1, maximum=100)
        papers = self.repository.list(limit=limit, keyword=keyword)
        return {
            "total": self.repository.count(keyword=keyword),
            "returned": len(papers),
            "keyword": keyword or "",
            "items": [self._paper_summary(paper) for paper in papers],
        }

    def get_knowledge_base_paper(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"paper": self._paper_detail(self._require_paper(arguments.get("record_id")))}

    def search_knowledge_base(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        limit = self._bounded_int(arguments.get("limit"), default=6, minimum=1, maximum=10)
        paper_ids = [str(value).strip() for value in arguments.get("paper_ids") or [] if str(value).strip()]
        if paper_ids:
            papers = [self.repository.find(record_id=record_id) for record_id in paper_ids]
            papers = [paper for paper in papers if isinstance(paper, dict)]
        else:
            papers = self.repository.list(limit=500)
        evidence = self.retriever.retrieve(query, papers, minimum_evidence_count=limit)[:limit]
        return {
            "query": query,
            "paperCount": len(papers),
            "count": len(evidence),
            "retrievalMode": self.retriever.last_retrieval_mode,
            "diagnostics": self.retriever.last_diagnostics,
            "results": [self._evidence_summary(item, index) for index, item in enumerate(evidence, start=1)],
        }

    def search_external_papers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        requested = arguments.get("sources") or ["arxiv", "crossref", "open_access"]
        sources = list(dict.fromkeys(str(value).strip().lower() for value in requested if str(value).strip()))
        unsupported = [source for source in sources if source not in SUPPORTED_SOURCES]
        if unsupported:
            raise ValueError(f"不支持的论文来源：{', '.join(unsupported)}")
        limit = self._bounded_int(arguments.get("limit_per_source"), default=5, minimum=1, maximum=20)
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for source in sources:
            try:
                payload = self.external_search(source, query, limit)
                results.extend(self._external_paper_summary(item) for item in payload.get("results", []))
            except Exception as error:
                errors.append({"source": source, "error": str(error)})
        return {"query": query, "sources": sources, "count": len(results), "results": results, "errors": errors}

    def get_domain_tree(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._normalize_project_id(arguments.get("project_id"))
        result = self.domain_store.load_result(self.domain_root / project_id, project_id)
        if not result:
            return {"found": False, "projectId": project_id, "message": "未找到已生成的领域树"}
        graph = result.get("knowledgeGraph") if isinstance(result.get("knowledgeGraph"), dict) else {}
        source_documents = self._source_documents(result)
        graph_summary = self._graph_summary(graph)
        return {
            "found": True,
            "projectId": project_id,
            "generatedAt": result.get("generatedAt", ""),
            "language": result.get("language", ""),
            "graphStatus": result.get("graphStatus", ""),
            "documentCount": result.get("documentCount", 0),
            "sourceDocuments": source_documents,
            "domainTree": result.get("domainTree", []),
            "graphAvailable": bool(graph),
            "graphEmpty": bool(graph) and not any(graph_summary.values()),
            "graphSummary": graph_summary,
            "warnings": result.get("warnings", []),
        }

    def query_knowledge_graph(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        project_id = self._normalize_project_id(arguments.get("project_id"))
        limit = self._bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
        requested_mode = str(arguments.get("mode") or "").strip().lower()
        result = self.domain_store.load_result(self.domain_root / project_id, project_id)
        if not isinstance(result, dict):
            return {
                "found": False,
                "graphAvailable": False,
                "graphEmpty": False,
                "projectId": project_id,
                "query": query,
                "message": "未找到已生成的领域树或知识图谱",
            }
        graph = result.get("knowledgeGraph") if isinstance(result.get("knowledgeGraph"), dict) else {}
        total_counts = self._graph_summary(graph)
        graph_available = bool(graph)
        graph_empty = graph_available and not any(total_counts.values())
        if not graph_available:
            return {
                "found": True,
                "graphAvailable": False,
                "graphEmpty": False,
                "graphStatus": result.get("graphStatus", ""),
                "projectId": project_id,
                "query": query,
                "totalCounts": total_counts,
                "matchedCounts": {"nodes": 0, "relations": 0, "evidence": 0},
                "counts": {"nodes": 0, "relations": 0, "evidence": 0},
                "message": "领域树结果存在，但当前没有可查询的知识图谱产物",
            }
        query_mode = self._resolve_graph_query_mode(requested_mode, query)
        if query_mode == "search" and not query:
            raise ValueError("知识图谱搜索模式必须提供具体查询关键词")
        normalized_query = query.casefold()
        all_nodes = [*self._list(graph, "nodes"), *self._list(graph, "entities")]
        all_relations = [*self._list(graph, "edges"), *self._list(graph, "semanticRelations")]
        all_evidence = self._list(graph, "evidence")
        if query_mode == "overview":
            nodes = all_nodes[:limit]
            matched_relations = all_relations[:limit]
            evidence = all_evidence[:limit]
        else:
            nodes = self._matching_items(all_nodes, normalized_query, limit)
            matched_ids = {str(item.get("id") or item.get("localId") or "") for item in nodes}
            matched_relations = [
                item for item in all_relations
                if self._matches(item, normalized_query)
                or str(item.get("source") or "") in matched_ids
                or str(item.get("target") or "") in matched_ids
            ][:limit]
            evidence = self._matching_items(all_evidence, normalized_query, limit)
        matched_counts = {"nodes": len(nodes), "relations": len(matched_relations), "evidence": len(evidence)}
        return {
            "found": True,
            "graphAvailable": True,
            "graphEmpty": graph_empty,
            "graphStatus": result.get("graphStatus", ""),
            "projectId": project_id,
            "query": query,
            "queryMode": query_mode,
            "nodes": nodes,
            "relations": matched_relations,
            "evidence": evidence,
            "totalCounts": total_counts,
            "matchedCounts": matched_counts,
            # 兼容已有调用方；新代码应使用语义明确的 matchedCounts。
            "counts": matched_counts,
        }

    def get_paper_metrics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        paper = self._require_paper(arguments.get("record_id"))
        venue_text = " ".join(str(paper.get(key) or "") for key in ("venue", "journal", "containerTitle", "title"))
        ccf = self.ccf_catalog.lookup(venue_text)
        sjr = self.sjr_metrics.lookup(str(paper.get("venue") or ""), refresh_if_empty=False)
        return {
            "recordId": paper.get("id", ""),
            "title": paper.get("title", ""),
            "venue": paper.get("venue", ""),
            "metrics": {
                "ccfLevel": ccf.get("ccfLevel") or paper.get("ccfLevel", ""),
                "ccfSource": ccf.get("ccfSource") or paper.get("ccfSource", ""),
                "ccfMatchedName": ccf.get("ccfMatchedName") or paper.get("ccfMatchedName", ""),
                "sjr": sjr.get("sjr") if sjr.get("sjr") is not None else paper.get("sjr"),
                "impactFactor": paper.get("impactFactor"),
                "metricSource": sjr.get("metricSource") or paper.get("metricSource", ""),
            },
            "cacheOnly": True,
        }

    def get_paper_sections(self, arguments: dict[str, Any]) -> dict[str, Any]:
        paper = self._require_paper(arguments.get("record_id"))
        limit = self._bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
        sections = self.retriever.list_paper_sections(paper, limit=limit)
        return {"recordId": paper.get("id", ""), "title": paper.get("title", ""), "count": len(sections), "sections": sections}

    @staticmethod
    def _schema(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}

    def _require_paper(self, record_id: Any) -> dict[str, Any]:
        normalized = str(record_id or "").strip()
        paper = self.repository.find(record_id=normalized) if normalized else None
        if not paper:
            raise ValueError(f"知识库中不存在文献：{normalized or '空 ID'}")
        return paper

    def _paper_summary(self, paper: dict[str, Any]) -> dict[str, Any]:
        capabilities = paper_capabilities(paper)
        has_abstract = capabilities["hasAbstract"]
        has_pdf = capabilities["hasPdf"]
        has_parsed_full_text = capabilities["hasParsedFullText"]
        return {
            "recordId": paper.get("id", ""), "title": paper.get("title", ""),
            "authors": list(paper.get("authors") or [])[:20], "year": paper.get("year", ""),
            "venue": paper.get("venue", ""), "source": paper.get("source", ""),
            "doi": paper.get("doi", ""), "url": paper.get("url", ""),
            "hasPdf": has_pdf,
            "hasAbstract": has_abstract,
            # 兼容旧调用方；新代码应使用语义更准确的 hasParsedFullText。
            "hasFullText": has_parsed_full_text,
            "hasParsedFullText": has_parsed_full_text,
            "availableContent": [
                name
                for name, available in (
                    ("metadata", True),
                    ("abstract", has_abstract),
                    ("pdf", has_pdf),
                    ("parsed_full_text", has_parsed_full_text),
                )
                if available
            ],
        }

    def _paper_detail(self, paper: dict[str, Any]) -> dict[str, Any]:
        return {
            **self._paper_summary(paper), "abstract": str(paper.get("abstract") or "")[:8000],
            "publishedAt": paper.get("publishedAt", ""), "savedAt": paper.get("savedAt", ""),
            "keyword": paper.get("keyword", ""), "ccfLevel": paper.get("ccfLevel", ""),
            "sjr": paper.get("sjr"), "impactFactor": paper.get("impactFactor"),
        }

    @staticmethod
    def _evidence_summary(item: dict[str, Any], index: int) -> dict[str, Any]:
        return {
            "index": index, "recordId": item.get("record_id") or item.get("recordId") or "",
            "title": item.get("title", ""), "section": item.get("section", ""),
            "chunkIndex": item.get("chunk_index") or item.get("chunkIndex") or 0,
            "score": round(float(item.get("score") or 0), 6), "excerpt": str(item.get("text") or "")[:1800],
            "source": item.get("source", ""), "url": item.get("url", ""),
        }

    @staticmethod
    def _external_paper_summary(paper: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": paper.get("source", ""), "title": paper.get("title", ""),
            "authors": list(paper.get("authors") or [])[:20], "abstract": str(paper.get("abstract") or "")[:1200],
            "year": paper.get("year", ""), "venue": paper.get("venue", ""), "doi": paper.get("doi", ""),
            "url": paper.get("url", ""), "pdfUrl": paper.get("pdfUrl") or paper.get("pdf_url") or "",
        }

    @staticmethod
    def _graph_summary(graph: dict[str, Any]) -> dict[str, int]:
        return {
            "nodes": len(graph.get("nodes") or []), "entities": len(graph.get("entities") or []),
            "relations": len(graph.get("edges") or []) + len(graph.get("semanticRelations") or []),
            "evidence": len(graph.get("evidence") or []), "citations": len(graph.get("citations") or []),
        }

    @staticmethod
    def _source_documents(result: dict[str, Any]) -> list[dict[str, Any]]:
        """返回领域树的准确来源清单，同时过滤本地路径和大段目录文本。"""
        manifest = result.get("manifest")
        documents = manifest.get("documents") if isinstance(manifest, dict) else []
        if not isinstance(documents, list):
            return []
        return [
            {
                "recordId": str(document.get("recordId") or document.get("record_id") or ""),
                "title": str(document.get("title") or "未命名文献"),
                "tocEntryCount": int(document.get("tocEntryCount") or document.get("toc_entry_count") or 0),
            }
            for document in documents
            if isinstance(document, dict)
        ]

    @staticmethod
    def _list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @classmethod
    def _matching_items(cls, items: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
        return [item for item in items if cls._matches(item, query)][:limit]

    @staticmethod
    def _matches(item: dict[str, Any], query: str) -> bool:
        return query in json.dumps(item, ensure_ascii=False, default=str).casefold()

    @staticmethod
    def _resolve_graph_query_mode(requested_mode: str, query: str) -> str:
        """仅根据结构化参数解析查询模式，不从自然语言中猜测意图。"""
        if requested_mode in {"search", "overview"}:
            return requested_mode
        return "search" if query else "overview"

    @staticmethod
    def _normalize_project_id(value: Any) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "workspace-domain-tree").strip())
        return cleaned[:120] or "workspace-domain-tree"

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _has_pdf(paper: dict[str, Any]) -> bool:
        return paper_capabilities(paper)["hasPdf"]

    @staticmethod
    def _has_full_text(paper: dict[str, Any]) -> bool:
        return paper_capabilities(paper)["hasParsedFullText"]


def build_research_tool_registry() -> ToolRegistry:
    """构建应用默认的研究只读工具注册表。"""
    return ResearchReadOnlyTools().build_registry()


__all__ = ["ResearchReadOnlyTools", "build_research_tool_registry"]
