"""从 Markdown 全文抽取实体、语义关系、原文证据和文献引用。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from app.core.config import settings
from app.prompt_loader import load_prompt, render_prompt
from app.services.model_client import (
    ModelCallError,
    ModelCallResult,
    ModelUsage,
    chat_completion_result,
)
from app.services.task_control import (
    DomainTreeGenerationCancelled,
    call_with_retry,
    raise_if_cancelled,
)


logger = logging.getLogger(__name__)

_MODEL_OUTPUT_PREVIEW_CHARS = 2000
_CACHE_SCHEMA_VERSION = "semantic-graph-v3-entity-type-language"
_TYPE_MAPPING_SCHEMA_VERSION = "entity-type-mapping-v2-target-language"
_CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")


def _log_text_preview(value: Any, *, limit: int = _MODEL_OUTPUT_PREVIEW_CHARS) -> str:
    """把模型输出压缩为适合单行日志的有限长度预览。"""
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}...<truncated {len(compact) - limit} chars>"

_REFERENCE_HEADING_PATTERN = re.compile(
    r"(?im)^#{1,6}\s*(references|bibliography|参考文献|参考资料)\s*$"
)
_HEADING_PATTERN = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
_REFERENCE_ENTRY_PATTERN = re.compile(r"(?m)^\s*(?:\[(\d+)\]|(\d+)[.)])\s+")
_INLINE_CITATION_PATTERN = re.compile(r"\[(\d+(?:\s*[-–,;]\s*\d+)*)\]")
_DOI_PATTERN = re.compile(r"(?i)\b10\.\d{4,9}/[-._;()/:A-Z0-9]+")
_URL_PATTERN = re.compile(r"https?://[^\s)>]+", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass(slots=True)
class SemanticSourceDocument:
    """保存语义抽取所需的最小文献信息。"""

    record_id: str
    title: str
    markdown_path: Path | None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class TextChunk:
    """保存一个带章节和行号定位的正文分块。"""

    index: int
    section: str
    text: str
    start_line: int


@dataclass(slots=True)
class ChunkOutcome:
    """保留单个语义分块的结果、失败类别和尝试次数。"""

    status: str
    payload: dict[str, Any] | None
    error_category: str = ""
    error_message: str = ""
    attempts: int = 0


class SemanticExtractionCircuitOpen(RuntimeError):
    """表示共享熔断器或预算已经阻止后续模型调用。"""


class _ExtractionGuard:
    """在线程间共享请求预算和失败窗口，避免各分块独立失控。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.request_count = 0
        self.estimated_input_tokens = 0
        self.recent: deque[bool] = deque(maxlen=settings.semantic_graph_failure_window)
        self.consecutive_fatal = 0
        self.open_reason = ""

    def before_call(self, estimated_input_tokens: int) -> int:
        with self.lock:
            if self.open_reason:
                raise SemanticExtractionCircuitOpen(self.open_reason)
            next_request = self.request_count + 1
            next_tokens = self.estimated_input_tokens + max(0, estimated_input_tokens)
            if settings.semantic_graph_max_requests and next_request > settings.semantic_graph_max_requests:
                self.open_reason = "request_budget_exceeded"
                raise SemanticExtractionCircuitOpen(self.open_reason)
            if (
                settings.semantic_graph_max_input_tokens
                and next_tokens > settings.semantic_graph_max_input_tokens
            ):
                self.open_reason = "input_token_budget_exceeded"
                raise SemanticExtractionCircuitOpen(self.open_reason)
            self.request_count = next_request
            self.estimated_input_tokens = next_tokens
            return next_request

    def record(self, *, success: bool, category: str = "") -> None:
        with self.lock:
            if self.open_reason:
                return
            self.recent.append(success)
            if success:
                self.consecutive_fatal = 0
            elif category in {"authentication", "quota_exhausted", "not_found"}:
                self.consecutive_fatal += 1
            else:
                self.consecutive_fatal = 0
            if self.consecutive_fatal >= settings.semantic_graph_consecutive_fatal_limit:
                self.open_reason = f"consecutive_{category}"
                return
            if len(self.recent) < settings.semantic_graph_failure_window_minimum:
                return
            failure_rate = 1 - (sum(self.recent) / len(self.recent))
            if failure_rate > settings.semantic_graph_failure_rate_limit:
                self.open_reason = "failure_rate_exceeded"


class SemanticGraphExtractor:
    """调用当前模型抽取全文语义，并用确定性规则解析参考文献。"""

    def __init__(
        self,
        runtime: dict[str, str] | None,
        *,
        chat_fn: Callable[..., str | ModelCallResult] = chat_completion_result,
        chunk_size: int = settings.semantic_graph_chunk_size,
        chunk_overlap: int = settings.semantic_graph_chunk_overlap,
        max_output_tokens: int = settings.semantic_graph_max_output_tokens,
        request_timeout_seconds: int = settings.domain_tree_request_timeout_seconds,
        entity_type_language: str = "English",
        cache_dir: str | Path | None = None,
        max_workers: int = settings.semantic_graph_max_workers,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        metric_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """初始化模型配置、调用函数与分块参数。"""
        self.runtime = dict(runtime or {})
        self.chat_fn = chat_fn
        self.chunk_size = max(1200, chunk_size)
        self.chunk_overlap = max(0, min(chunk_overlap, self.chunk_size // 3))
        self.max_output_tokens = max(
            1,
            min(settings.model_output_tokens_upper_bound, int(max_output_tokens)),
        )
        self.request_timeout_seconds = max(5, min(600, int(request_timeout_seconds)))
        self.entity_type_language = self._normalize_entity_type_language(entity_type_language)
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self.max_workers = max(1, min(int(max_workers), 16))
        self.cancel_event = cancel_event
        self.progress_callback = progress_callback
        self.metric_callback = metric_callback
        self._guard = _ExtractionGuard()
        self._usage_lock = threading.Lock()
        self._usage = {
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "cachedTokens": 0,
            "reportedCallCount": 0,
        }

    def extract(self, documents: Iterable[SemanticSourceDocument]) -> dict[str, Any]:
        """抽取所有文献并合并跨分块重复实体。"""
        extraction_started_at = time.perf_counter()
        raise_if_cancelled(self.cancel_event)
        source_documents = list(documents)
        logger.info(
            "全文语义抽取开始：document_count=%s chunk_size=%s chunk_overlap=%s "
            "provider=%s model=%s max_workers=%s cache_enabled=%s",
            len(source_documents),
            self.chunk_size,
            self.chunk_overlap,
            self.runtime.get("provider", ""),
            self.runtime.get("model", ""),
            self.max_workers,
            self.cache_dir is not None,
        )
        local_titles = {
            self._normalize_name(document.title): document.record_id
            for document in source_documents
            if self._normalize_name(document.title)
        }
        local_metadata = {
            document.record_id: {
                **(document.metadata or {}),
                "title": document.title,
            }
            for document in source_documents
        }
        state: dict[str, Any] = {
            "entities": {},
            "entityAliases": {},
            "relations": {},
            "evidence": {},
            "citations": [],
            "documentLanguages": {},
            "processedChunkCount": 0,
            "failedChunkCount": 0,
            "documentCount": len(source_documents),
        }

        prepared_documents: list[
            tuple[SemanticSourceDocument, str, str, int, list[TextChunk]]
        ] = []
        for document in source_documents:
            raise_if_cancelled(self.cancel_event)
            prepare_started_at = time.perf_counter()
            markdown = self._read_markdown(document)
            if not markdown:
                logger.warning("[%s] Markdown 正文为空，跳过全文语义抽取", document.record_id)
                continue

            body, reference_text, reference_start_line = self.split_reference_section(markdown)
            state["documentLanguages"][document.record_id] = self._detect_text_language(body)
            chunks = self.split_chunks(body)
            prepared_documents.append(
                (document, body, reference_text, reference_start_line, chunks)
            )
            logger.info(
                "[%s] 语义文档准备完成：markdown_chars=%s body_chars=%s reference_chars=%s "
                "chunk_count=%s elapsed_ms=%.1f",
                document.record_id,
                len(markdown),
                len(body),
                len(reference_text),
                len(chunks),
                (time.perf_counter() - prepare_started_at) * 1000,
            )

        total_chunks = sum(len(item[4]) for item in prepared_documents)
        completed_chunks = 0
        cache_hit_count = 0
        cache_miss_count = 0
        self._report_progress(
            stage="semantic_extraction",
            message=f"准备抽取 {total_chunks} 个语义分块",
            totalChunks=total_chunks,
            completedChunks=0,
            processedChunks=0,
            failedChunks=0,
            cacheHits=0,
            cacheMisses=0,
            pendingChunks=0,
            maxWorkers=self.max_workers,
        )

        work_items: list[tuple[int, SemanticSourceDocument, TextChunk]] = []
        for document, body, reference_text, reference_start_line, chunks in prepared_documents:
            raise_if_cancelled(self.cancel_event)
            citations = self.parse_citations(
                document,
                body,
                reference_text,
                reference_start_line=reference_start_line,
                local_titles=local_titles,
                local_metadata=local_metadata,
            )
            state["citations"].extend(citations)

            logger.info(
                "[%s] 开始抽取全文语义：chunk_count=%s citation_count=%s",
                document.record_id,
                len(chunks),
                len(citations),
            )
            for chunk in chunks:
                work_items.append((len(work_items), document, chunk))

        results: dict[int, dict[str, Any] | None] = {}
        failure_reasons: Counter[str] = Counter()
        uncached_items: list[tuple[int, SemanticSourceDocument, TextChunk, str]] = []
        for order, document, chunk in work_items:
            raise_if_cancelled(self.cancel_event)
            cache_key = self._chunk_cache_key(document, chunk)
            payload = self._load_cached_payload(cache_key)
            if payload is not None:
                invalid_types = self._invalid_entity_types(payload)
                if invalid_types:
                    logger.warning(
                        "[%s] 忽略实体类型语言不合格的语义缓存：chunk=%s expected=%s types=%s",
                        document.record_id,
                        chunk.index,
                        self.entity_type_language,
                        invalid_types,
                    )
                    payload = None
            if payload is None:
                cache_miss_count += 1
                uncached_items.append((order, document, chunk, cache_key))
                continue
            cache_hit_count += 1
            results[order] = payload
            completed_chunks += 1
            state["processedChunkCount"] += 1
            self._report_progress(
                stage="semantic_extraction",
                message=f"已复用 {cache_hit_count} 个语义分块缓存",
                completedChunks=completed_chunks,
                processedChunks=state["processedChunkCount"],
                failedChunks=state["failedChunkCount"],
                cacheHits=cache_hit_count,
                cacheMisses=cache_miss_count,
                pendingChunks=cache_miss_count,
            )

        if uncached_items:
            logger.info(
                "语义分块并发抽取开始：cache_hits=%s cache_misses=%s max_workers=%s",
                cache_hit_count,
                cache_miss_count,
                self.max_workers,
            )
            self._report_progress(
                stage="semantic_extraction",
                message=f"正在并发抽取语义分块 {completed_chunks}/{total_chunks}",
                completedChunks=completed_chunks,
                processedChunks=state["processedChunkCount"],
                failedChunks=state["failedChunkCount"],
                cacheHits=cache_hit_count,
                cacheMisses=cache_miss_count,
                pendingChunks=cache_miss_count,
                maxWorkers=self.max_workers,
            )
            executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="semantic-graph",
            )
            futures: dict[
                Future[ChunkOutcome],
                tuple[int, SemanticSourceDocument, TextChunk, str],
            ] = {
                executor.submit(self._extract_chunk, document, chunk): (
                    order,
                    document,
                    chunk,
                    cache_key,
                )
                for order, document, chunk, cache_key in uncached_items
            }
            try:
                for future in as_completed(futures):
                    raise_if_cancelled(self.cancel_event)
                    order, document, chunk, cache_key = futures[future]
                    outcome = future.result()
                    results[order] = outcome.payload
                    completed_chunks += 1
                    if outcome.status == "failed":
                        state["failedChunkCount"] += 1
                        failure_reasons[outcome.error_category or "unknown"] += 1
                    else:
                        state["processedChunkCount"] += 1
                        if outcome.payload is not None:
                            self._save_cached_payload(cache_key, outcome.payload)
                    self._report_progress(
                        stage="semantic_extraction",
                        message=f"正在并发抽取语义分块 {completed_chunks}/{total_chunks}",
                        currentChunk=completed_chunks,
                        currentDocumentId=document.record_id,
                        completedChunks=completed_chunks,
                        processedChunks=state["processedChunkCount"],
                        failedChunks=state["failedChunkCount"],
                        cacheHits=cache_hit_count,
                        cacheMisses=cache_miss_count,
                        pendingChunks=max(
                            0,
                            cache_miss_count - (completed_chunks - cache_hit_count),
                        ),
                    )
            finally:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    for future in futures:
                        future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)

        for order, document, chunk in work_items:
            payload = results.get(order)
            if payload is not None:
                self._merge_chunk_payload(state, document, chunk, payload)

        logger.info(
            "语义分块处理完成：total=%s cache_hits=%s cache_misses=%s processed=%s failed=%s",
            total_chunks,
            cache_hit_count,
            cache_miss_count,
            state["processedChunkCount"],
            state["failedChunkCount"],
        )

        entities = list(state["entities"].values())
        type_normalization = self._canonicalize_entity_types(entities)
        entities = sorted(entities, key=lambda item: (item["type"], item["name"].lower()))
        relations = sorted(
            state["relations"].values(),
            key=lambda item: (item["relationType"], item["predicate"], item["source"], item["target"]),
        )
        evidence = sorted(
            state["evidence"].values(),
            key=lambda item: (item["documentId"], item.get("lineStart", 0), item["id"]),
        )
        citations = sorted(
            state["citations"],
            key=lambda item: (item["documentId"], item.get("referenceNumber", 0)),
        )
        self.bind_relation_citations(relations, evidence, citations)
        logger.info(
            "全文语义抽取完成：processed_chunks=%s failed_chunks=%s entities=%s relations=%s "
            "evidence=%s citations=%s elapsed_ms=%.1f",
            state["processedChunkCount"],
            state["failedChunkCount"],
            len(entities),
            len(relations),
            len(evidence),
            len(citations),
            (time.perf_counter() - extraction_started_at) * 1000,
        )
        coverage_ratio = state["processedChunkCount"] / total_chunks if total_chunks else 0.0
        if coverage_ratio >= settings.semantic_graph_ready_ratio:
            quality_status = "ready"
        elif coverage_ratio >= settings.semantic_graph_degraded_ratio:
            quality_status = "degraded"
        else:
            quality_status = "failed"
        return {
            "entities": entities,
            "semanticRelations": relations,
            "evidence": evidence,
            "citations": citations,
            "extraction": {
                "mode": "llm_full_text_with_rule_based_citations",
                "documentCount": state["documentCount"],
                "processedChunkCount": state["processedChunkCount"],
                "failedChunkCount": state["failedChunkCount"],
                "cacheHitCount": cache_hit_count,
                "cacheMissCount": cache_miss_count,
                "maxWorkers": self.max_workers,
                "entityCount": len(entities),
                "semanticRelationCount": len(relations),
                "citationCount": len(citations),
                "evidenceCount": len(evidence),
                "coverageRatio": round(coverage_ratio, 6),
                "qualityStatus": quality_status,
                "failureReasons": dict(failure_reasons),
                "circuitOpenReason": self._guard.open_reason,
                "requestCount": self._guard.request_count,
                "estimatedInputTokens": self._guard.estimated_input_tokens,
                "usage": dict(self._usage),
                **type_normalization,
            },
        }

    def _report_progress(self, **update: Any) -> None:
        """把进度增量上报给任务管理器。"""
        if not self.progress_callback:
            return
        completed = int(update.get("completedChunks") or 0)
        total = int(update.get("totalChunks") or 0)
        stage = str(update.get("stage") or "")
        important = (
            completed == 0
            or (total > 0 and completed >= total)
            or completed % settings.semantic_graph_progress_interval == 0
            or stage != getattr(self, "_last_progress_stage", "")
            or "retryAttempt" in update
        )
        if important:
            self._last_progress_stage = stage
            self.progress_callback(update)

    def _chunk_cache_key(self, document: SemanticSourceDocument, chunk: TextChunk) -> str:
        """根据模型、提示词版本和原文内容生成稳定的语义分块缓存键。"""
        payload = {
            "schema": _CACHE_SCHEMA_VERSION,
            "provider": self.runtime.get("provider", ""),
            "protocol": self.runtime.get("protocol", ""),
            "model": self.runtime.get("model", ""),
            "base_url": self.runtime.get("base_url", ""),
            "entityTypeLanguage": self.entity_type_language,
            "title": document.title,
            "section": chunk.section,
            "text": chunk.text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cache_path(self, cache_key: str) -> Path | None:
        """返回分片后的缓存文件路径，避免单目录堆积过多文件。"""
        if self.cache_dir is None:
            return None
        return self.cache_dir / cache_key[:2] / f"{cache_key}.json"

    def _load_cached_payload(self, cache_key: str) -> dict[str, Any] | None:
        """读取并校验单个语义分块缓存，损坏缓存按未命中处理。"""
        path = self._cache_path(cache_key)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("读取语义分块缓存失败：path=%s error=%s", path, error)
            return None
        return payload if isinstance(payload, dict) else None

    def _save_cached_payload(self, cache_key: str, payload: dict[str, Any]) -> None:
        """原子写入单个语义分块缓存，避免中断留下半写文件。"""
        path = self._cache_path(cache_key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(f".{threading.get_ident()}.tmp")
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary_path.replace(path)
        except OSError as error:
            logger.warning("写入语义分块缓存失败：path=%s error=%s", path, error)

    def split_reference_section(self, markdown: str) -> tuple[str, str, int]:
        """把正文和文末参考文献分开，并返回参考文献起始行。"""
        match = _REFERENCE_HEADING_PATTERN.search(markdown)
        if not match:
            return markdown, "", 0
        reference_start_line = markdown[: match.start()].count("\n") + 1
        return markdown[: match.start()].rstrip(), markdown[match.end() :].strip(), reference_start_line

    def split_chunks(self, markdown: str) -> list[TextChunk]:
        """按 Markdown 章节和字符上限切分全文，同时保留少量上下文重叠。"""
        if not markdown.strip():
            return []

        headings = list(_HEADING_PATTERN.finditer(markdown))
        sections: list[tuple[str, str, int]] = []
        if not headings:
            sections.append(("正文", markdown.strip(), 1))
        else:
            if markdown[: headings[0].start()].strip():
                sections.append(("文档首页", markdown[: headings[0].start()].strip(), 1))
            for index, heading in enumerate(headings):
                end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
                section_text = markdown[heading.end() : end].strip()
                if section_text:
                    line = markdown[: heading.start()].count("\n") + 1
                    sections.append((heading.group(2).strip(), section_text, line))

        chunks: list[TextChunk] = []
        for section, text, section_line in sections:
            offset = 0
            while offset < len(text):
                end = min(len(text), offset + self.chunk_size)
                if end < len(text):
                    paragraph_break = text.rfind("\n\n", offset + self.chunk_size // 2, end)
                    sentence_break = max(text.rfind("。", offset, end), text.rfind(". ", offset, end))
                    split_at = paragraph_break if paragraph_break > offset else sentence_break + 1
                    if split_at > offset + self.chunk_size // 2:
                        end = split_at
                chunk_text = text[offset:end].strip()
                if chunk_text:
                    start_line = section_line + text[:offset].count("\n")
                    chunks.append(TextChunk(len(chunks) + 1, section, chunk_text, start_line))
                if end >= len(text):
                    break
                offset = max(offset + 1, end - self.chunk_overlap)
        return chunks

    def parse_citations(
        self,
        document: SemanticSourceDocument,
        body: str,
        reference_text: str,
        *,
        reference_start_line: int,
        local_titles: dict[str, str],
        local_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """解析参考文献条目、正文引用标记以及本地文献链接。"""
        if not reference_text:
            return []
        entries = self._parse_reference_entries(reference_text)
        contexts = self._find_inline_citation_contexts(body)
        citations: list[dict[str, Any]] = []
        metadata_by_id = local_metadata or {}

        for number, raw_reference, entry_line in entries:
            doi_match = _DOI_PATTERN.search(raw_reference)
            url_match = _URL_PATTERN.search(raw_reference)
            parsed = self._parse_reference_metadata(raw_reference)
            title = str(parsed.get("title") or "")
            matched_document_id = self._match_local_document(raw_reference, title, local_titles)
            parsed_doi = doi_match.group(0).rstrip(".,;") if doi_match else ""
            if not matched_document_id and parsed_doi:
                normalized_doi = self._normalize_doi(parsed_doi)
                matched_document_id = next(
                    (
                        record_id
                        for record_id, item in metadata_by_id.items()
                        if self._normalize_doi(item.get("doi")) == normalized_doi
                    ),
                    "",
                )
            local_item = metadata_by_id.get(matched_document_id, {})
            if matched_document_id:
                title = str(local_item.get("title") or title).strip()
            year = self._coerce_year(local_item.get("year")) or parsed.get("year")
            authors = self._string_list(local_item.get("authors")) or list(parsed.get("authors") or [])
            doi = str(local_item.get("doi") or parsed_doi).rstrip(".,;")
            title_valid = self._is_valid_reference_title(title, raw_reference, authors, year)
            if not title_valid:
                title = ""
            citation_id = f"citation:{document.record_id}:{number}"
            citation_contexts = contexts.get(number, []) + self._find_author_year_contexts(
                body,
                list(parsed.get("authorKeys") or []),
                str(year or ""),
            )
            unique_contexts = {
                (str(item.get("section")), int(item.get("lineStart") or 0), str(item.get("quote"))): item
                for item in citation_contexts
            }
            citations.append(
                {
                    "id": citation_id,
                    "documentId": document.record_id,
                    "referenceNumber": number,
                    "marker": f"[{number}]",
                    "title": title,
                    "rawReference": raw_reference,
                    "authors": authors,
                    "year": year,
                    "doi": doi,
                    "url": url_match.group(0).rstrip(".,;") if url_match else "",
                    "matchedDocumentId": matched_document_id,
                    "metadataSource": (
                        "csl"
                        if matched_document_id and local_item.get("csl")
                        else "zotero"
                        if matched_document_id and str(local_item.get("source") or "").lower() == "zotero"
                        else "local"
                        if matched_document_id
                        else "doi"
                        if doi
                        else "text"
                    ),
                    "metadataQuality": "valid" if title_valid else "invalid_title",
                    "authorKeys": list(parsed.get("authorKeys") or []),
                    "referenceLine": reference_start_line + entry_line,
                    "contexts": list(unique_contexts.values())[:8],
                }
            )
        return citations

    def _extract_chunk(
        self,
        document: SemanticSourceDocument,
        chunk: TextChunk,
    ) -> ChunkOutcome:
        """调用模型抽取单个正文分块的实体、属性和关系。"""
        if not self.runtime:
            logger.warning("[%s] 未配置模型，跳过第 %s 个语义分块", document.record_id, chunk.index)
            return ChunkOutcome("failed", None, "model_not_configured", "模型未配置")
        prompt = self._build_extraction_prompt(document, chunk)
        messages = self._build_extraction_messages(prompt)
        started_at = time.perf_counter()
        logger.info(
            "[%s] 语义分块模型请求开始：chunk=%s section=%s chunk_chars=%s prompt_chars=%s "
            "timeout_seconds=%s",
            document.record_id,
            chunk.index,
            _log_text_preview(chunk.section, limit=120),
            len(chunk.text),
            len(prompt),
            settings.request_timeout,
        )
        try:
            answer, attempts = self._call_chunk_model(messages, document, chunk)
            payload = self._extract_json_object(answer)
            invalid_types = self._invalid_entity_types(payload)
            if invalid_types and settings.semantic_graph_type_language_model_correction:
                logger.warning(
                    "[%s] 实体类型语言不符合要求：chunk=%s expected=%s types=%s，正在纠正",
                    document.record_id,
                    chunk.index,
                    self.entity_type_language,
                    invalid_types,
                )
                correction_messages = [
                    *messages,
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": self._build_type_language_correction(invalid_types)},
                ]
                answer, correction_attempts = self._call_chunk_model(
                    correction_messages,
                    document,
                    chunk,
                )
                attempts += correction_attempts
                payload = self._extract_json_object(answer)
                remaining_invalid_types = self._invalid_entity_types(payload)
                if remaining_invalid_types:
                    logger.warning(
                        "[%s] 实体类型纠正后仍不符合要求：chunk=%s expected=%s types=%s，使用通用类型",
                        document.record_id,
                        chunk.index,
                        self.entity_type_language,
                        remaining_invalid_types,
                    )
                    self._replace_invalid_entity_types(payload)
            remaining_invalid_types = self._invalid_entity_types(payload)
            if remaining_invalid_types:
                self._replace_invalid_entity_types(payload)
            entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
            relations = payload.get("relations") if isinstance(payload.get("relations"), list) else []
            logger.info(
                "[%s] 语义分块模型请求完成：chunk=%s elapsed_ms=%.1f output_chars=%s "
                "entity_count=%s relation_count=%s output_preview=%s",
                document.record_id,
                chunk.index,
                (time.perf_counter() - started_at) * 1000,
                len(answer),
                len(entities),
                len(relations),
                _log_text_preview(answer),
            )
            self._guard.record(success=True)
            return ChunkOutcome("success", payload, attempts=attempts)
        except DomainTreeGenerationCancelled:
            raise
        except Exception as error:
            category = self._error_category(error)
            self._guard.record(success=False, category=category)
            logger.warning(
                "[%s] 第 %s 个语义分块抽取失败：elapsed_ms=%.1f error=%s",
                document.record_id,
                chunk.index,
                (time.perf_counter() - started_at) * 1000,
                error,
            )
            return ChunkOutcome(
                "failed",
                None,
                error_category=category,
                error_message=str(error)[:500],
            )

    def _build_extraction_messages(self, prompt: str) -> list[dict[str, str]]:
        """构造与实体类型目标语言一致的模型消息。"""
        if self.entity_type_language == "中文":
            system_prompt = load_prompt("semantic_graph/extraction_system.zh.md")
        else:
            system_prompt = load_prompt("semantic_graph/extraction_system.en.md")
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    def _call_chunk_model(
        self,
        messages: list[dict[str, str]],
        document: SemanticSourceDocument,
        chunk: TextChunk,
    ) -> tuple[str, int]:
        """统一执行分块抽取和语言纠错请求，并复用传输层重试策略。"""
        attempts = 0
        estimated_tokens = max(
            1,
            int(
                sum(len(str(message.get("content") or "")) for message in messages)
                / settings.semantic_graph_estimated_chars_per_token
            ),
        )

        def operation() -> ModelCallResult:
            nonlocal attempts
            attempts += 1
            self._guard.before_call(estimated_tokens)
            started_at = time.perf_counter()
            try:
                raw = self.chat_fn(
                    self.runtime,
                    messages,
                    temperature=0.0,
                    timeout=self.request_timeout_seconds,
                    response_format=(
                        {"type": "json_object"}
                        if settings.semantic_graph_json_output
                        else None
                    ),
                    max_output_tokens=self.max_output_tokens,
                    # 结构化抽取是确定性任务，关闭 DeepSeek 默认思考，避免推理耗尽 JSON 输出预算。
                    thinking=False,
                )
                result = (
                    raw
                    if isinstance(raw, ModelCallResult)
                    else ModelCallResult(str(raw), ModelUsage())
                )
                self._record_model_metric(
                    document,
                    chunk,
                    attempts,
                    "success",
                    result=result,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                )
                return result
            except Exception as error:
                self._record_model_metric(
                    document,
                    chunk,
                    attempts,
                    "failed",
                    error=error,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                )
                raise

        result = call_with_retry(
            operation,
            max_attempts=settings.domain_tree_retry_attempts,
            base_delay_seconds=settings.domain_tree_retry_base_delay_seconds,
            cancel_event=self.cancel_event,
            on_retry=lambda attempt, error, delay: self._on_chunk_retry(
                document,
                chunk,
                attempt,
                error,
                delay,
            ),
        )
        return result.content, attempts

    def _record_model_metric(
        self,
        document: SemanticSourceDocument | None,
        chunk: TextChunk | None,
        attempt: int,
        status: str,
        *,
        stage: str = "semantic_extraction",
        result: ModelCallResult | None = None,
        error: Exception | None = None,
        elapsed_ms: float,
    ) -> None:
        model_error = error if isinstance(error, ModelCallError) else None
        usage = (
            result.usage
            if result is not None
            else (model_error.usage if model_error is not None else ModelUsage())
        )
        with self._usage_lock:
            self._usage["reportedCallCount"] += 1
            self._usage["promptTokens"] += usage.prompt_tokens or 0
            self._usage["completionTokens"] += usage.completion_tokens or 0
            self._usage["totalTokens"] += usage.total_tokens or 0
            self._usage["cachedTokens"] += usage.cached_tokens or 0
        if not self.metric_callback:
            return
        self.metric_callback(
            {
                "stage": stage,
                "documentId": document.record_id if document else None,
                "chunkIndex": chunk.index if chunk else None,
                "attempt": attempt,
                "status": status,
                "errorCategory": self._error_category(error) if error else None,
                "httpStatus": model_error.http_status if model_error else None,
                "requestAccepted": model_error.request_accepted if model_error else None,
                "requestId": (
                    result.request_id
                    if result is not None
                    else (model_error.request_id if model_error else "")
                ),
                "finishReason": (
                    result.finish_reason
                    if result is not None
                    else (model_error.finish_reason if model_error else "")
                ),
                "promptTokens": usage.prompt_tokens,
                "completionTokens": usage.completion_tokens,
                "totalTokens": usage.total_tokens,
                "cachedTokens": usage.cached_tokens,
                "reasoningTokens": usage.reasoning_tokens,
                "elapsedMs": elapsed_ms,
            }
        )

    @staticmethod
    def _error_category(error: Exception | None) -> str:
        if isinstance(error, ModelCallError):
            return error.category
        if isinstance(error, SemanticExtractionCircuitOpen):
            return str(error) or "circuit_open"
        text = str(error or "").lower()
        if "json" in text:
            return "invalid_json"
        if "timeout" in text or "timed out" in text:
            return "timeout"
        if "http 429" in text:
            return "rate_limited"
        if "http 401" in text or "http 403" in text:
            return "authentication"
        if "http 402" in text:
            return "quota_exhausted"
        if "http 404" in text:
            return "not_found"
        if re.search(r"http 5\d\d", text):
            return "upstream"
        return "unknown"

    def _build_type_language_correction(self, invalid_types: list[str]) -> str:
        """要求模型仅纠正类型语言并完整返回原 JSON。"""
        if self.entity_type_language == "中文":
            return render_prompt(
                "semantic_graph/type_correction.zh.md",
                invalid_types=json.dumps(invalid_types, ensure_ascii=False),
            )
        return render_prompt(
            "semantic_graph/type_correction.en.md",
            invalid_types=json.dumps(invalid_types, ensure_ascii=False),
        )

    def _on_chunk_retry(
        self,
        document: SemanticSourceDocument,
        chunk: TextChunk,
        attempt: int,
        error: Exception,
        delay: float,
    ) -> None:
        """记录可恢复失败，并把重试状态暴露给前端。"""
        next_attempt = attempt + 1
        logger.warning(
            "[%s] 第 %s 个语义分块第 %s 次调用失败，%.1f 秒后进行第 %s 次：%s",
            document.record_id,
            chunk.index,
            attempt,
            delay,
            next_attempt,
            error,
        )
        self._report_progress(
            message=f"第 {chunk.index} 个分块请求超时，正在进行第 {next_attempt} 次尝试",
            retryAttempt=next_attempt,
            retryDelaySeconds=delay,
        )

    def _build_extraction_prompt(self, document: SemanticSourceDocument, chunk: TextChunk) -> str:
        """构造紧凑 JSON 契约，减少每个分块重复发送的固定 Token。"""
        if self.entity_type_language == "English":
            resource = "semantic_graph/extraction.en.md"
        else:
            resource = "semantic_graph/extraction.zh.md"
        return render_prompt(
            resource,
            record_id=document.record_id,
            title=document.title,
            section=chunk.section,
            text=chunk.text,
        )

    def _merge_chunk_payload(
        self,
        state: dict[str, Any],
        document: SemanticSourceDocument,
        chunk: TextChunk,
        payload: dict[str, Any],
    ) -> None:
        """校验并合并一个分块的模型结果，同时建立证据定位。"""
        raw_entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
        local_entity_ids: dict[str, str] = {}

        for raw_entity in raw_entities:
            if not isinstance(raw_entity, dict):
                continue
            name = str(raw_entity.get("canonicalName") or raw_entity.get("name") or "").strip()
            mention = str(raw_entity.get("name") or name).strip()
            if not name or not mention:
                continue
            local_id = str(raw_entity.get("localId") or raw_entity.get("id") or mention).strip()
            evidence_id = self._add_evidence(
                state,
                document,
                chunk,
                str(raw_entity.get("evidenceQuote") or ""),
                kind="entity",
            )
            # 实体也必须能够回到原文；否则关系端点可能成为无来源的模型幻觉。
            if not evidence_id:
                continue
            aliases = self._string_list(raw_entity.get("aliases"))
            normalized_names = {
                self._normalize_name(value)
                for value in [name, mention, *aliases]
                if self._normalize_name(value)
            }
            entity_id = next(
                (
                    state["entityAliases"][normalized]
                    for normalized in normalized_names
                    if normalized in state["entityAliases"]
                ),
                self._entity_id(name),
            )
            for normalized in normalized_names:
                state["entityAliases"][normalized] = entity_id
            local_entity_ids[local_id] = entity_id
            for normalized in normalized_names:
                local_entity_ids[normalized] = entity_id
            attributes = self._normalize_attributes(raw_entity.get("attributes"), evidence_id)
            observed_type = str(raw_entity.get("type") or "entity").strip() or "entity"
            raw_observed_type = str(raw_entity.get("_invalidEntityType") or observed_type).strip()
            existing = state["entities"].get(entity_id)
            if existing:
                existing["aliases"] = sorted(set(existing["aliases"] + aliases + [mention]) - {existing["name"]})
                existing["attributes"] = self._merge_attributes(existing["attributes"], attributes)
                existing["evidenceIds"] = sorted(set(existing["evidenceIds"] + ([evidence_id] if evidence_id else [])))
                existing["documentIds"] = sorted(set(existing["documentIds"] + [document.record_id]))
                existing["typeCounts"][observed_type] = existing["typeCounts"].get(observed_type, 0) + 1
                existing["rawTypeLabels"] = sorted(
                    set(existing.get("rawTypeLabels", []) + [raw_observed_type]),
                    key=str.casefold,
                )
                continue
            state["entities"][entity_id] = {
                "id": entity_id,
                "name": name,
                "type": observed_type,
                "typeCounts": {observed_type: 1},
                "rawTypeLabels": [raw_observed_type],
                "aliases": sorted(set(aliases + ([mention] if mention != name else []))),
                "attributes": attributes,
                "evidenceIds": [evidence_id] if evidence_id else [],
                "documentIds": [document.record_id],
            }

        raw_relations = payload.get("relations") if isinstance(payload.get("relations"), list) else []
        for raw_relation in raw_relations:
            if not isinstance(raw_relation, dict):
                continue
            source = self._resolve_relation_entity(raw_relation.get("source"), local_entity_ids)
            target = self._resolve_relation_entity(raw_relation.get("target"), local_entity_ids)
            predicate = str(raw_relation.get("predicate") or raw_relation.get("relation") or "").strip()
            if not source or not target or source == target or not predicate:
                continue
            relation_type = str(raw_relation.get("relationType") or "general").strip().lower()
            if relation_type not in {"general", "causal", "comparison", "experimental", "property"}:
                relation_type = "general"
            evidence_id = self._add_evidence(
                state,
                document,
                chunk,
                str(raw_relation.get("evidenceQuote") or ""),
                kind="relation",
            )
            if not evidence_id:
                continue
            confidence = self._clamp_confidence(raw_relation.get("confidence"))
            relation_id = self._stable_id("relation", source, predicate.lower(), target)
            existing = state["relations"].get(relation_id)
            if existing:
                existing["evidenceIds"] = sorted(set(existing["evidenceIds"] + [evidence_id]))
                existing["documentIds"] = sorted(set(existing["documentIds"] + [document.record_id]))
                existing["confidence"] = max(existing["confidence"], confidence)
                continue
            state["relations"][relation_id] = {
                "id": relation_id,
                "source": source,
                "target": target,
                "predicate": predicate,
                "relationType": relation_type,
                "confidence": confidence,
                "evidenceIds": [evidence_id],
                "documentIds": [document.record_id],
            }

    def _canonicalize_entity_types(self, entities: list[dict[str, Any]]) -> dict[str, Any]:
        """根据本次抽取结果动态归并实体类型，不依赖预设领域词表。"""
        raw_type_counts: dict[str, int] = {}
        for entity in entities:
            counts = entity.get("typeCounts") if isinstance(entity.get("typeCounts"), dict) else {}
            if not counts:
                fallback = str(entity.get("type") or "entity").strip() or "entity"
                counts = {fallback: 1}
            for raw_type, count in counts.items():
                label = str(raw_type or "").strip()
                if label:
                    raw_type_counts[label] = raw_type_counts.get(label, 0) + max(1, int(count or 1))

        if not raw_type_counts:
            return {
                "entityTypeCountBefore": 0,
                "entityTypeCountAfter": 0,
                "entityTypeNormalizationMode": "none",
            }

        # 第一阶段只处理 Unicode 形式、大小写、首尾标点和空白，不判断语义同义关系。
        deterministic_mapping = {
            raw_type: self._normalize_type_label(raw_type)
            for raw_type in raw_type_counts
        }
        normalized_type_counts: dict[str, int] = {}
        for raw_type, count in raw_type_counts.items():
            normalized = deterministic_mapping[raw_type]
            normalized_type_counts[normalized] = normalized_type_counts.get(normalized, 0) + count

        semantic_mapping = {
            label: label if self._is_valid_entity_type_language(label) else self._default_entity_type()
            for label in normalized_type_counts
        }
        mode = "deterministic"
        needs_model_mapping = len(normalized_type_counts) > 1 or any(
            not self._is_valid_entity_type_language(label)
            for label in normalized_type_counts
        )
        if needs_model_mapping and self.runtime:
            inferred_mapping = self._infer_type_mapping(normalized_type_counts)
            if inferred_mapping is not None:
                semantic_mapping.update(inferred_mapping)
                mode = "dynamic_model"

        for entity in entities:
            counts = entity.pop("typeCounts", None)
            observed_raw_types = entity.pop("rawTypeLabels", None)
            if not isinstance(counts, dict) or not counts:
                raw_type = str(entity.get("type") or "entity").strip() or "entity"
                counts = {raw_type: 1}
            canonical_counts: dict[str, int] = {}
            raw_types = (
                [str(label).strip() for label in observed_raw_types if str(label).strip()]
                if isinstance(observed_raw_types, list)
                else []
            )
            has_observed_raw_types = bool(raw_types)
            for raw_type, count in counts.items():
                label = str(raw_type or "").strip()
                if not label:
                    continue
                if not has_observed_raw_types:
                    raw_types.append(label)
                normalized = deterministic_mapping.get(label, self._normalize_type_label(label))
                canonical = semantic_mapping.get(normalized, normalized)
                canonical_counts[canonical] = canonical_counts.get(canonical, 0) + max(1, int(count or 1))
            if canonical_counts:
                entity["type"] = min(
                    canonical_counts,
                    key=lambda label: (-canonical_counts[label], label),
                )
            entity["rawTypes"] = sorted(set(raw_types), key=lambda label: label.casefold())

        final_types = {str(entity.get("type") or "") for entity in entities if entity.get("type")}
        logger.info(
            "实体类型归并完成：mode=%s before=%s deterministic=%s after=%s",
            mode,
            len(raw_type_counts),
            len(normalized_type_counts),
            len(final_types),
        )
        return {
            "entityTypeCountBefore": len(raw_type_counts),
            "entityTypeCountAfter": len(final_types),
            "entityTypeNormalizationMode": mode,
        }

    def _infer_type_mapping(self, type_counts: dict[str, int]) -> dict[str, str] | None:
        """让模型归并本次类型，并把 canonical 约束到目标语言。"""
        labels = sorted(type_counts)
        cache_key = self._type_mapping_cache_key(type_counts)
        cached = self._load_cached_payload(cache_key)
        if cached is not None:
            mapping = self._validate_type_mapping(cached, labels)
            if mapping is not None:
                return mapping

        if self.entity_type_language == "中文":
            system_prompt = load_prompt("semantic_graph/type_merge_system.zh.md")
            prompt_resource = "semantic_graph/type_merge.zh.md"
        else:
            system_prompt = load_prompt("semantic_graph/type_merge_system.en.md")
            prompt_resource = "semantic_graph/type_merge.en.md"
        prompt = render_prompt(
            prompt_resource,
            type_counts=json.dumps(type_counts, ensure_ascii=False, sort_keys=True),
        )
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {"role": "user", "content": prompt},
        ]
        self._report_progress(
            stage="semantic_type_normalization",
            message=f"正在归并 {len(labels)} 个实体类型",
        )
        if self._guard.open_reason:
            return None
        try:
            attempts = 0
            estimated_tokens = max(
                1,
                int(
                    sum(len(str(message.get("content") or "")) for message in messages)
                    / settings.semantic_graph_estimated_chars_per_token
                ),
            )

            def operation() -> ModelCallResult:
                nonlocal attempts
                attempts += 1
                self._guard.before_call(estimated_tokens)
                started_at = time.perf_counter()
                try:
                    raw = self.chat_fn(
                        self.runtime,
                        messages,
                        temperature=0.0,
                        timeout=self.request_timeout_seconds,
                        response_format=(
                            {"type": "json_object"}
                            if settings.semantic_graph_json_output
                            else None
                        ),
                        max_output_tokens=self.max_output_tokens,
                        # 类型归并同样是短 JSON 任务，不需要 DeepSeek 的长链推理。
                        thinking=False,
                    )
                    result = (
                        raw
                        if isinstance(raw, ModelCallResult)
                        else ModelCallResult(str(raw), ModelUsage())
                    )
                    self._record_model_metric(
                        None,
                        None,
                        attempts,
                        "success",
                        stage="semantic_type_normalization",
                        result=result,
                        elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    )
                    return result
                except Exception as error:
                    self._record_model_metric(
                        None,
                        None,
                        attempts,
                        "failed",
                        stage="semantic_type_normalization",
                        error=error,
                        elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    )
                    raise

            raw_answer = call_with_retry(
                operation,
                max_attempts=settings.domain_tree_retry_attempts,
                base_delay_seconds=settings.domain_tree_retry_base_delay_seconds,
                cancel_event=self.cancel_event,
            )
            answer = (
                raw_answer.content
                if isinstance(raw_answer, ModelCallResult)
                else str(raw_answer)
            )
            payload = self._extract_json_object(answer)
            mapping = self._validate_type_mapping(payload, labels)
            if mapping is None:
                logger.warning("模型返回的实体类型映射未通过完整性校验，使用确定性归一化")
                return None
            self._save_cached_payload(cache_key, payload)
            return mapping
        except DomainTreeGenerationCancelled:
            raise
        except Exception as error:
            logger.warning("实体类型动态归并失败，使用确定性归一化：%s", error)
            return None

    def _validate_type_mapping(
        self,
        payload: dict[str, Any],
        labels: list[str],
    ) -> dict[str, str] | None:
        """只接受完整覆盖输入且 canonical 符合目标语言的类型分组。"""
        groups = payload.get("groups") if isinstance(payload.get("groups"), list) else []
        allowed = set(labels)
        mapping: dict[str, str] = {}
        for group in groups:
            if not isinstance(group, dict):
                return None
            canonical = self._normalize_type_label(str(group.get("canonical") or ""))
            members = group.get("members") if isinstance(group.get("members"), list) else []
            normalized_members = [self._normalize_type_label(str(member or "")) for member in members]
            if not self._is_valid_entity_type_language(canonical):
                return None
            target_language_members = [
                member for member in normalized_members
                if self._is_valid_entity_type_language(member)
            ]
            if target_language_members and canonical not in target_language_members:
                return None
            for member in normalized_members:
                if member not in allowed or member in mapping:
                    return None
                mapping[member] = canonical
        return mapping if set(mapping) == allowed else None

    def _type_mapping_cache_key(self, type_counts: dict[str, int]) -> str:
        """按模型与实际类型集合缓存动态归并结果。"""
        payload = {
            "schema": _TYPE_MAPPING_SCHEMA_VERSION,
            "kind": "entity_type_mapping",
            "provider": self.runtime.get("provider", ""),
            "protocol": self.runtime.get("protocol", ""),
            "model": self.runtime.get("model", ""),
            "base_url": self.runtime.get("base_url", ""),
            "entityTypeLanguage": self.entity_type_language,
            "types": type_counts,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _normalize_entity_type_language(self, value: str) -> str:
        """把外部语言配置收敛为语义抽取器支持的两种类型语言。"""
        normalized = str(value or "English").strip().casefold()
        if normalized in {"zh", "zh-cn", "cn", "中文", "chinese"}:
            return "中文"
        return "English"

    def _default_entity_type(self) -> str:
        """返回目标语言下的安全通用类型。"""
        return "实体" if self.entity_type_language == "中文" else "entity"

    def _is_valid_entity_type_language(self, value: str) -> bool:
        """校验推断类型是否满足当前目标语言契约。"""
        label = str(value or "").strip()
        if not label:
            return False
        contains_chinese = bool(_CHINESE_TEXT_PATTERN.search(label))
        if self.entity_type_language == "中文":
            return contains_chinese
        return label.isascii() and bool(re.search(r"[A-Za-z]", label))

    def _invalid_entity_types(self, payload: dict[str, Any]) -> list[str]:
        """返回模型结果中违反目标语言契约的实体类型。"""
        entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
        invalid = {
            str(entity.get("type") or "").strip()
            for entity in entities
            if isinstance(entity, dict)
            and not self._is_valid_entity_type_language(str(entity.get("type") or ""))
        }
        return sorted(invalid, key=str.casefold)

    def _replace_invalid_entity_types(self, payload: dict[str, Any]) -> None:
        """保留实体与证据，仅把违规类型降级为目标语言下的通用类型。"""
        entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
        fallback = self._default_entity_type()
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            if not self._is_valid_entity_type_language(str(entity.get("type") or "")):
                entity["_invalidEntityType"] = str(entity.get("type") or "").strip()
                entity["type"] = fallback

    def _normalize_type_label(self, value: str) -> str:
        """以语言无关的形式统一类型标签表面差异。"""
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        normalized = re.sub(r"\s+", " ", normalized).strip()
        normalized = normalized.strip(".,;:!?-_–—/\\|()[]{}<>《》【】（）")
        return normalized.casefold() or "entity"

    def _add_evidence(
        self,
        state: dict[str, Any],
        document: SemanticSourceDocument,
        chunk: TextChunk,
        quote: str,
        *,
        kind: str,
    ) -> str:
        """仅接受能够在当前原文分块中定位到的逐字证据。"""
        normalized_quote = re.sub(r"\s+", " ", quote).strip()
        if not normalized_quote:
            return ""
        position = chunk.text.find(quote)
        if position < 0:
            compact_text = re.sub(r"\s+", " ", chunk.text)
            position = compact_text.find(normalized_quote)
        if position < 0:
            logger.debug("[%s] 丢弃无法回定位的模型证据：%s", document.record_id, normalized_quote[:80])
            return ""
        line_start = chunk.start_line + chunk.text[:position].count("\n")
        evidence_id = self._stable_id("evidence", document.record_id, chunk.section, normalized_quote)
        state["evidence"].setdefault(
            evidence_id,
            {
                "id": evidence_id,
                "documentId": document.record_id,
                "section": chunk.section,
                "chunkIndex": chunk.index,
                "lineStart": line_start,
                "quote": normalized_quote,
                "kind": kind,
                "language": self._detect_text_language(normalized_quote),
                "documentLanguage": state.get("documentLanguages", {}).get(
                    document.record_id,
                    "unknown",
                ),
            },
        )
        evidence = state["evidence"][evidence_id]
        evidence["languageMismatch"] = (
            evidence.get("language") not in {"unknown", evidence.get("documentLanguage")}
        )
        return evidence_id

    def _parse_reference_entries(self, reference_text: str) -> list[tuple[int, str, int]]:
        """把编号参考文献区解析为编号、原文和相对行号。"""
        matches = list(_REFERENCE_ENTRY_PATTERN.finditer(reference_text))
        entries: list[tuple[int, str, int]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(reference_text)
            number = int(match.group(1) or match.group(2))
            raw = re.sub(r"\s+", " ", reference_text[match.end() : end]).strip()
            if raw:
                entries.append((number, raw, reference_text[: match.start()].count("\n") + 1))
        if entries:
            return entries

        # APA 等作者—年份格式常不带编号，此时按段落顺序生成稳定的内部编号。
        cursor = 0
        for index, paragraph in enumerate(re.split(r"\n\s*\n", reference_text), start=1):
            raw = re.sub(r"\s+", " ", paragraph).strip()
            if not raw:
                continue
            position = reference_text.find(paragraph, cursor)
            cursor = max(cursor, position + len(paragraph))
            entries.append((index, raw, reference_text[: max(position, 0)].count("\n") + 1))
        return entries

    def _find_inline_citation_contexts(self, body: str) -> dict[int, list[dict[str, Any]]]:
        """查找正文数字引用标记，并保存短上下文、章节和行号。"""
        contexts: dict[int, list[dict[str, Any]]] = {}
        headings = list(_HEADING_PATTERN.finditer(body))
        for match in _INLINE_CITATION_PATTERN.finditer(body):
            start = max(0, match.start() - 180)
            end = min(len(body), match.end() + 180)
            sentence = re.sub(r"\s+", " ", body[start:end]).strip()
            section = "正文"
            for heading in headings:
                if heading.start() > match.start():
                    break
                section = heading.group(2).strip()
            context = {
                "section": section,
                "lineStart": body[: match.start()].count("\n") + 1,
                "quote": sentence,
            }
            for number in self._expand_citation_numbers(match.group(1)):
                contexts.setdefault(number, []).append(context)
        return contexts

    def _find_author_year_contexts(
        self,
        body: str,
        author_keys: list[str],
        year: str,
    ) -> list[dict[str, Any]]:
        """为作者—年份制参考文献查找正文引用上下文。"""
        if not year or not author_keys:
            return []
        surnames = [value for value in author_keys if len(value) >= 3][:3]
        if not surnames:
            return []
        surname_pattern = "|".join(re.escape(value) for value in surnames)
        pattern = re.compile(
            rf"(?i)\b(?:{surname_pattern})\b"
            rf"(?:\s+et\s+al\.?)?\s*[,;(]?\s*\b{re.escape(year)}\b"
        )
        headings = list(_HEADING_PATTERN.finditer(body))
        contexts: list[dict[str, Any]] = []
        for match in pattern.finditer(body):
            start = max(0, match.start() - 160)
            end = min(len(body), match.end() + 160)
            section = "正文"
            for heading in headings:
                if heading.start() > match.start():
                    break
                section = heading.group(2).strip()
            contexts.append(
                {
                    "section": section,
                    "lineStart": body[: match.start()].count("\n") + 1,
                    "quote": re.sub(r"\s+", " ", body[start:end]).strip(),
                }
            )
        return contexts[:8]

    def _expand_citation_numbers(self, value: str) -> list[int]:
        """展开 `[1,3-5]` 一类引用编号表达式。"""
        numbers: set[int] = set()
        for part in re.split(r"[,;]", value):
            bounds = re.split(r"[-–]", part.strip())
            try:
                if len(bounds) == 2:
                    start, end = int(bounds[0]), int(bounds[1])
                    if 0 <= end - start <= 50:
                        numbers.update(range(start, end + 1))
                elif bounds and bounds[0]:
                    numbers.add(int(bounds[0]))
            except ValueError:
                continue
        return sorted(numbers)

    def _parse_reference_metadata(self, raw_reference: str) -> dict[str, Any]:
        """按常见 CSL 输出结构解析作者、年份和标题；只在无结构化元数据时使用。"""
        compact = re.sub(r"\s+", " ", raw_reference).strip()
        year_match = _YEAR_PATTERN.search(compact)
        year = int(year_match.group(0)) if year_match else None
        author_text = compact[: year_match.start()].strip(" .,:;()") if year_match else ""
        remainder = compact[year_match.end() :].strip(" .,:;()") if year_match else compact

        # Springer/BibTeX 风格常以“作者: 标题”分隔，冒号比年份更可靠。
        colon_index = compact.find(":")
        if 0 < colon_index < 160 and not compact[:colon_index].lower().endswith(("http", "https", "doi")):
            author_text = compact[:colon_index].strip(" .,:;()")
            remainder = compact[colon_index + 1 :].strip()
            embedded_year = _YEAR_PATTERN.search(remainder)
            if embedded_year and year is None:
                year = int(embedded_year.group(0))

        authors = self._parse_author_names(author_text)
        author_keys = self._author_keys(authors)
        title = self._extract_title_candidate(remainder)
        return {
            "authors": authors,
            "authorKeys": author_keys,
            "year": year,
            "title": title,
        }

    def _guess_reference_title(self, raw_reference: str) -> str:
        """兼容旧调用方，返回通过质量校验的标题。"""
        parsed = self._parse_reference_metadata(raw_reference)
        title = str(parsed.get("title") or "")
        return title if self._is_valid_reference_title(
            title,
            raw_reference,
            list(parsed.get("authors") or []),
            parsed.get("year"),
        ) else ""

    def _extract_title_candidate(self, value: str) -> str:
        """从去除作者与年份后的 CSL 文本中保守提取标题。"""
        cleaned = re.sub(r"^\(?\d{4}[a-z]?\)?[.,;:]?\s*", "", value, flags=re.IGNORECASE)
        cleaned = re.sub(_DOI_PATTERN, "", cleaned)
        cleaned = re.sub(_URL_PATTERN, "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;()")
        if not cleaned:
            return ""
        title = re.split(
            r"\.\s+(?=(?:In\b|Proceedings\b|Proc\.\b|arXiv\b|"
            r"(?:International|Annual|IEEE|ACM|Springer|Elsevier)\b))",
            cleaned,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        # 普通期刊信息通常从标题后的首个句号开始；缩写后的短片段不据此切分。
        sentence = re.match(r"^(.{4,240}?[A-Za-z0-9)])\.\s+[A-Z]", title)
        if sentence:
            title = sentence.group(1)
        return re.sub(r"\s+", " ", title).strip(" .,:;()")

    def _parse_author_names(self, value: str) -> list[str]:
        """解析作者串并排除卷号、年份和单字符噪声。"""
        cleaned = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", value).strip(" .,:;")
        if not cleaned:
            return []
        parts = re.split(r"\s+(?:and|&)\s+|;\s*|,\s*(?=[A-Z][a-z]+\s+[A-Z])", cleaned)
        authors = []
        for part in parts:
            candidate = re.sub(r"\s+", " ", part).strip(" .,:;")
            letters = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ\u3400-\u9fff]", "", candidate)
            if len(letters) >= 2 and not candidate.isdigit():
                authors.append(candidate)
        return authors[:20]

    def _author_keys(self, authors: list[str]) -> list[str]:
        """提取可用于作者—年份上下文匹配的稳定姓氏。"""
        keys: list[str] = []
        for author in authors:
            tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+", author)
            if not tokens:
                continue
            key = tokens[0] if "," in author else tokens[-1]
            if len(key) >= 3 and key.casefold() not in {"and", "the", "with"}:
                keys.append(key)
        return list(dict.fromkeys(keys))

    def _is_valid_reference_title(
        self,
        title: str,
        raw_reference: str,
        authors: list[str],
        year: int | None,
    ) -> bool:
        """拒绝单字符、纯数字、作者年份串和整条参考文献被误当标题。"""
        compact = re.sub(r"\s+", " ", str(title or "")).strip(" .,:;")
        raw = re.sub(r"\s+", " ", raw_reference).strip()
        if len(compact) < 4 or len(compact) > 300 or compact.isdigit():
            return False
        if self._normalize_name(compact) == self._normalize_name(raw):
            return False
        if len(raw) > 80 and len(compact) / len(raw) > 0.88:
            return False
        normalized = self._normalize_name(compact)
        if year and normalized == str(year):
            return False
        return not any(normalized == self._normalize_name(author) for author in authors)

    @staticmethod
    def _coerce_year(value: Any) -> int | None:
        match = _YEAR_PATTERN.search(str(value or ""))
        return int(match.group(0)) if match else None

    @staticmethod
    def _normalize_doi(value: Any) -> str:
        """统一 DOI URL、doi: 前缀和大小写，供本地元数据精确匹配。"""
        normalized = str(value or "").strip().casefold()
        normalized = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", normalized)
        return normalized.rstrip(".,;")

    @staticmethod
    def _detect_text_language(value: str) -> str:
        """按 Unicode 文字脚本检测语言，不依赖领域词典。"""
        text = str(value or "")
        han_count = len(re.findall(r"[\u3400-\u9fff]", text))
        latin_count = len(re.findall(r"[A-Za-z]", text))
        total = han_count + latin_count
        if total == 0:
            return "unknown"
        if han_count >= 3 and latin_count >= 3 and min(han_count, latin_count) / total >= 0.1:
            return "mixed"
        return "zh" if han_count > latin_count else "en"

    def bind_relation_citations(
        self,
        relations: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> None:
        """建立关系证据到正文引用上下文再到参考文献的可验证链路。"""
        evidence_by_id = {str(item.get("id") or ""): item for item in evidence}
        citations_by_document: dict[str, list[dict[str, Any]]] = {}
        citations_by_number: dict[tuple[str, int], dict[str, Any]] = {}
        citations_by_year: dict[tuple[str, int], list[dict[str, Any]]] = {}
        contexts_by_line: dict[tuple[str, int], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for citation in citations:
            self._upgrade_legacy_citation(citation)
            document_id = str(citation.get("documentId") or "")
            citations_by_document.setdefault(document_id, []).append(citation)
            citations_by_number[(document_id, int(citation.get("referenceNumber") or 0))] = citation
            citation_year = self._coerce_year(citation.get("year"))
            if citation_year:
                citations_by_year.setdefault((document_id, citation_year), []).append(citation)
            for context in citation.get("contexts") or []:
                line_start = int(context.get("lineStart") or 0)
                if line_start:
                    contexts_by_line.setdefault((document_id, line_start), []).append(
                        (citation, context)
                    )

        for relation in relations:
            linked: set[str] = set()
            relation_evidence = [
                evidence_by_id[evidence_id]
                for evidence_id in relation.get("evidenceIds") or []
                if evidence_id in evidence_by_id
            ]
            for item in relation_evidence:
                document_id = str(item.get("documentId") or "")
                quote = re.sub(r"\s+", " ", str(item.get("quote") or "")).strip()
                if not quote:
                    continue
                explicit_numbers = {
                    number
                    for match in _INLINE_CITATION_PATTERN.finditer(quote)
                    for number in self._expand_citation_numbers(match.group(1))
                }
                for number in explicit_numbers:
                    citation = citations_by_number.get((document_id, number))
                    if citation and citation.get("id"):
                        linked.add(str(citation["id"]))

                evidence_line = int(item.get("lineStart") or 0)
                nearby_contexts = [
                    pair
                    for line_number in range(max(1, evidence_line - 4), evidence_line + 5)
                    for pair in contexts_by_line.get((document_id, line_number), [])
                ] if evidence_line else [
                    (citation, context)
                    for citation in citations_by_document.get(document_id, [])
                    for context in citation.get("contexts") or []
                ]
                for citation, context in nearby_contexts:
                    citation_id = str(citation.get("id") or "")
                    if citation_id and self._evidence_matches_citation_context(item, context):
                        linked.add(citation_id)
                quote_years = {int(value) for value in _YEAR_PATTERN.findall(quote)}
                for quote_year in quote_years:
                    for citation in citations_by_year.get((document_id, quote_year), []):
                        citation_id = str(citation.get("id") or "")
                        if citation_id and self._quote_contains_author_year(
                            quote,
                            list(citation.get("authorKeys") or []),
                            quote_year,
                        ):
                            linked.add(citation_id)
            relation["citationIds"] = sorted(linked)

    def annotate_evidence_languages(self, evidence: list[dict[str, Any]]) -> None:
        """为历史证据按文档整体脚本分布补齐语言及异常语言标记。"""
        quotes_by_document: dict[str, list[str]] = {}
        for item in evidence:
            document_id = str(item.get("documentId") or "")
            quote = str(item.get("quote") or "")
            if document_id and quote:
                quotes_by_document.setdefault(document_id, []).append(quote)
        document_languages: dict[str, str] = {}
        for document_id, quotes in quotes_by_document.items():
            language_counts = Counter(
                language
                for language in (self._detect_text_language(quote) for quote in quotes)
                if language in {"zh", "en"}
            )
            document_languages[document_id] = (
                language_counts.most_common(1)[0][0]
                if language_counts
                else self._detect_text_language(" ".join(quotes))
            )
        for item in evidence:
            language = str(item.get("language") or "") or self._detect_text_language(
                str(item.get("quote") or "")
            )
            document_language = str(item.get("documentLanguage") or "") or document_languages.get(
                str(item.get("documentId") or ""),
                "unknown",
            )
            item["language"] = language
            item["documentLanguage"] = document_language
            item["languageMismatch"] = language not in {"unknown", document_language}

    def _upgrade_legacy_citation(self, citation: dict[str, Any]) -> None:
        """在读取历史图谱时补齐新版结构化引用字段，不修改原始参考文献。"""
        if citation.get("metadataQuality"):
            return
        raw_reference = str(citation.get("rawReference") or "")
        if not raw_reference:
            return
        parsed = self._parse_reference_metadata(raw_reference)
        authors = list(parsed.get("authors") or [])
        year = self._coerce_year(citation.get("year")) or parsed.get("year")
        title = str(parsed.get("title") or "")
        title_valid = self._is_valid_reference_title(title, raw_reference, authors, year)
        citation["title"] = title if title_valid else ""
        citation["authors"] = authors
        citation["authorKeys"] = list(parsed.get("authorKeys") or [])
        citation["year"] = year
        citation["metadataSource"] = (
            "doi" if self._normalize_doi(citation.get("doi")) else "text"
        )
        citation["metadataQuality"] = "valid" if title_valid else "invalid_title"

    @staticmethod
    def _evidence_matches_citation_context(
        evidence: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        """用行号邻近和词项覆盖确认引用上下文确实包含关系证据。"""
        evidence_line = int(evidence.get("lineStart") or 0)
        context_line = int(context.get("lineStart") or 0)
        if evidence_line and context_line and abs(evidence_line - context_line) > 4:
            return False
        evidence_tokens = re.findall(
            r"[\w\u3400-\u9fff]+",
            str(evidence.get("quote") or "").casefold(),
        )
        context_tokens = set(
            re.findall(
                r"[\w\u3400-\u9fff]+",
                str(context.get("quote") or "").casefold(),
            )
        )
        if len(evidence_tokens) < 6:
            return False
        covered = sum(token in context_tokens for token in evidence_tokens)
        return covered / len(evidence_tokens) >= 0.72

    @staticmethod
    def _quote_contains_author_year(quote: str, author_keys: list[str], year: Any) -> bool:
        if not author_keys or not year:
            return False
        for surname in author_keys[:3]:
            if re.search(
                rf"(?i)\b{re.escape(surname)}\b(?:\s+et\s+al\.?)?\s*[,;(]?\s*\b{year}\b",
                quote,
            ):
                return True
        return False

    def _match_local_document(
        self,
        raw_reference: str,
        title: str,
        local_titles: dict[str, str],
    ) -> str:
        """根据规范化标题把参考文献链接到本地文献节点。"""
        normalized_reference = self._normalize_name(raw_reference)
        normalized_title = self._normalize_name(title)
        for candidate, record_id in local_titles.items():
            if len(candidate) >= 12 and (candidate in normalized_reference or candidate == normalized_title):
                return record_id
        return ""

    def _read_markdown(self, document: SemanticSourceDocument) -> str:
        """安全读取 UTF-8 Markdown 全文。"""
        path = document.markdown_path
        if not path or not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError as error:
            logger.warning("[%s] 读取 Markdown 全文失败：%s", document.record_id, error)
            return ""

    def _extract_json_object(self, answer: str) -> dict[str, Any]:
        """从模型回答中提取第一个完整 JSON 对象。"""
        cleaned = str(answer or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        candidates = [cleaned]
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            candidates.insert(0, match.group(0))
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise ValueError("模型没有返回有效的语义抽取 JSON")

    def _resolve_relation_entity(self, value: Any, mapping: dict[str, str]) -> str:
        """把关系端点的局部 ID 或名称解析为全局实体 ID。"""
        raw = str(value or "").strip()
        return mapping.get(raw) or mapping.get(self._normalize_name(raw), "")

    def _normalize_attributes(self, value: Any, evidence_id: str) -> list[dict[str, Any]]:
        """校验实体属性并附加对应证据 ID。"""
        raw_items = value if isinstance(value, list) else []
        attributes: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            attribute_value = str(item.get("value") or "").strip()
            if name and attribute_value:
                attributes.append(
                    {
                        "name": name,
                        "value": attribute_value,
                        "unit": str(item.get("unit") or "").strip(),
                        "evidenceId": evidence_id,
                    }
                )
        return attributes

    def _merge_attributes(
        self,
        current: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按属性名、值和单位合并重复属性。"""
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in current + incoming:
            key = (str(item.get("name")), str(item.get("value")), str(item.get("unit")))
            merged.setdefault(key, item)
        return list(merged.values())

    def _string_list(self, value: Any) -> list[str]:
        """把模型返回值清洗为非空字符串列表。"""
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _clamp_confidence(self, value: Any) -> float:
        """把置信度限制到 0 到 1。"""
        try:
            return round(max(0.0, min(float(value), 1.0)), 4)
        except (TypeError, ValueError):
            return 0.5

    def _entity_id(self, canonical_name: str) -> str:
        """根据规范名称生成跨文档稳定实体 ID。"""
        return self._stable_id("entity", self._normalize_name(canonical_name))

    def _stable_id(self, prefix: str, *parts: str) -> str:
        """使用内容摘要生成长度适中的稳定 ID。"""
        content = "\x1f".join(str(part) for part in parts)
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:{digest}"

    def _normalize_name(self, value: str) -> str:
        """统一实体与标题名称，便于去重和匹配。"""
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


__all__ = ["SemanticGraphExtractor", "SemanticSourceDocument", "TextChunk"]
