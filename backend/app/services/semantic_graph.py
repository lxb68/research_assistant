"""从 Markdown 全文抽取实体、语义关系、原文证据和文献引用。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from app.core.config import settings
from app.services.model_client import chat_completion
from app.services.task_control import (
    DomainTreeGenerationCancelled,
    call_with_retry,
    raise_if_cancelled,
)


logger = logging.getLogger(__name__)

_MODEL_OUTPUT_PREVIEW_CHARS = 2000
_CACHE_SCHEMA_VERSION = "semantic-graph-v2-source-language"


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


@dataclass(slots=True)
class TextChunk:
    """保存一个带章节和行号定位的正文分块。"""

    index: int
    section: str
    text: str
    start_line: int


class SemanticGraphExtractor:
    """调用当前模型抽取全文语义，并用确定性规则解析参考文献。"""

    def __init__(
        self,
        runtime: dict[str, str] | None,
        *,
        chat_fn: Callable[..., str] = chat_completion,
        chunk_size: int = 6000,
        chunk_overlap: int = 400,
        cache_dir: str | Path | None = None,
        max_workers: int = 4,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """初始化模型配置、调用函数与分块参数。"""
        self.runtime = dict(runtime or {})
        self.chat_fn = chat_fn
        self.chunk_size = max(1200, chunk_size)
        self.chunk_overlap = max(0, min(chunk_overlap, self.chunk_size // 3))
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self.max_workers = max(1, min(int(max_workers), 16))
        self.cancel_event = cancel_event
        self.progress_callback = progress_callback

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
        state: dict[str, Any] = {
            "entities": {},
            "entityAliases": {},
            "relations": {},
            "evidence": {},
            "citations": [],
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
        uncached_items: list[tuple[int, SemanticSourceDocument, TextChunk, str]] = []
        for order, document, chunk in work_items:
            raise_if_cancelled(self.cancel_event)
            cache_key = self._chunk_cache_key(document, chunk)
            payload = self._load_cached_payload(cache_key)
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
            )

        if uncached_items:
            logger.info(
                "语义分块并发抽取开始：cache_hits=%s cache_misses=%s max_workers=%s",
                cache_hit_count,
                cache_miss_count,
                self.max_workers,
            )
            executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="semantic-graph",
            )
            futures: dict[
                Future[dict[str, Any] | None],
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
                    payload = future.result()
                    results[order] = payload
                    completed_chunks += 1
                    if payload is None:
                        state["failedChunkCount"] += 1
                    else:
                        state["processedChunkCount"] += 1
                        self._save_cached_payload(cache_key, payload)
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

        entities = sorted(state["entities"].values(), key=lambda item: (item["type"], item["name"].lower()))
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
            },
        }

    def _report_progress(self, **update: Any) -> None:
        """把进度增量上报给任务管理器。"""
        if self.progress_callback:
            self.progress_callback(update)

    def _chunk_cache_key(self, document: SemanticSourceDocument, chunk: TextChunk) -> str:
        """根据模型、提示词版本和原文内容生成稳定的语义分块缓存键。"""
        payload = {
            "schema": _CACHE_SCHEMA_VERSION,
            "provider": self.runtime.get("provider", ""),
            "protocol": self.runtime.get("protocol", ""),
            "model": self.runtime.get("model", ""),
            "base_url": self.runtime.get("base_url", ""),
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
    ) -> list[dict[str, Any]]:
        """解析参考文献条目、正文引用标记以及本地文献链接。"""
        if not reference_text:
            return []
        entries = self._parse_reference_entries(reference_text)
        contexts = self._find_inline_citation_contexts(body)
        citations: list[dict[str, Any]] = []

        for number, raw_reference, entry_line in entries:
            doi_match = _DOI_PATTERN.search(raw_reference)
            url_match = _URL_PATTERN.search(raw_reference)
            year_match = _YEAR_PATTERN.search(raw_reference)
            title = self._guess_reference_title(raw_reference)
            matched_document_id = self._match_local_document(raw_reference, title, local_titles)
            citation_id = f"citation:{document.record_id}:{number}"
            citation_contexts = contexts.get(number, []) + self._find_author_year_contexts(
                body,
                raw_reference,
                year_match.group(0) if year_match else "",
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
                    "year": int(year_match.group(0)) if year_match else None,
                    "doi": doi_match.group(0).rstrip(".,;") if doi_match else "",
                    "url": url_match.group(0).rstrip(".,;") if url_match else "",
                    "matchedDocumentId": matched_document_id,
                    "referenceLine": reference_start_line + entry_line,
                    "contexts": list(unique_contexts.values())[:8],
                }
            )
        return citations

    def _extract_chunk(
        self,
        document: SemanticSourceDocument,
        chunk: TextChunk,
    ) -> dict[str, Any] | None:
        """调用模型抽取单个正文分块的实体、属性和关系。"""
        if not self.runtime:
            logger.warning("[%s] 未配置模型，跳过第 %s 个语义分块", document.record_id, chunk.index)
            return None
        prompt = self._build_extraction_prompt(document, chunk)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是科研文献语义抽取器。只能依据给定原文抽取，不得补充常识或猜测。"
                    "必须返回合法 JSON，不要使用 Markdown 代码块。"
                    "实体名称、实体类型、别名、属性、关系谓词和证据必须保留原文语言，禁止翻译。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
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
            answer = call_with_retry(
                lambda: self.chat_fn(
                    self.runtime,
                    messages,
                    temperature=0.0,
                    timeout=settings.request_timeout,
                ),
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
            payload = self._extract_json_object(answer)
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
            return payload
        except DomainTreeGenerationCancelled:
            raise
        except Exception as error:
            logger.warning(
                "[%s] 第 %s 个语义分块抽取失败：elapsed_ms=%.1f error=%s",
                document.record_id,
                chunk.index,
                (time.perf_counter() - started_at) * 1000,
                error,
            )
            return None

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
        """构造严格、可被多种模型理解的 JSON 抽取提示词。"""
        return f"""请从以下科研文献原文抽取实体、实体属性和实体间关系。

文献ID：{document.record_id}
文献标题：{document.title}
章节：{chunk.section}

要求：
1. 实体只保留具有研究意义的对象，例如方法、模型、算法、材料、疾病、药物、基因、数据集、指标、机构、人物、任务和理论。
2. canonicalName 使用原文中最完整、最规范的名称；aliases 只填写原文明确出现的别名或缩写。
3. 属性必须是原文明示的参数、数值、单位、时间、性能或类别。
4. relationType 只能是 general、causal、comparison、experimental、property 之一。
5. causal 只用于原文明示的导致、促进、抑制、影响等因果关系，不能把相关性写成因果。
6. evidenceQuote 必须逐字复制本段中的短句，禁止改写；无法找到直接证据时不要输出该项。
7. confidence 使用 0 到 1 之间的数字。
8. 关系的 source 和 target 使用 entities 中的 localId。
9. name、canonicalName、type、aliases、属性名称与值必须使用原文语言和原文术语，禁止翻译。
10. predicate 必须使用原文语言描述，禁止把英文关系翻译为中文或把中文关系翻译为英文。
11. evidenceQuote 必须保持原文语言并逐字引用，禁止翻译、改写或概括。

严格返回以下结构：
{{
  "entities": [
    {{
      "localId": "e1",
      "name": "原文名称",
      "canonicalName": "规范名称",
      "type": "实体类型",
      "aliases": ["别名"],
      "attributes": [{{"name": "属性名", "value": "属性值", "unit": "单位"}}],
      "evidenceQuote": "原文短句"
    }}
  ],
  "relations": [
    {{
      "source": "e1",
      "target": "e2",
      "predicate": "关系名称",
      "relationType": "general",
      "confidence": 0.9,
      "evidenceQuote": "原文短句"
    }}
  ]
}}

原文：
{chunk.text}
"""

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
            existing = state["entities"].get(entity_id)
            if existing:
                existing["aliases"] = sorted(set(existing["aliases"] + aliases + [mention]) - {existing["name"]})
                existing["attributes"] = self._merge_attributes(existing["attributes"], attributes)
                existing["evidenceIds"] = sorted(set(existing["evidenceIds"] + ([evidence_id] if evidence_id else [])))
                existing["documentIds"] = sorted(set(existing["documentIds"] + [document.record_id]))
                continue
            state["entities"][entity_id] = {
                "id": entity_id,
                "name": name,
                "type": str(raw_entity.get("type") or "entity").strip() or "entity",
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
            },
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
        raw_reference: str,
        year: str,
    ) -> list[dict[str, Any]]:
        """为作者—年份制参考文献查找正文引用上下文。"""
        if not year:
            return []
        author_prefix = raw_reference.split(":", 1)[0].split(",", 1)[0].strip()
        surname_matches = re.findall(r"[A-Za-z][A-Za-z'’\-]+", author_prefix)
        if not surname_matches:
            return []
        surname = surname_matches[-1]
        pattern = re.compile(
            rf"(?i)\b{re.escape(surname)}\b[^\n]{{0,100}}?\b{re.escape(year)}\b"
            rf"|\b{re.escape(year)}\b[^\n]{{0,100}}?\b{re.escape(surname)}\b"
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

    def _guess_reference_title(self, raw_reference: str) -> str:
        """从常见参考文献格式中保守推断标题，失败时保留截断原文。"""
        after_authors = raw_reference.split(":", 1)[1].strip() if ":" in raw_reference else raw_reference
        title = re.split(r"\.\s+(?:In:|arXiv|https?://|[A-Z][A-Za-z. ]+\s+\d)", after_authors, maxsplit=1)[0]
        title = re.sub(r"\s+", " ", title).strip(" .")
        return title[:300] or raw_reference[:300]

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
