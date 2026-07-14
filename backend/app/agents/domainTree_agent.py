"""从本地 Markdown 资料生成领域树、知识图谱及其持久化结果。"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from app.core.config import settings


logger = logging.getLogger(__name__)

_GENERIC_SECTION_TITLES = {
    "additional ablation studies",
    "additional implementation details",
    "additional mathematical derivation",
    "abstract",
    "acknowledgments",
    "appendix",
    "background",
    "bibliography",
    "conclusion",
    "conclusions",
    "discussion",
    "evaluation",
    "experiment",
    "experiments",
    "future work",
    "implementation",
    "introduction",
    "limitations",
    "methodology",
    "method",
    "methods",
    "model",
    "models",
    "overview",
    "preliminaries",
    "references",
    "related work",
    "results",
    "setup",
    "system model",
    "technical approach",
    "technical framework",
}
_GENERIC_LABEL_TOKENS = {
    "algorithm",
    "algorithms",
    "analysis",
    "approach",
    "approaches",
    "architecture",
    "architectures",
    "background",
    "evaluation",
    "experiment",
    "experiments",
    "framework",
    "frameworks",
    "implementation",
    "implementations",
    "method",
    "methods",
    "methodology",
    "model",
    "models",
    "overview",
    "pipeline",
    "pipelines",
    "scheme",
    "schemes",
    "setup",
    "strategy",
    "strategies",
    "system",
    "systems",
    "workflow",
    "workflows",
    "方法",
    "方法学",
    "实验",
    "实验设计",
    "实验设置",
    "实验结果",
    "总体方案",
    "技术路线",
    "技术框架",
    "系统框架",
    "系统设计",
    "系统模型",
    "架构设计",
    "模型设计",
    "模型结构",
    "机制设计",
    "流程设计",
    "评估",
    "评价",
    "背景",
    "概述",
}
_NON_CORE_SECTION_PATTERNS = (
    "ccf concept",
    "ccf concepts",
    "ccs concept",
    "ccs concepts",
    "concept term",
    "concept terms",
    "index term",
    "index terms",
    "keyword",
    "keywords",
    "subject descriptor",
    "subject descriptors",
    "categories and subject descriptors",
    "classification term",
    "classification terms",
    "acm classification",
    "acm computing classification",
    "mathematics subject classification",
    "ams subject classification",
    "msc",
    "notation",
    "notations",
    "symbol",
    "symbols",
    "abbreviation",
    "abbreviations",
    "reference",
    "references",
    "bibliography",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "funding",
    "conflict of interest",
    "conflicts of interest",
    "declaration",
    "ethical approval",
    "author contribution",
    "author contributions",
    "competing interests",
    "data availability",
    "supplementary material",
    "supplementary materials",
    "appendix",
    "journal",
    "conference",
    "publication info",
    "copyright",
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "approach",
    "based",
    "by",
    "for",
    "from",
    "in",
    "into",
    "model",
    "models",
    "of",
    "on",
    "system",
    "the",
    "to",
    "toward",
    "using",
    "with",
}


@dataclass(slots=True)
class SourceDocument:
    record_id: str
    title: str
    abstract: str
    keywords: list[str]
    markdown_path: Path | None
    markdown_dir: Path | None
    toc_entries: list[dict[str, Any]]


class DomainTreeAgent:
    """创建领域树和轻量级知识图谱的代理类。"""

    def __init__(
        self,
        *,
        storage_dir: str | Path | None = None,
        metadata_db_path: str | Path | None = None,
        prompt_dir: str | Path | None = None,
    ) -> None:
        self.storage_dir = self._resolve_storage_dir(storage_dir)
        self.metadata_db_path = Path(metadata_db_path or settings.hunter_metadata_db).resolve()
        self.prompt_dir = Path(prompt_dir or (Path(__file__).resolve().parents[2] / "src" / "prompt")).resolve()
        self.analysis_root = self.storage_dir / "domain_tree"
        self.analysis_root.mkdir(parents=True, exist_ok=True)

    async def handle_domain_tree(
        self,
        project_id: str,
        *,
        action: str = "rebuild",
        all_toc: str | None = None,
        new_toc: str | None = None,
        model: Any | None = None,
        language: str = "中文",
        delete_toc: str | None = None,
        project: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]] | None:
        normalized_project_id = self._normalize_project_id(project_id)
        # 直接调用 get_tags 返回当前已存储的标签，不进行任何计算或生成
        if action == "keep":
            logger.info("[%s] 使用已有领域树", normalized_project_id)
            return self.get_tags(normalized_project_id)

        documents = self._load_documents(normalized_project_id)
        if not documents:
            logger.warning("[%s] 存储目录中未找到 Markdown 来源", normalized_project_id)
            return None

        #
        catalog_text = all_toc or self._build_catalog_text(documents)
        if not catalog_text.strip():
            logger.warning("[%s] 目录文本为空，跳过领域树生成", normalized_project_id)
            return None

        existing_tags = self.get_tags(normalized_project_id)
        existing_manifest = self._load_manifest(normalized_project_id)
        next_document_catalog = self._document_catalog_map(documents)
        previous_document_catalog = self._manifest_document_catalog_map(existing_manifest)
        if action == "revise" and existing_manifest and (not new_toc and not delete_toc):
            new_toc = self._join_catalog_sections(
                next_document_catalog[record_id]
                for record_id in next_document_catalog
                if previous_document_catalog.get(record_id) != next_document_catalog[record_id]
            )
            delete_toc = self._join_catalog_sections(
                previous_document_catalog[record_id]
                for record_id in previous_document_catalog
                if previous_document_catalog.get(record_id) != next_document_catalog.get(record_id)
            )
        if action == "revise" and existing_tags:
            prompt = self.get_label_revise_prompt(
                language,
                {
                    "text": catalog_text[:100000],
                    "existingTags": self.filter_domain_tree(existing_tags),
                    "newContent": new_toc or "",
                    "deletedContent": delete_toc or "",
                },
            )
        else:
            prompt = self.get_label_prompt(language, {"text": catalog_text[:100000]})

        # 生成领域树标签
        tags = self._generate_domain_tree(
            prompt=prompt,
            documents=documents,
            catalog_text=catalog_text,
            language=language,
            model=model,
        )
        tags = self._refine_tree_specificity(tags, documents)
        if not tags:
            logger.error("[%s] 领域树标签生成失败", normalized_project_id)
            return None

        # 构建知识图谱
        graph = self._build_knowledge_graph(
            project_id=normalized_project_id,
            documents=documents,
            tags=tags,
            catalog_text=catalog_text,
            project=project or {},
        )
        self.batch_save_tags(
            normalized_project_id,
            tags,
            graph,
            documents=documents,
            catalog_text=catalog_text,
            action=action,
            language=language,
        )
        return tags

    # 获取当前项目的领域树标签，如果不存在则返回 None
    def get_tags(self, project_id: str) -> list[dict[str, Any]] | None:
        domain_tree_path = self._analysis_dir(project_id) / "domain_tree.json"
        if not domain_tree_path.exists():
            return None

        try:
            payload = json.loads(domain_tree_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[%s] 读取领域树失败：%s", project_id, error)
            return None

        # 解析 payload，确保返回的结果是一个列表
        if isinstance(payload, dict):
            tree = payload.get("domainTree")
            return tree if isinstance(tree, list) else None

        return payload if isinstance(payload, list) else None

    def get_result(self, project_id: str) -> dict[str, Any] | None:
        output_dir = self._analysis_dir(self._normalize_project_id(project_id))
        domain_tree_path = output_dir / "domain_tree.json"
        knowledge_graph_path = output_dir / "knowledge_graph.json"
        manifest_path = output_dir / "manifest.json"
        catalog_path = output_dir / "catalog.txt"

        if not domain_tree_path.exists():
            return None

        try:
            domain_payload = json.loads(domain_tree_path.read_text(encoding="utf-8"))
            graph_payload = (
                json.loads(knowledge_graph_path.read_text(encoding="utf-8"))
                if knowledge_graph_path.exists()
                else {}
            )
            manifest_payload = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists()
                else {}
            )
            catalog_text = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[%s] 读取领域树结果失败：%s", project_id, error)
            return None

        domain_tree = domain_payload.get("domainTree") if isinstance(domain_payload, dict) else domain_payload
        return {
            "projectId": domain_payload.get("projectId", project_id) if isinstance(domain_payload, dict) else project_id,
            "generatedAt": domain_payload.get("generatedAt", "") if isinstance(domain_payload, dict) else "",
            "action": domain_payload.get("action", "") if isinstance(domain_payload, dict) else "",
            "language": domain_payload.get("language", "") if isinstance(domain_payload, dict) else "",
            "documentCount": domain_payload.get("documentCount", 0) if isinstance(domain_payload, dict) else 0,
            "domainTree": domain_tree if isinstance(domain_tree, list) else [],
            "knowledgeGraph": graph_payload if isinstance(graph_payload, dict) else {},
            "manifest": manifest_payload if isinstance(manifest_payload, dict) else {},
            "catalogText": catalog_text,
        }

    def _load_manifest(self, project_id: str) -> dict[str, Any]:
        manifest_path = self._analysis_dir(self._normalize_project_id(project_id)) / "manifest.json"
        if not manifest_path.exists():
            return {}
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[%s] 读取清单文件失败：%s", project_id, error)
            return {}
        return payload if isinstance(payload, dict) else {}

    def get_project_tocs(self, project_id: str) -> str:
        documents = self._load_documents(project_id)
        return self._build_catalog_text(documents)

    def get_label_prompt(self, language: str, data: dict[str, Any]) -> str:
        template = self._read_prompt_file("lable", language)
        return self._render_prompt(template, data)

    def get_label_revise_prompt(self, language: str, data: dict[str, Any]) -> str:
        template = self._read_prompt_file("labelRevise", language)
        return self._render_prompt(template, data)

    def filter_domain_tree(self, tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for index, item in enumerate(tags, start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            children: list[dict[str, str]] = []
            raw_children = item.get("child")
            if isinstance(raw_children, list):
                for child_index, child in enumerate(raw_children, start=1):
                    if not isinstance(child, dict):
                        continue
                    child_label = str(child.get("label", "")).strip()
                    if child_label:
                        children.append({"label": child_label or f"{index}.{child_index}"})
            node = {"label": label}
            if children:
                node["child"] = children
            filtered.append(node)
        return filtered

    def batch_save_tags(
        self,
        project_id: str,
        tags: list[dict[str, Any]],
        knowledge_graph: dict[str, Any],
        *,
        documents: list[SourceDocument],
        catalog_text: str,
        action: str,
        language: str,
    ) -> None:
        output_dir = self._analysis_dir(project_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        generated_at = datetime.now(timezone.utc).isoformat()
        domain_payload = {
            "projectId": project_id,
            "generatedAt": generated_at,
            "action": action,
            "language": language,
            "documentCount": len(documents),
            "domainTree": tags,
        }
        graph_payload = {
            **knowledge_graph,
            "projectId": project_id,
            "generatedAt": generated_at,
            "documentCount": len(documents),
        }
        manifest_payload = {
            "projectId": project_id,
            "generatedAt": generated_at,
            "action": action,
            "language": language,
            "documents": [
                {
                    "recordId": document.record_id,
                    "title": document.title,
                    "markdownPath": str(document.markdown_path) if document.markdown_path else "",
                    "markdownDir": str(document.markdown_dir) if document.markdown_dir else "",
                    "tocEntryCount": len(document.toc_entries),
                    "catalogText": self._build_document_catalog_text(document, index + 1),
                }
                for index, document in enumerate(documents)
            ],
        }

        (output_dir / "domain_tree.json").write_text(
            json.dumps(domain_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / "knowledge_graph.json").write_text(
            json.dumps(graph_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / "catalog.txt").write_text(catalog_text, encoding="utf-8")
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[%s] 已将领域树和知识图谱保存到 %s", project_id, output_dir)

    def _generate_domain_tree(
        self,
        *,
        prompt: str,
        documents: list[SourceDocument],
        catalog_text: str,
        language: str,
        model: Any | None,
    ) -> list[dict[str, Any]]:
        llm_output = self._call_llm(prompt, language=language, model=model)
        tags = self.extract_json_from_llm_output(llm_output) if llm_output else None
        if tags:
            return self.filter_domain_tree(tags)
        logger.info("大模型不可用或返回了无效 JSON，改用启发式规则生成领域树")
        return self._heuristic_domain_tree(documents, catalog_text)

    def _call_llm(self, prompt: str, *, language: str, model: Any | None) -> str | None:
        api_key = ""
        if isinstance(model, dict):
            api_key = str(model.get("api_key") or model.get("apiKey") or "").strip()
        if not api_key:
            api_key = (
                os.getenv("DOMAIN_TREE_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or settings.llm_translation_api_key
            ).strip()
        if not api_key:
            return None

        base_url = ""
        if isinstance(model, dict):
            base_url = str(model.get("base_url") or model.get("baseUrl") or "").strip().rstrip("/")
        if not base_url:
            base_url = (
                os.getenv("DOMAIN_TREE_BASE_URL")
                or os.getenv("OPENAI_BASE_URL")
                or settings.llm_translation_base_url
            ).rstrip("/")
        model_name = self._resolve_model_name(model)
        system_constraint = ""
        if isinstance(model, dict):
            system_constraint = str(model.get("system_constraint") or model.get("systemConstraint") or "").strip()
        payload = {
            "model": model_name,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise knowledge classification assistant. "
                        "Return only valid JSON and do not include markdown fences. "
                        f"{system_constraint}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        request = Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urlopen(request, timeout=settings.request_timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            logger.warning("领域树大模型调用失败：%s", error)
            return None

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.warning("领域树大模型响应格式无效：%s", data)
            return None
        return str(content).strip()

    def extract_json_from_llm_output(self, output: str | None) -> list[dict[str, Any]] | None:
        if not output:
            return None

        cleaned = output.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        candidates = [cleaned]
        array_match = re.search(r"\[[\s\S]*\]", cleaned)
        if array_match:
            candidates.insert(0, array_match.group(0))

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        return None

    def _load_documents(self, project_id: str) -> list[SourceDocument]:
        specific_document = self._load_document_for_project(project_id)
        if specific_document:
            return [specific_document]

        documents: list[SourceDocument] = []
        if self.metadata_db_path.exists():
            documents.extend(self._load_documents_from_metadata_db())

        if documents:
            unique: dict[str, SourceDocument] = {}
            for document in documents:
                key = str(document.markdown_dir or document.markdown_path or document.record_id).lower()
                unique.setdefault(key, document)
            return list(unique.values())

        markdown_root = self.storage_dir / "markdown"
        if not markdown_root.exists():
            return []

        fallback_documents: list[SourceDocument] = []
        for directory in sorted(path for path in markdown_root.iterdir() if path.is_dir()):
            fallback_documents.append(self._build_document_from_markdown_dir(directory.name, {}, directory))
        return fallback_documents

    def _load_document_for_project(self, project_id: str) -> SourceDocument | None:
        if self.metadata_db_path.exists():
            with sqlite3.connect(self.metadata_db_path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    (
                        "SELECT id, title, metadata_json FROM papers "
                        "WHERE id = ? OR title = ? LIMIT 1"
                    ),
                    [project_id, project_id],
                ).fetchone()
            if row:
                metadata = self._parse_metadata_json(row["metadata_json"])
                return self._build_document_from_metadata(str(row["id"]), metadata, fallback_title=str(row["title"]))

        markdown_dir = self.storage_dir / "markdown" / project_id
        if markdown_dir.exists() and markdown_dir.is_dir():
            return self._build_document_from_markdown_dir(project_id, {}, markdown_dir)
        return None

    def _load_documents_from_metadata_db(self) -> list[SourceDocument]:
        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT id, title, metadata_json FROM papers").fetchall()

        documents: list[SourceDocument] = []
        for row in rows:
            metadata = self._parse_metadata_json(row["metadata_json"])
            document = self._build_document_from_metadata(str(row["id"]), metadata, fallback_title=str(row["title"]))
            if document and (document.markdown_path or document.markdown_dir):
                documents.append(document)
        return documents

    def _build_document_from_metadata(
        self,
        record_id: str,
        metadata: dict[str, Any],
        *,
        fallback_title: str = "",
    ) -> SourceDocument | None:
        markdown_path = self._resolve_optional_path(metadata.get("markdownPath"))
        markdown_dir = self._resolve_optional_path(metadata.get("markdownOutputDir"))
        if markdown_dir is None and markdown_path is not None:
            markdown_dir = markdown_path.parent

        if markdown_path is None and markdown_dir is None:
            return None
        if markdown_path is None and markdown_dir is not None:
            markdown_path = self._detect_markdown_path(markdown_dir)
        if markdown_dir is not None and not markdown_dir.exists():
            return None
        if markdown_path is not None and not markdown_path.exists():
            markdown_path = self._detect_markdown_path(markdown_dir) if markdown_dir else None

        title = str(metadata.get("title") or fallback_title or record_id).strip()
        abstract = str(metadata.get("abstract") or "").strip()
        keywords = self._extract_keywords(metadata, markdown_path)
        toc_entries = self._load_toc_entries(markdown_dir, markdown_path)

        return SourceDocument(
            record_id=record_id,
            title=title,
            abstract=abstract,
            keywords=keywords,
            markdown_path=markdown_path,
            markdown_dir=markdown_dir,
            toc_entries=toc_entries,
        )

    def _build_document_from_markdown_dir(
        self,
        record_id: str,
        metadata: dict[str, Any],
        markdown_dir: Path,
    ) -> SourceDocument:
        markdown_path = self._detect_markdown_path(markdown_dir)
        title = str(metadata.get("title") or (markdown_path.stem if markdown_path else record_id)).strip()
        abstract = str(metadata.get("abstract") or "").strip()
        keywords = self._extract_keywords(metadata, markdown_path)
        toc_entries = self._load_toc_entries(markdown_dir, markdown_path)
        return SourceDocument(
            record_id=record_id,
            title=title,
            abstract=abstract,
            keywords=keywords,
            markdown_path=markdown_path,
            markdown_dir=markdown_dir,
            toc_entries=toc_entries,
        )

    def _build_catalog_text(self, documents: list[SourceDocument]) -> str:
        return "\n\n".join(
            self._build_document_catalog_text(document, index)
            for index, document in enumerate(documents, start=1)
        )

    def _build_document_catalog_text(self, document: SourceDocument, index: int) -> str:
        lines = [f"## 文档{index}: {document.title}", f"记录ID: {document.record_id}"]
        if document.abstract:
            lines.append(f"摘要: {self._truncate(document.abstract, 1000)}")
        if document.keywords:
            lines.append(f"关键词: {', '.join(document.keywords[:12])}")
        lines.append("目录:")

        if document.toc_entries:
            for entry in self._filter_toc_entries(document.toc_entries)[:120]:
                level = max(1, min(int(entry.get("level", 1)), 6))
                indent = "  " * (level - 1)
                title = str(entry.get("title", "")).strip()
                if title:
                    lines.append(f"{indent}- {title}")
        elif document.markdown_path and document.markdown_path.exists():
            headings = self._filter_toc_entries(self._extract_headings_from_markdown(document.markdown_path))
            for entry in headings[:120]:
                level = max(1, min(int(entry.get("level", 1)), 6))
                indent = "  " * (level - 1)
                title = str(entry.get("title", "")).strip()
                if title:
                    lines.append(f"{indent}- {title}")
        return "\n".join(lines)

    def _document_catalog_map(self, documents: list[SourceDocument]) -> dict[str, str]:
        return {
            document.record_id: self._build_document_catalog_text(document, index)
            for index, document in enumerate(documents, start=1)
        }

    def _manifest_document_catalog_map(self, manifest: dict[str, Any]) -> dict[str, str]:
        documents = manifest.get("documents")
        if not isinstance(documents, list):
            return {}

        mapping: dict[str, str] = {}
        for item in documents:
            if not isinstance(item, dict):
                continue
            record_id = str(item.get("recordId") or "").strip()
            catalog_text = str(item.get("catalogText") or "").strip()
            if record_id and catalog_text:
                mapping[record_id] = catalog_text
        return mapping

    def _join_catalog_sections(self, sections: Any) -> str:
        values = [str(section).strip() for section in sections if str(section).strip()]
        return "\n\n".join(values)

    def _load_toc_entries(self, markdown_dir: Path | None, markdown_path: Path | None) -> list[dict[str, Any]]:
        if markdown_dir:
            toc_entries = self._filter_toc_entries(self._extract_toc_from_content_list(markdown_dir))
            if toc_entries:
                return toc_entries
        if markdown_path and markdown_path.exists():
            return self._filter_toc_entries(self._extract_headings_from_markdown(markdown_path))
        return []

    def _extract_toc_from_content_list(self, markdown_dir: Path) -> list[dict[str, Any]]:
        candidates = sorted(markdown_dir.glob("*_content_list_v2.json")) or sorted(markdown_dir.glob("*_content_list.json"))
        if not candidates:
            return []

        try:
            payload = json.loads(candidates[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("解析内容列表失败 %s：%s", candidates[0], error)
            return []

        entries: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for page in payload if isinstance(payload, list) else []:
            if not isinstance(page, list):
                continue
            for item in page:
                if not isinstance(item, dict) or item.get("type") != "title":
                    continue
                content = item.get("content", {})
                if not isinstance(content, dict):
                    continue
                title_parts = content.get("title_content", [])
                fragments: list[str] = []
                if isinstance(title_parts, list):
                    for part in title_parts:
                        if isinstance(part, dict):
                            text = str(part.get("content", "")).strip()
                            if text:
                                fragments.append(text)
                title = " ".join(fragments).strip()
                title = re.sub(r"\s+", " ", title)
                if not title:
                    continue
                level = int(content.get("level") or 1)
                key = (level, title.lower())
                if key in seen:
                    continue
                seen.add(key)
                entries.append({"level": level, "title": title})
        return entries

    def _extract_headings_from_markdown(self, markdown_path: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        try:
            for line in markdown_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
                if not match:
                    continue
                level = len(match.group(1))
                title = re.sub(r"\s+", " ", match.group(2).strip())
                if not title:
                    continue
                key = (level, title.lower())
                if key in seen:
                    continue
                seen.add(key)
                entries.append({"level": level, "title": title})
        except OSError as error:
            logger.warning("读取 Markdown 标题失败 %s：%s", markdown_path, error)
        return entries

    def _extract_keywords(self, metadata: dict[str, Any], markdown_path: Path | None) -> list[str]:
        for key in ("keywords", "keywordList"):
            value = metadata.get(key)
            if isinstance(value, list):
                keywords = [str(item).strip() for item in value if str(item).strip()]
                if keywords:
                    return keywords
            if isinstance(value, str) and value.strip():
                return self._split_keywords(value)

        if not markdown_path or not markdown_path.exists():
            return []

        try:
            text = markdown_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []

        match = re.search(r"(?im)^keywords?\s*:\s*(.+)$", text)
        if not match:
            return []
        return self._split_keywords(match.group(1))

    # 生成知识图谱
    def _build_knowledge_graph(
        self,
        *,
        project_id: str,
        documents: list[SourceDocument],
        tags: list[dict[str, Any]],
        catalog_text: str,
        project: dict[str, Any],
    ) -> dict[str, Any]:
        del catalog_text, project

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        edge_keys: set[tuple[str, str, str]] = set()
        domain_keywords = self._domain_keywords_from_tree(tags)

        def add_node(node_id: str, name: str, node_type: str, **extra: Any) -> None:
            if not node_id or node_id in node_ids:
                return
            node_ids.add(node_id)
            nodes.append({"id": node_id, "name": name, "type": node_type, **extra})

        def add_edge(source: str, target: str, relation: str, **extra: Any) -> None:
            key = (source, target, relation)
            if not source or not target or key in edge_keys:
                return
            edge_keys.add(key)
            edges.append({"source": source, "target": target, "relation": relation, **extra})

        add_node(f"project:{project_id}", project_id, "project")

        for top_index, tag in enumerate(tags, start=1):
            label = str(tag.get("label", "")).strip()
            if not label:
                continue
            top_node_id = f"domain:{top_index}"
            add_node(top_node_id, label, "domain")
            add_edge(f"project:{project_id}", top_node_id, "has_domain")

            for child_index, child in enumerate(tag.get("child", []) if isinstance(tag.get("child"), list) else [], start=1):
                child_label = str(child.get("label", "")).strip()
                if not child_label:
                    continue
                child_node_id = f"domain:{top_index}.{child_index}"
                add_node(child_node_id, child_label, "subdomain")
                add_edge(top_node_id, child_node_id, "has_subdomain")

        for document in documents:
            doc_node_id = f"doc:{document.record_id}"
            add_node(
                doc_node_id,
                document.title,
                "document",
                markdownPath=str(document.markdown_path) if document.markdown_path else "",
            )
            add_edge(f"project:{project_id}", doc_node_id, "contains_document")

            phrases = self._extract_document_topics(document)
            for phrase in phrases[:12]:
                topic_id = f"topic:{self._slugify(phrase)}"
                add_node(topic_id, phrase, "topic")
                add_edge(doc_node_id, topic_id, "mentions_topic")

                matched_domain = self._match_topic_to_domain(phrase, domain_keywords)
                if matched_domain:
                    add_edge(matched_domain, topic_id, "covers_topic")

            for entry in self._filter_toc_entries(document.toc_entries)[:30]:
                title = str(entry.get("title", "")).strip()
                if not title:
                    continue
                section_id = f"section:{document.record_id}:{self._slugify(title)}"
                add_node(section_id, title, "section", level=int(entry.get("level", 1)))
                add_edge(doc_node_id, section_id, "has_section")

        return {
            "projectId": project_id,
            "nodes": nodes,
            "edges": edges,
        }

    # 启发式规则生成领域树标签，当大模型不可用或返回无效 JSON 时使用
    def _heuristic_domain_tree(
        self,
        documents: list[SourceDocument],
        catalog_text: str,
    ) -> list[dict[str, Any]]:
        del catalog_text
        topic_scores, topic_documents = self._collect_topic_candidates(documents)
        if not topic_scores:
            fallback_labels = [
                {"label": "1 核心主题", "child": [{"label": "1.1 文献主题"}]},
                {"label": "2 方法机制", "child": [{"label": "2.1 关键方法"}]},
                {"label": "3 实验应用", "child": [{"label": "3.1 应用场景"}]},
            ]
            return fallback_labels

        ranked_topics = sorted(
            topic_scores.items(),
            key=lambda item: (-item[1], -len(topic_documents[item[0]]), item[0]),
        )
        primary_topics = ranked_topics[: min(6, max(3, len(ranked_topics)))]
        tree: list[dict[str, Any]] = []

        for index, (topic, _) in enumerate(primary_topics, start=1):
            subtopics = self._collect_subtopics(topic, topic_documents[topic], documents)
            node: dict[str, Any] = {"label": f"{index} {self._short_label(topic)}"}
            if subtopics:
                node["child"] = [
                    {"label": f"{index}.{child_index} {self._short_label(subtopic)}"}
                    for child_index, subtopic in enumerate(subtopics[:6], start=1)
                ]
            tree.append(node)

        return tree

    # 收集主题候选词
    def _collect_topic_candidates(self, documents: list[SourceDocument]) -> tuple[Counter[str], dict[str, set[str]]]:
        topic_scores: Counter[str] = Counter()
        topic_documents: dict[str, set[str]] = defaultdict(set)
        for document in documents:
            title_phrases = self._extract_candidate_phrases(document.title)
            for phrase in title_phrases:
                normalized = self._normalize_topic_phrase(phrase)
                if normalized:
                    topic_documents[normalized].add(document.record_id)
                    topic_scores[normalized] += 6

            for keyword in document.keywords[:8]:
                normalized = self._normalize_topic_phrase(keyword)
                if normalized:
                    topic_documents[normalized].add(document.record_id)
                    topic_scores[normalized] += 1

            for entry in self._filter_toc_entries(document.toc_entries)[:20]:
                normalized = self._normalize_topic_phrase(str(entry.get("title", "")))
                if normalized:
                    level = int(entry.get("level", 1))
                    topic_documents[normalized].add(document.record_id)
                    topic_scores[normalized] += 4 if level <= 2 else 1

            for phrase in self._extract_candidate_phrases(document.abstract)[:12]:
                normalized = self._normalize_topic_phrase(phrase)
                if normalized:
                    topic_documents[normalized].add(document.record_id)
                    topic_scores[normalized] += 5

        return topic_scores, topic_documents

    def _extract_document_topics(self, document: SourceDocument) -> list[str]:
        phrases: list[str] = []
        title_phrases = self._extract_candidate_phrases(document.title)
        heading_phrases = [
            self._normalize_topic_phrase(str(entry.get("title", "")))
            for entry in self._filter_toc_entries(document.toc_entries)[:20]
            if int(entry.get("level", 1)) <= 2
        ]
        abstract_phrases = self._extract_candidate_phrases(document.abstract)[:12]

        for source in (title_phrases, heading_phrases, abstract_phrases):
            for phrase in source:
                cleaned = self._normalize_topic_phrase(phrase)
                if cleaned and cleaned not in phrases:
                    phrases.append(cleaned)
        return phrases

    # 提取候选短语，用于生成主题标签
    def _extract_candidate_phrases(self, text: str) -> list[str]:
        if not text:
            return []

        candidates: list[str] = []
        for raw in re.split(r"[,;:()|/]+", text):
            phrase = raw.strip()
            if len(phrase) < 3:
                continue
            normalized = re.sub(r"\s+", " ", phrase)
            lowered = normalized.lower()
            if lowered in _GENERIC_SECTION_TITLES:
                continue
            word_tokens = re.findall(r"[A-Za-z][A-Za-z0-9+.#-]*", normalized)
            if 1 <= len(word_tokens) <= 8:
                if all(token.lower() in _STOPWORDS for token in word_tokens):
                    continue
                candidates.append(normalized)

        acronym_matches = re.findall(r"\b[A-Z][A-Z0-9-]{1,}\b", text)
        for acronym in acronym_matches:
            if acronym not in candidates:
                candidates.append(acronym)
        return candidates

    def _collect_subtopics(
        self,
        topic: str,
        related_document_ids: set[str],
        documents: list[SourceDocument],
    ) -> list[str]:
        counter: Counter[str] = Counter()
        for document in documents:
            if document.record_id not in related_document_ids:
                continue
            for entry in self._filter_toc_entries(document.toc_entries)[:20]:
                title = self._normalize_topic_phrase(str(entry.get("title", "")))
                if not title or title == topic or title.lower() in _GENERIC_SECTION_TITLES:
                    continue
                level = int(entry.get("level", 1))
                counter[title] += 2 if level <= 2 else 1
            for keyword in document.keywords[:12]:
                normalized_keyword = self._normalize_topic_phrase(keyword)
                if not normalized_keyword or normalized_keyword == topic:
                    continue
                counter[normalized_keyword] += 3
            for phrase in self._extract_candidate_phrases(document.abstract)[:16]:
                normalized_phrase = self._normalize_topic_phrase(phrase)
                if not normalized_phrase or normalized_phrase == topic:
                    continue
                counter[normalized_phrase] += 1
        return [name for name, _ in counter.most_common(6)]

    def _refine_tree_specificity(
        self,
        tags: list[dict[str, Any]] | None,
        documents: list[SourceDocument],
    ) -> list[dict[str, Any]]:
        if not isinstance(tags, list):
            return []

        refined: list[dict[str, Any]] = []
        global_candidates = self._collect_specific_candidates_from_documents(documents)
        for index, item in enumerate(tags, start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            if not label:
                continue

            node: dict[str, Any] = {"label": label}
            raw_children = item.get("child")
            if isinstance(raw_children, list):
                related_document_ids = self._related_document_ids_for_topic(label, documents)
                scoped_documents = [
                    document for document in documents if document.record_id in related_document_ids
                ] or documents
                candidate_pool = self._collect_specific_candidates_from_documents(scoped_documents)
                if not candidate_pool:
                    candidate_pool = global_candidates

                used_labels = {
                    self._normalize_topic_phrase(label),
                }
                children: list[dict[str, str]] = []
                for child_index, child in enumerate(raw_children, start=1):
                    if not isinstance(child, dict):
                        continue
                    child_label = str(child.get("label", "")).strip()
                    replacement = child_label
                    if self._is_generic_label(child_label):
                        replacement = self._pick_specific_replacement(
                            candidate_pool,
                            used_labels=used_labels,
                            fallback=child_label,
                        )
                    normalized_replacement = self._normalize_topic_phrase(replacement)
                    if normalized_replacement:
                        used_labels.add(normalized_replacement)
                    if replacement:
                        children.append({"label": f"{index}.{child_index} {normalized_replacement or replacement}"})
                if children:
                    node["child"] = children
            refined.append(node)
        return refined

    def _collect_specific_candidates_from_documents(
        self,
        documents: list[SourceDocument],
    ) -> list[str]:
        counter: Counter[str] = Counter()
        for document in documents:
            for keyword in document.keywords[:12]:
                normalized = self._normalize_topic_phrase(keyword)
                if normalized and not self._is_generic_label(normalized):
                    counter[normalized] += 5

            for phrase in self._extract_candidate_phrases(document.title)[:12]:
                normalized = self._normalize_topic_phrase(phrase)
                if normalized and not self._is_generic_label(normalized):
                    counter[normalized] += 4

            for entry in self._filter_toc_entries(document.toc_entries)[:30]:
                normalized = self._normalize_topic_phrase(str(entry.get("title", "")))
                if normalized and not self._is_generic_label(normalized):
                    level = int(entry.get("level", 1))
                    counter[normalized] += 4 if level <= 2 else 2

            for phrase in self._extract_candidate_phrases(document.abstract)[:20]:
                normalized = self._normalize_topic_phrase(phrase)
                if normalized and not self._is_generic_label(normalized):
                    counter[normalized] += 1

        return [name for name, _ in counter.most_common(24)]

    def _related_document_ids_for_topic(
        self,
        topic: str,
        documents: list[SourceDocument],
    ) -> set[str]:
        topic_tokens = {
            self._keyword_token(token)
            for token in self._tokenize_label(topic)
            if self._keyword_token(token)
        }
        if not topic_tokens:
            return set()

        matched_ids: set[str] = set()
        for document in documents:
            document_topics = self._extract_document_topics(document)
            for document_topic in document_topics:
                document_tokens = {
                    self._keyword_token(token)
                    for token in self._tokenize_label(document_topic)
                    if self._keyword_token(token)
                }
                if topic_tokens & document_tokens:
                    matched_ids.add(document.record_id)
                    break
        return matched_ids

    def _pick_specific_replacement(
        self,
        candidates: list[str],
        *,
        used_labels: set[str],
        fallback: str,
    ) -> str:
        for candidate in candidates:
            normalized = self._normalize_topic_phrase(candidate)
            if not normalized:
                continue
            if normalized in used_labels:
                continue
            if self._is_generic_label(normalized):
                continue
            return normalized
        return fallback

    def _domain_keywords_from_tree(self, tags: list[dict[str, Any]]) -> dict[str, set[str]]:
        mapping: dict[str, set[str]] = {}
        for index, tag in enumerate(tags, start=1):
            node_id = f"domain:{index}"
            keywords = {self._keyword_token(token) for token in self._tokenize_label(str(tag.get("label", "")))}
            for child in tag.get("child", []) if isinstance(tag.get("child"), list) else []:
                keywords.update(self._keyword_token(token) for token in self._tokenize_label(str(child.get("label", ""))))
            mapping[node_id] = {token for token in keywords if token}
        return mapping

    def _match_topic_to_domain(self, phrase: str, domain_keywords: dict[str, set[str]]) -> str | None:
        phrase_tokens = {self._keyword_token(token) for token in self._tokenize_label(phrase)}
        phrase_tokens = {token for token in phrase_tokens if token}
        if not phrase_tokens:
            return None

        best_node_id = ""
        best_score = 0
        for node_id, tokens in domain_keywords.items():
            score = len(tokens & phrase_tokens)
            if score > best_score:
                best_score = score
                best_node_id = node_id
        return best_node_id or None

    def _read_prompt_file(self, category: str, language: str) -> str:
        language_code = "zh" if self._is_chinese_language(language) else "en"
        prompt_path = self.prompt_dir / category / f"{language_code}.md"
        return prompt_path.read_text(encoding="utf-8")

    def _render_prompt(self, template: str, data: dict[str, Any]) -> str:
        rendered = template
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                replacement = json.dumps(value, ensure_ascii=False, indent=2)
            else:
                replacement = str(value)
            rendered = rendered.replace(f"{{{{{key}}}}}", replacement)
        return rendered

    def _resolve_model_name(self, model: Any | None) -> str:
        if isinstance(model, dict):
            name = model.get("model") or model.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        if isinstance(model, str) and model.strip():
            return model.strip()
        return os.getenv("DOMAIN_TREE_MODEL") or os.getenv("OPENAI_MODEL") or settings.llm_translation_model

    def _analysis_dir(self, project_id: str) -> Path:
        return self.analysis_root / project_id

    def _resolve_storage_dir(self, storage_dir: str | Path | None) -> Path:
        configured = Path(storage_dir or settings.backend_storage_dir)
        candidates = [configured]

        cwd_storage = Path.cwd() / "storage"
        if cwd_storage not in candidates:
            candidates.append(cwd_storage)

        sandbox_storage = Path(__file__).resolve().parents[2] / "storage"
        if sandbox_storage not in candidates:
            candidates.append(sandbox_storage)

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".domain_tree_write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return candidate.resolve()
            except Exception as error:
                last_error = error
                continue

        raise PermissionError(f"unable to use storage directory for domain tree outputs: {last_error}")

    def _resolve_optional_path(self, value: Any) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.storage_dir / candidate).resolve()

    def _detect_markdown_path(self, markdown_dir: Path | None) -> Path | None:
        if markdown_dir is None or not markdown_dir.exists():
            return None
        preferred = markdown_dir / "full.md"
        if preferred.exists():
            return preferred.resolve()
        markdown_files = sorted(path for path in markdown_dir.glob("*.md") if path.is_file())
        return markdown_files[0].resolve() if markdown_files else None

    def _parse_metadata_json(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}

    def _split_keywords(self, raw: str) -> list[str]:
        values = [raw]
        for separator in (";", "；", ",", "，", "|"):
            values = [part for item in values for part in item.split(separator)]
        return [part.strip() for part in values if part.strip()]

    def _normalize_project_id(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
        return cleaned[:120] or "default"

    def _normalize_topic_phrase(self, phrase: str) -> str:
        cleaned = re.sub(r"^\d+(?:\.\d+)*\s*", "", str(phrase).strip())
        cleaned = re.sub(r"^[A-Z]\s+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.strip(" -_#")
        lowered = cleaned.lower()
        if not cleaned or lowered in _GENERIC_SECTION_TITLES:
            return ""
        if lowered.startswith("appendix") or lowered.startswith("additional "):
            return ""
        if self._is_non_core_section(cleaned):
            return ""
        if re.fullmatch(r"[a-z]", lowered):
            return ""
        if len(cleaned.split()) == 1 and lowered in {"action", "camera", "implementation", "derivation", "study"}:
            return ""
        if len(cleaned) < 3:
            return ""
        return cleaned

    def _is_generic_label(self, label: str) -> bool:
        normalized = self._normalize_topic_phrase(label)
        if not normalized:
            return True
        lowered = normalized.lower()
        if lowered in _GENERIC_LABEL_TOKENS or lowered in _GENERIC_SECTION_TITLES:
            return True
        chinese = re.sub(r"^\d+(?:\.\d+)*\s*", "", normalized).strip()
        if chinese in _GENERIC_LABEL_TOKENS:
            return True
        return False

    def _filter_toc_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "")).strip()
            if not title or self._is_non_core_section(title):
                continue
            filtered.append(entry)
        return filtered

    def _is_non_core_section(self, title: str) -> bool:
        normalized = re.sub(r"^\d+(?:\.\d+)*\s*", "", str(title).strip())
        normalized = re.sub(r"\s+", " ", normalized).strip(" -_#:.").lower()
        if not normalized:
            return True
        if normalized in _GENERIC_SECTION_TITLES:
            return True
        if normalized.startswith("appendix") or normalized.startswith("additional "):
            return True
        return any(pattern in normalized for pattern in _NON_CORE_SECTION_PATTERNS)

    def _short_label(self, phrase: str) -> str:
        cleaned = self._normalize_topic_phrase(phrase)
        words = cleaned.split()
        if not words:
            return "未分类"
        if len(words) > 4:
            cleaned = " ".join(words[:4])
        return cleaned[:36]

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
        return slug[:80] or "unknown"

    def _tokenize_label(self, label: str) -> list[str]:
        cleaned = re.sub(r"^\d+(?:\.\d+)*\s*", "", label.strip())
        return [token for token in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", cleaned) if token]

    def _keyword_token(self, token: str) -> str:
        return token.strip().lower()

    def _truncate(self, text: str, max_length: int) -> str:
        normalized = re.sub(r"\s+", " ", text.strip())
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."

    def _is_chinese_language(self, language: str) -> bool:
        normalized = str(language).strip().lower()
        return normalized in {"zh", "中文", "cn", "chinese"}


async def handle_domain_tree(
    project_id: str,
    action: str = "rebuild",
    all_toc: str | None = None,
    new_toc: str | None = None,
    model: Any | None = None,
    language: str = "中文",
    delete_toc: str | None = None,
    project: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    agent = DomainTreeAgent()
    return await agent.handle_domain_tree(
        project_id,
        action=action,
        all_toc=all_toc,
        new_toc=new_toc,
        model=model,
        language=language,
        delete_toc=delete_toc,
        project=project,
    )


__all__ = ["DomainTreeAgent", "handle_domain_tree"]
