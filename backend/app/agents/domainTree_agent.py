"""从本地 Markdown 资料生成领域树、知识图谱及其持久化结果。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.config import settings
from app.services.model_client import chat_completion
from app.services.model_config import ModelConfigStore
from app.services.domain_tree_store import DomainTreeStore
from app.services.project_repository import DEFAULT_PROJECT_ID, ProjectRepository
from app.services.semantic_graph import SemanticGraphExtractor, SemanticSourceDocument
from app.services.task_control import (
    DomainTreeGenerationCancelled,
    call_with_retry,
    raise_if_cancelled,
)


logger = logging.getLogger(__name__)

_MODEL_OUTPUT_PREVIEW_CHARS = 2000


class DomainTreeModelGenerationError(RuntimeError):
    """Raised when model-backed domain-tree generation cannot produce a valid tree."""

    def __init__(self, message: str, *, reason: str = "model_call_failed") -> None:
        super().__init__(message)
        self.reason = reason


_AUTO_LANGUAGE_VALUES = {"auto", "跟随文献语言", "follow source", "source"}


def _log_text_preview(value: Any, *, limit: int = _MODEL_OUTPUT_PREVIEW_CHARS) -> str:
    """把模型输出压缩为适合单行日志的有限长度预览。"""
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}...<truncated {len(compact) - limit} chars>"

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
    """保存用于领域树分析的单篇来源文档及其目录信息。"""
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
        project_repository: ProjectRepository | None = None,
    ) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.storage_dir = self._resolve_storage_dir(storage_dir)
        self.metadata_db_path = Path(metadata_db_path or settings.hunter_metadata_db).resolve()
        self.prompt_dir = Path(prompt_dir or (Path(__file__).resolve().parents[2] / "src" / "prompt")).resolve()
        self.analysis_root = self.storage_dir / "domain_tree"
        self.analysis_root.mkdir(parents=True, exist_ok=True)
        self.store = DomainTreeStore()
        # 项目仓储按需初始化，纯模型/图谱单元测试不会产生数据库副作用。
        self.project_repository = project_repository
        self._generation_metadata: dict[str, Any] = self._default_generation_metadata()

    async def handle_domain_tree(
        self,
        project_id: str,
        *,
        action: str = "rebuild",
        all_toc: str | None = None,
        new_toc: str | None = None,
        model: Any | None = None,
        language: str = "auto",
        delete_toc: str | None = None,
        project: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]] | None:
        """在线程中生成领域树，避免同步模型请求阻塞事件循环。"""
        return await asyncio.to_thread(
            self.handle_domain_tree_sync,
            project_id,
            action=action,
            all_toc=all_toc,
            new_toc=new_toc,
            model=model,
            language=language,
            delete_toc=delete_toc,
            project=project,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def handle_domain_tree_sync(
        self,
        project_id: str,
        *,
        action: str = "rebuild",
        all_toc: str | None = None,
        new_toc: str | None = None,
        model: Any | None = None,
        language: str = "auto",
        delete_toc: str | None = None,
        project: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]] | None:
        """同步执行领域树主流程，供工作线程和测试直接调用。"""
        started_at = time.perf_counter()
        self._generation_metadata = self._default_generation_metadata()

        def report(**update: Any) -> None:
            if progress_callback:
                progress_callback(update)

        raise_if_cancelled(cancel_event)
        normalized_project_id = self._normalize_project_id(project_id)
        logger.info(
            "[%s] 领域树任务开始：action=%s language=%s",
            normalized_project_id,
            action,
            language,
        )
        # 直接调用 get_tags 返回当前已存储的标签，不进行任何计算或生成
        if action == "keep":
            logger.info("[%s] 使用已有领域树", normalized_project_id)
            return self.get_tags(normalized_project_id)

        stage_started_at = time.perf_counter()
        documents = self._load_documents(normalized_project_id)
        logger.info(
            "[%s] 文档加载完成：document_count=%s elapsed_ms=%.1f",
            normalized_project_id,
            len(documents),
            (time.perf_counter() - stage_started_at) * 1000,
        )
        if not documents:
            logger.warning("[%s] 存储目录中未找到 Markdown 来源", normalized_project_id)
            return None

        report(stage="domain_tree", message="正在准备文献目录", documentCount=len(documents))

        #
        stage_started_at = time.perf_counter()
        catalog_text = all_toc or self._build_catalog_text(documents)
        logger.info(
            "[%s] 文献目录准备完成：catalog_chars=%s supplied=%s elapsed_ms=%.1f",
            normalized_project_id,
            len(catalog_text),
            bool(all_toc),
            (time.perf_counter() - stage_started_at) * 1000,
        )
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
        requested_language = language
        language = self._resolve_analysis_language(language, catalog_text)
        logger.info(
            "[%s] 领域树语言已确定：requested=%s resolved=%s",
            normalized_project_id,
            requested_language,
            language,
        )
        report(requestedLanguage=requested_language, resolvedLanguage=language)
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
        raise_if_cancelled(cancel_event)
        report(stage="domain_tree", message="正在调用模型生成领域树")
        stage_started_at = time.perf_counter()
        tags = self._generate_domain_tree(
            prompt=prompt,
            documents=documents,
            catalog_text=catalog_text,
            language=language,
            model=model,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        tags = self._refine_tree_specificity(tags, documents)
        logger.info(
            "[%s] 领域树标签生成完成：top_level_count=%s elapsed_ms=%.1f output=%s",
            normalized_project_id,
            len(tags),
            (time.perf_counter() - stage_started_at) * 1000,
            _log_text_preview(json.dumps(tags, ensure_ascii=False)),
        )
        if not tags:
            logger.error("[%s] 领域树标签生成失败", normalized_project_id)
            return None

        # 先保存领域树，使前端无需等待全文知识图谱即可展示分类结果。
        raise_if_cancelled(cancel_event)
        generated_at = self.save_domain_tree_snapshot(
            normalized_project_id,
            tags,
            documents=documents,
            catalog_text=catalog_text,
            action=action,
            language=language,
            requested_language=requested_language,
        )
        report(
            stage="knowledge_graph",
            message="领域树已生成，知识图谱正在后台构建",
            domainTreeReady=True,
            partialResult=self.get_result(normalized_project_id),
        )

        # 在领域树可用后继续构建知识图谱。
        stage_started_at = time.perf_counter()
        try:
            graph = self._build_knowledge_graph(
                project_id=normalized_project_id,
                documents=documents,
                tags=tags,
                catalog_text=catalog_text,
                project=project or {},
                model_runtime=self._resolve_model_runtime(model),
                entity_type_language=language,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
        except DomainTreeGenerationCancelled:
            self._set_graph_status(normalized_project_id, "cancelled")
            raise
        except Exception:
            self._set_graph_status(normalized_project_id, "failed")
            raise
        extraction = graph.get("extraction") if isinstance(graph.get("extraction"), dict) else {}
        logger.info(
            "[%s] 知识图谱构建完成：nodes=%s edges=%s entities=%s relations=%s "
            "processed_chunks=%s failed_chunks=%s elapsed_ms=%.1f",
            normalized_project_id,
            len(graph.get("nodes") or []),
            len(graph.get("edges") or []),
            len(graph.get("entities") or []),
            len(graph.get("semanticRelations") or []),
            extraction.get("processedChunkCount", 0),
            extraction.get("failedChunkCount", 0),
            (time.perf_counter() - stage_started_at) * 1000,
        )
        raise_if_cancelled(cancel_event)
        report(stage="saving", message="正在保存领域树和知识图谱")
        stage_started_at = time.perf_counter()
        self.batch_save_tags(
            normalized_project_id,
            tags,
            graph,
            documents=documents,
            catalog_text=catalog_text,
            action=action,
            language=language,
            requested_language=requested_language,
            generated_at=generated_at,
        )
        logger.info(
            "[%s] 领域树任务完成：save_elapsed_ms=%.1f total_elapsed_ms=%.1f",
            normalized_project_id,
            (time.perf_counter() - stage_started_at) * 1000,
            (time.perf_counter() - started_at) * 1000,
        )
        return tags

    # 获取当前项目的领域树标签，如果不存在则返回 None
    def get_tags(self, project_id: str) -> list[dict[str, Any]] | None:
        """读取项目当前保存的领域树标签。"""
        return self.store.load_tags(self._analysis_dir(project_id))

    def get_result(self, project_id: str) -> dict[str, Any] | None:
        """读取项目完整的领域树、知识图谱和清单结果。"""
        normalized = self._normalize_project_id(project_id)
        return self.store.load_result(self._analysis_dir(normalized), normalized)

    def get_result_path(self, project_id: str) -> Path:
        """返回任务表可持久化的领域树结果文件路径。"""
        normalized = self._normalize_project_id(project_id)
        return (self._analysis_dir(normalized) / "domain_tree.json").resolve()

    def _load_manifest(self, project_id: str) -> dict[str, Any]:
        """加载项目清单。"""
        normalized = self._normalize_project_id(project_id)
        return self.store.load_manifest(self._analysis_dir(normalized))

    def get_project_tocs(self, project_id: str) -> str:
        """读取项目中各文档的目录结构。"""
        documents = self._load_documents(project_id)
        return self._build_catalog_text(documents)

    def get_label_prompt(self, language: str, data: dict[str, Any]) -> str:
        """读取领域标签生成提示词模板。"""
        template = self._read_prompt_file("lable", language)
        return self._render_prompt(template, data)

    def get_label_revise_prompt(self, language: str, data: dict[str, Any]) -> str:
        """读取领域标签修订提示词模板。"""
        template = self._read_prompt_file("labelRevise", language)
        return self._render_prompt(template, data)

    def filter_domain_tree(self, tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清理领域树中的空节点、泛化节点和重复节点。"""
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

    def save_domain_tree_snapshot(
        self,
        project_id: str,
        tags: list[dict[str, Any]],
        *,
        documents: list[SourceDocument],
        catalog_text: str,
        action: str,
        language: str,
        requested_language: str | None = None,
    ) -> str:
        """先保存可展示的领域树快照，并将知识图谱标记为后台构建中。"""
        output_dir = self._analysis_dir(project_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now(timezone.utc).isoformat()
        domain_payload = {
            "projectId": project_id,
            "generatedAt": generated_at,
            "action": action,
            "language": language,
            "requestedLanguage": requested_language or language,
            "graphStatus": "building",
            "documentCount": len(documents),
            "domainTree": tags,
            **self._generation_metadata,
        }
        manifest_payload = {
            "projectId": project_id,
            "generatedAt": generated_at,
            "action": action,
            "language": language,
            "requestedLanguage": requested_language or language,
            "graphStatus": "building",
            **self._generation_metadata,
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
        self._write_text_atomic(output_dir / "catalog.txt", catalog_text)
        self._write_json_atomic(output_dir / "manifest.json", manifest_payload)
        # 最后提交 domain_tree.json，使 building 状态只在其他快照文件完整后可见。
        self._write_json_atomic(output_dir / "domain_tree.json", domain_payload)
        logger.info("[%s] 领域树快照已保存，知识图谱转入后台构建", project_id)
        return generated_at

    def _set_graph_status(self, project_id: str, status: str) -> None:
        """在图谱任务取消或失败时更新领域树快照状态。"""
        domain_tree_path = self._analysis_dir(project_id) / "domain_tree.json"
        if not domain_tree_path.exists():
            return
        try:
            payload = json.loads(domain_tree_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            payload["graphStatus"] = status
            self._write_json_atomic(domain_tree_path, payload)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[%s] 更新知识图谱状态失败：%s", project_id, error)

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
        requested_language: str | None = None,
        generated_at: str | None = None,
    ) -> None:
        """批量保存领域树标签及相关分析产物。"""
        output_dir = self._analysis_dir(project_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        domain_payload = {
            "projectId": project_id,
            "generatedAt": generated_at,
            "action": action,
            "language": language,
            "requestedLanguage": requested_language or language,
            "graphStatus": "ready",
            "documentCount": len(documents),
            "domainTree": tags,
            **self._generation_metadata,
        }
        graph_payload = {
            **knowledge_graph,
            "projectId": project_id,
            "generatedAt": generated_at,
            "documentCount": len(documents),
            "graphStatus": "ready",
        }
        manifest_payload = {
            "projectId": project_id,
            "generatedAt": generated_at,
            "action": action,
            "language": language,
            "requestedLanguage": requested_language or language,
            "graphStatus": "ready",
            **self._generation_metadata,
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

        self._write_json_atomic(output_dir / "knowledge_graph.json", graph_payload)
        self._write_text_atomic(output_dir / "catalog.txt", catalog_text)
        self._write_json_atomic(output_dir / "manifest.json", manifest_payload)
        # domain_tree.json 是就绪状态的提交点，必须最后写入。
        self._write_json_atomic(output_dir / "domain_tree.json", domain_payload)
        logger.info(
            "[%s] 已保存领域树和知识图谱：directory=%s domain_tree_bytes=%s "
            "knowledge_graph_bytes=%s catalog_bytes=%s manifest_bytes=%s",
            project_id,
            output_dir,
            (output_dir / "domain_tree.json").stat().st_size,
            (output_dir / "knowledge_graph.json").stat().st_size,
            (output_dir / "catalog.txt").stat().st_size,
            (output_dir / "manifest.json").stat().st_size,
        )

    def _write_json_atomic(self, path: Path, payload: Any) -> None:
        """在同目录写临时文件后原子替换 JSON 结果。"""
        self._write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _write_text_atomic(self, path: Path, content: str) -> None:
        """原子写入文本，避免读取端观察到半写文件。"""
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        temporary_path.write_text(content, encoding="utf-8")
        temporary_path.replace(path)

    def _generate_domain_tree(
        self,
        *,
        prompt: str,
        documents: list[SourceDocument],
        catalog_text: str,
        language: str,
        model: Any | None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """生成领域树。"""
        runtime = self._resolve_model_runtime(model)
        allow_fallback = runtime.get("allow_heuristic_fallback") is True
        failure: DomainTreeModelGenerationError | None = None
        try:
            llm_output = self._call_llm(
                prompt,
                language=language,
                model=runtime,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
            tags = self.extract_json_from_llm_output(llm_output)
            if not tags:
                raise DomainTreeModelGenerationError(
                    "模型返回内容不是有效的领域树 JSON",
                    reason="invalid_model_output",
                )
            self._generation_metadata = self._default_generation_metadata()
            return self.filter_domain_tree(tags)
        except DomainTreeGenerationCancelled:
            raise
        except DomainTreeModelGenerationError as error:
            failure = error
        except Exception as error:
            failure = DomainTreeModelGenerationError(
                f"领域树模型调用失败：{error}",
                reason="model_call_failed",
            )

        if not allow_fallback:
            raise failure

        warning = f"模型生成失败，已按设置降级为启发式生成：{failure}"
        logger.warning(warning)
        self._generation_metadata = {
            "generationMode": "heuristic",
            "degraded": True,
            "degradeReason": failure.reason,
            "warnings": [warning],
        }
        if progress_callback:
            progress_callback(
                {
                    "stage": "domain_tree",
                    "message": "模型生成失败，已按设置降级为启发式生成",
                    "generationMode": "heuristic",
                    "degraded": True,
                    "degradeReason": failure.reason,
                }
            )
        return self._heuristic_domain_tree(documents, catalog_text, language=language)

    def _call_llm(
        self,
        prompt: str,
        *,
        language: str,
        model: Any | None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """调用大模型生成领域树，并校验返回结构。"""
        runtime = self._resolve_model_runtime(model)
        if not runtime:
            raise DomainTreeModelGenerationError("模型配置不可用", reason="model_not_configured")
        system_constraint = str(runtime.get("system_constraint") or "").strip()
        output_language_constraint = (
            "Return all domain and subdomain labels in Chinese."
            if self._is_chinese_language(language)
            else "Return all domain and subdomain labels in English."
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a precise knowledge classification assistant. "
                    "Return only valid JSON and do not include markdown fences. "
                    f"{output_language_constraint} "
                    f"{system_constraint}"
                ),
            },
            {"role": "user", "content": prompt},
        ]

        started_at = time.perf_counter()
        logger.info(
            "领域树模型请求开始：provider=%s model=%s prompt_chars=%s timeout_seconds=%s",
            runtime.get("provider", ""),
            runtime.get("model", ""),
            len(prompt),
            settings.request_timeout,
        )
        try:
            answer = call_with_retry(
                lambda: chat_completion(
                    runtime,
                    messages,
                    temperature=0.2,
                    timeout=settings.request_timeout,
                ),
                max_attempts=settings.domain_tree_retry_attempts,
                base_delay_seconds=settings.domain_tree_retry_base_delay_seconds,
                cancel_event=cancel_event,
                on_retry=lambda attempt, error, delay: self._report_model_retry(
                    progress_callback,
                    attempt,
                    error,
                    delay,
                ),
            )
            logger.info(
                "领域树模型请求完成：elapsed_ms=%.1f output_chars=%s output_preview=%s",
                (time.perf_counter() - started_at) * 1000,
                len(answer),
                _log_text_preview(answer),
            )
            return answer
        except DomainTreeGenerationCancelled:
            raise
        except Exception as error:
            logger.warning(
                "领域树大模型调用失败：elapsed_ms=%.1f error=%s",
                (time.perf_counter() - started_at) * 1000,
                error,
            )
            raise DomainTreeModelGenerationError(
                f"领域树模型调用失败：{error}",
                reason="model_call_failed",
            ) from error

    def _report_model_retry(
        self,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        attempt: int,
        error: Exception,
        delay: float,
    ) -> None:
        """记录领域树模型调用的退避重试。"""
        logger.warning("领域树大模型第 %s 次调用失败，%.1f 秒后重试：%s", attempt, delay, error)
        if progress_callback:
            progress_callback(
                {
                    "stage": "domain_tree",
                    "message": f"领域树模型调用超时，正在进行第 {attempt + 1} 次尝试",
                    "retryAttempt": attempt + 1,
                    "retryDelaySeconds": delay,
                }
            )

    def _resolve_model_runtime(self, model: Any | None) -> dict[str, Any]:
        """把请求传入的模型设置统一转换为语义抽取可复用的运行时配置。"""
        runtime = dict(model) if isinstance(model, dict) else (ModelConfigStore().build_model_payload() or {})
        if isinstance(model, str) and model.strip():
            runtime["model"] = model.strip()
        return {str(key): value for key, value in runtime.items() if value is not None}

    @staticmethod
    def _default_generation_metadata() -> dict[str, Any]:
        return {
            "generationMode": "llm",
            "degraded": False,
            "degradeReason": "",
            "warnings": [],
        }

    def extract_json_from_llm_output(self, output: str | None) -> list[dict[str, Any]] | None:
        """从大模型文本响应中提取 JSON 对象。"""
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
        """只加载当前项目成员论文，禁止未知项目回退到全局文献。"""
        project_repository = self.project_repository or ProjectRepository(self.metadata_db_path)
        project = project_repository.get(project_id)
        if not project or project.get("status") != "active":
            logger.warning("[%s] 项目不存在或已归档，拒绝加载文献", project_id)
            return []

        paper_ids = project_repository.list_paper_ids(project_id)
        documents = self._load_documents_from_metadata_db(paper_ids)

        if documents:
            unique: dict[str, SourceDocument] = {}
            for document in documents:
                key = str(document.markdown_dir or document.markdown_path or document.record_id).lower()
                unique.setdefault(key, document)
            return list(unique.values())

        # 默认项目兼容尚未登记到 papers 表的历史 Markdown 目录；新项目必须显式关联论文。
        if project_id != DEFAULT_PROJECT_ID:
            return []
        markdown_root = self.storage_dir / "markdown"
        if not markdown_root.exists():
            return []

        fallback_documents: list[SourceDocument] = []
        for directory in sorted(path for path in markdown_root.iterdir() if path.is_dir()):
            fallback_documents.append(self._build_document_from_markdown_dir(directory.name, {}, directory))
        return fallback_documents

    def _load_documents_from_metadata_db(self, record_ids: list[str]) -> list[SourceDocument]:
        """按项目成员 ID 加载来源文档和元数据。"""
        if not record_ids or not self.metadata_db_path.exists():
            return []
        placeholders = ", ".join("?" for _ in record_ids)
        with closing(sqlite3.connect(self.metadata_db_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT id, title, metadata_json FROM papers WHERE id IN ({placeholders})",
                record_ids,
            ).fetchall()

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
        """构建来源文档、元数据。"""
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
        """构建来源文档、Markdown。"""
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
        """构建目录文本、文本。"""
        return "\n\n".join(
            self._build_document_catalog_text(document, index)
            for index, document in enumerate(documents, start=1)
        )

    def _build_document_catalog_text(self, document: SourceDocument, index: int) -> str:
        """构建来源文档、目录文本、文本。"""
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
        """建立文档 ID 到目录文本的映射。"""
        return {
            document.record_id: self._build_document_catalog_text(document, index)
            for index, document in enumerate(documents, start=1)
        }

    def _manifest_document_catalog_map(self, manifest: dict[str, Any]) -> dict[str, str]:
        """从项目清单建立文档目录映射。"""
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
        """拼接目录文本、章节。"""
        values = [str(section).strip() for section in sections if str(section).strip()]
        return "\n\n".join(values)

    def _load_toc_entries(self, markdown_dir: Path | None, markdown_path: Path | None) -> list[dict[str, Any]]:
        """加载目录条目、条目。"""
        if markdown_dir:
            toc_entries = self._filter_toc_entries(self._extract_toc_from_content_list(markdown_dir))
            if toc_entries:
                return toc_entries
        if markdown_path and markdown_path.exists():
            return self._filter_toc_entries(self._extract_headings_from_markdown(markdown_path))
        return []

    def _extract_toc_from_content_list(self, markdown_dir: Path) -> list[dict[str, Any]]:
        """提取目录条目。"""
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
        """提取标题、Markdown。"""
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
        """提取关键词。"""
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
        model_runtime: dict[str, str] | None = None,
        entity_type_language: str = "English",
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """构建领域结构图，并合并全文实体、证据和引用关系。"""
        del catalog_text, project

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        edge_keys: set[tuple[str, str, str]] = set()
        domain_keywords = self._domain_keywords_from_tree(tags)
        logger.debug("领域关键词映射：%s", domain_keywords)

        def add_node(node_id: str, name: str, node_type: str, **extra: Any) -> None:
            """向知识图谱加入一个去重后的节点。"""
            if not node_id or node_id in node_ids:
                return
            node_ids.add(node_id)
            nodes.append({"id": node_id, "name": name, "type": node_type, **extra})

        def add_edge(source: str, target: str, relation: str, **extra: Any) -> None:
            """向知识图谱加入一条去重后的关系边。"""
            key = (source, target, relation)
            if not source or not target or key in edge_keys:
                return
            edge_keys.add(key)
            edges.append({"source": source, "target": target, "relation": relation, **extra})

        add_node(f"project:{project_id}", project_id, "project")

        # 将层级化的一二级标签数据转换为图结构
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

            # 提取文档主题并创建主题节点
            phrases = self._extract_document_topics(document)
            for phrase in phrases[:12]:
                topic_id = f"topic:{self._slugify(phrase)}"
                add_node(topic_id, phrase, "topic")
                add_edge(doc_node_id, topic_id, "mentions_topic")

                matched_domain = self._match_topic_to_domain(phrase, domain_keywords)
                if matched_domain:
                    add_edge(matched_domain, topic_id, "covers_topic")

            # 尝试将主题与领域匹配（桥接主题与已有领域）
            for entry in self._filter_toc_entries(document.toc_entries)[:30]: #筛出来的合格条目
                title = str(entry.get("title", "")).strip()
                if not title:
                    continue
                section_id = f"section:{document.record_id}:{self._slugify(title)}"
                add_node(section_id, title, "section", level=int(entry.get("level", 1)))
                add_edge(doc_node_id, section_id, "has_section")

        # 全文语义抽取独立于领域树规则：即使某个实体无法归入领域，也保留其原文证据。
        semantic_graph = SemanticGraphExtractor(
            model_runtime,
            entity_type_language=entity_type_language,
            cache_dir=self._analysis_dir(project_id) / "semantic_cache",
            max_workers=settings.semantic_graph_max_workers,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        ).extract(
            SemanticSourceDocument(
                record_id=document.record_id,
                title=document.title,
                markdown_path=document.markdown_path,
            )
            for document in documents
        )
        for entity in semantic_graph.get("entities", []):
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("id") or "")
            add_node(
                entity_id,
                str(entity.get("name") or entity_id),
                "entity",
                entityType=str(entity.get("type") or "entity"),
                aliases=entity.get("aliases") or [],
                attributes=entity.get("attributes") or [],
                evidenceIds=entity.get("evidenceIds") or [],
            )
            for document_id in entity.get("documentIds") or []:
                add_edge(
                    f"doc:{document_id}",
                    entity_id,
                    "mentions_entity",
                    evidenceIds=entity.get("evidenceIds") or [],
                )

        for relation in semantic_graph.get("semanticRelations", []):
            if not isinstance(relation, dict):
                continue
            add_edge(
                str(relation.get("source") or ""),
                str(relation.get("target") or ""),
                "semantic_relation",
                predicate=str(relation.get("predicate") or ""),
                relationType=str(relation.get("relationType") or "general"),
                confidence=relation.get("confidence", 0.5),
                evidenceIds=relation.get("evidenceIds") or [],
                documentIds=relation.get("documentIds") or [],
            )

        for citation in semantic_graph.get("citations", []):
            if not isinstance(citation, dict):
                continue
            source = f"doc:{citation.get('documentId', '')}"
            matched_document_id = str(citation.get("matchedDocumentId") or "")
            if matched_document_id:
                target = f"doc:{matched_document_id}"
            else:
                target = str(citation.get("id") or "")
                add_node(
                    target,
                    str(citation.get("title") or citation.get("rawReference") or "未命名参考文献"),
                    "reference",
                    referenceNumber=citation.get("referenceNumber"),
                    rawReference=str(citation.get("rawReference") or ""),
                    doi=str(citation.get("doi") or ""),
                    url=str(citation.get("url") or ""),
                    year=citation.get("year"),
                )
            add_edge(
                source,
                target,
                "cites",
                referenceNumber=citation.get("referenceNumber"),
                contexts=citation.get("contexts") or [],
            )

        return {
            "projectId": project_id,
            "nodes": nodes,
            "edges": edges,
            **semantic_graph,
        }

    # 启发式规则生成领域树标签，当大模型不可用或返回无效 JSON 时使用
    def _heuristic_domain_tree(
        self,
        documents: list[SourceDocument],
        catalog_text: str,
        *,
        language: str = "auto",
    ) -> list[dict[str, Any]]:
        """在大模型不可用时按启发式规则生成领域树。"""
        del catalog_text
        topic_scores, topic_documents = self._collect_topic_candidates(documents)
        if not topic_scores:
            fallback_labels = (
                [
                    {"label": "1 核心主题", "child": [{"label": "1.1 文献主题"}]},
                    {"label": "2 方法机制", "child": [{"label": "2.1 关键方法"}]},
                    {"label": "3 实验应用", "child": [{"label": "3.1 应用场景"}]},
                ]
                if self._is_chinese_language(language)
                else [
                    {"label": "1 Core Topics", "child": [{"label": "1.1 Research Topics"}]},
                    {"label": "2 Methods", "child": [{"label": "2.1 Key Methods"}]},
                    {"label": "3 Applications", "child": [{"label": "3.1 Use Cases"}]},
                ]
            )
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
        """收集主题、候选项。"""
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
        """提取来源文档、主题。"""
        phrases: list[str] = []
        title_phrases = self._extract_candidate_phrases(document.title) #从标题提取候选短语
        
        #从目录条目提取主题短语
        heading_phrases = [
            self._normalize_topic_phrase(str(entry.get("title", "")))
            for entry in self._filter_toc_entries(document.toc_entries)[:20]
            if int(entry.get("level", 1)) <= 2
        ]
        #从摘要中提取候选短语
        abstract_phrases = self._extract_candidate_phrases(document.abstract)[:12]

        #合并、归一化和去重
        for source in (title_phrases, heading_phrases, abstract_phrases):
            for phrase in source:
                cleaned = self._normalize_topic_phrase(phrase)
                if cleaned and cleaned not in phrases:
                    phrases.append(cleaned)
        return phrases

    # 提取候选短语，用于生成主题标签
    def _extract_candidate_phrases(self, text: str) -> list[str]:
        """提取候选项、短语。"""
        if not text:
            return []

        candidates: list[str] = []
        for raw in re.split(r"[,;:()|/]+", text):
            phrase = raw.strip()
            if len(phrase) < 3: #长度小于3的跳过
                continue
            normalized = re.sub(r"\s+", " ", phrase)
            lowered = normalized.lower()
            if lowered in _GENERIC_SECTION_TITLES: # 排除通用章节标题
                continue
            word_tokens = re.findall(r"[A-Za-z][A-Za-z0-9+.#-]*", normalized)
            if 1 <= len(word_tokens) <= 8: 
                if all(token.lower() in _STOPWORDS for token in word_tokens): # 排除停用词
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
        """收集子主题。"""
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
        """用更具体的候选主题替换泛化领域标签。"""
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
        """收集候选项、来源文档。"""
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
        """查找与指定主题相关的来源文档 ID。"""
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
        """为泛化主题选择更具体且未使用的替代标签。"""
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
        """从（两层）领域树提取用于主题归属判断的关键词。"""
        mapping: dict[str, set[str]] = {}
        for index, tag in enumerate(tags, start=1):
            node_id = f"domain:{index}"
            keywords = {self._keyword_token(token) for token in self._tokenize_label(str(tag.get("label", "")))}
            for child in tag.get("child", []) if isinstance(tag.get("child"), list) else []:
                keywords.update(self._keyword_token(token) for token in self._tokenize_label(str(child.get("label", ""))))
            mapping[node_id] = {token for token in keywords if token}
        return mapping

    def _match_topic_to_domain(self, phrase: str, domain_keywords: dict[str, set[str]]) -> str | None:
        """匹配主题、领域。"""
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
        logger.debug(
            "主题领域匹配：topic=%s best_node_id=%s best_score=%s",
            phrase,
            best_node_id,
            best_score,
        )
        return best_node_id or None

    def _read_prompt_file(self, category: str, language: str) -> str:
        """读取提示词。"""
        language_code = "zh" if self._is_chinese_language(language) else "en"
        prompt_path = self.prompt_dir / category / f"{language_code}.md"
        return prompt_path.read_text(encoding="utf-8")

    def _render_prompt(self, template: str, data: dict[str, Any]) -> str:
        """渲染提示词。"""
        rendered = template
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                replacement = json.dumps(value, ensure_ascii=False, indent=2)
            else:
                replacement = str(value)
            rendered = rendered.replace(f"{{{{{key}}}}}", replacement)
        return rendered

    def _analysis_dir(self, project_id: str) -> Path:
        """返回指定项目的领域树分析目录。"""
        return self.analysis_root / project_id

    def _resolve_storage_dir(self, storage_dir: str | Path | None) -> Path:
        """解析存储目录。"""
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
        """解析路径。"""
        raw = str(value or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.storage_dir / candidate).resolve()

    def _detect_markdown_path(self, markdown_dir: Path | None) -> Path | None:
        """检测Markdown、路径。"""
        if markdown_dir is None or not markdown_dir.exists():
            return None
        preferred = markdown_dir / "full.md"
        if preferred.exists():
            return preferred.resolve()
        markdown_files = sorted(path for path in markdown_dir.glob("*.md") if path.is_file())
        return markdown_files[0].resolve() if markdown_files else None

    def _parse_metadata_json(self, raw: Any) -> dict[str, Any]:
        """解析元数据、JSON 数据。"""
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}

    def _split_keywords(self, raw: str) -> list[str]:
        """切分关键词。"""
        values = [raw]
        for separator in (";", "；", ",", "，", "|"):
            values = [part for item in values for part in item.split(separator)]
        return [part.strip() for part in values if part.strip()]

    def _normalize_project_id(self, value: str) -> str:
        """规范化项目标识。"""
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
        return cleaned[:120] or "default"

    def _normalize_topic_phrase(self, phrase: str) -> str:
        """规范化主题、短语。"""
        cleaned = re.sub(r"^\d+(?:\.\d+)*\s*", "", str(phrase).strip())
        cleaned = re.sub(r"^[A-Z]\s+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.strip(" -_#")
        lowered = cleaned.lower()
        if not cleaned or lowered in _GENERIC_SECTION_TITLES:
            return ""
        if lowered.startswith("appendix") or lowered.startswith("additional "):
            return ""
        if self._is_non_core_section(cleaned): # 非重点内容
            return ""
        if re.fullmatch(r"[a-z]", lowered):
            return ""
        if len(cleaned.split()) == 1 and lowered in {"action", "camera", "implementation", "derivation", "study"}:
            return ""
        if len(cleaned) < 3:
            return ""
        return cleaned

    def _is_generic_label(self, label: str) -> bool:
        """判断标签。"""
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
        """筛选目录条目、条目,返回合格的条目。"""
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
        """判断章节。"""
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
        """把主题文本压缩为适合树节点展示的短标签。"""
        cleaned = self._normalize_topic_phrase(phrase)
        words = cleaned.split()
        if not words:
            return "未分类"
        if len(words) > 4:
            cleaned = " ".join(words[:4])
        return cleaned[:36]

    def _slugify(self, value: str) -> str:
        """把文本转换为稳定、可用于标识的短字符串。"""
        slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
        return slug[:80] or "unknown"

    def _tokenize_label(self, label: str) -> list[str]:
        """分词标签。"""
        cleaned = re.sub(r"^\d+(?:\.\d+)*\s*", "", label.strip()) # 清除前面的数字编号
        return [token for token in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", cleaned) if token] #将中文和英文拆分开

    def _keyword_token(self, token: str) -> str:
        """把关键词规范化为便于比较的词元。"""
        return token.strip().lower()

    def _truncate(self, text: str, max_length: int) -> str:
        """按最大长度截断文本并保留省略标记。"""
        normalized = re.sub(r"\s+", " ", text.strip())
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."

    def _is_chinese_language(self, language: str) -> bool:
        """判断语言配置是否表示中文。"""
        normalized = str(language).strip().lower()
        return normalized in {"zh", "中文", "cn", "chinese"}

    def _resolve_analysis_language(self, language: str, source_text: str) -> str:
        """解析显式语言设置，或根据文献目录中的字符分布自动判断主要语言。"""
        normalized = str(language or "auto").strip().lower()
        if normalized not in _AUTO_LANGUAGE_VALUES:
            return "中文" if self._is_chinese_language(language) else "English"

        chinese_char_count = len(re.findall(r"[\u3400-\u9fff]", source_text))
        latin_char_count = len(re.findall(r"[A-Za-z]", source_text))
        total_language_chars = chinese_char_count + latin_char_count
        if total_language_chars == 0:
            return "English"
        chinese_ratio = chinese_char_count / total_language_chars
        return "中文" if chinese_char_count >= 20 and chinese_ratio >= 0.2 else "English"


async def handle_domain_tree(
    project_id: str,
    action: str = "rebuild",
    all_toc: str | None = None,
    new_toc: str | None = None,
    model: Any | None = None,
    language: str = "auto",
    delete_toc: str | None = None,
    project: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """根据请求动作生成、修订或读取领域树结果。"""
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
