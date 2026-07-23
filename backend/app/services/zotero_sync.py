"""把 Zotero 题录及其本地 PDF 增量同步到项目知识库。"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable

from app.agents import HunterAgent
from app.core.config import settings
from app.services.project_repository import ProjectRepository
from app.services.document_parse_repository import DocumentParseRepository
from app.services.mineru_batch import (
    MinerUBatchCoordinator,
    MinerUBatchInput,
    MinerUCloudBatchClient,
    build_parse_key,
)
from app.services.task_control import TaskCancelled, raise_if_task_cancelled
from app.services.zotero_connector import ZoteroConnector
from app.services.zotero_project_router import ZoteroProjectRouter
from app.services.zotero_source_repository import ZoteroSourceRepository


ProgressCallback = Callable[[int, str, str], None]
LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class ZoteroParseCandidate:
    """Zotero 发现阶段产生的稳定解析输入。"""

    source: dict[str, Any]
    item_key: str
    item_version: int
    attachment_key: str
    attachment_version: int
    pdf_path: Path
    file_hash: str
    paper_id: str
    metadata: dict[str, Any]


class ZoteroSyncService:
    """协调 Zotero 读取、内容变更判断、全文索引和项目成员更新。"""

    def __init__(
        self,
        *,
        metadata_db_path: str | Path | None = None,
        log_callback: LogCallback | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.metadata_db_path = Path(metadata_db_path or settings.hunter_metadata_db)
        self.sources = ZoteroSourceRepository(self.metadata_db_path)
        self.projects = ProjectRepository(self.metadata_db_path)
        self.agent = HunterAgent(metadata_db_path=self.metadata_db_path, log_callback=log_callback)
        self.log_callback = log_callback
        self.progress_callback = progress_callback

    def sync(self, source_id: str, *, cancel_event=None) -> dict[str, Any]:
        source = self.sources.require(source_id)
        self.projects.require(source["projectId"])
        connector = ZoteroConnector(
            base_url=source["apiBaseUrl"],
            library_type=source["libraryType"],
            library_id=source["libraryId"],
        )
        self.sources.set_source_status(source_id, status="syncing")
        try:
            connector.test_connection()
            source, _target_project, migrated = ZoteroProjectRouter(
                self.metadata_db_path,
            ).ensure_routed(connector, source)
            if migrated:
                self._log(f"Zotero 数据源已迁移到同名项目：project_id={source['projectId']}")
            items = self._load_source_items(connector, source)
            total = len(items)
            stats = {
                "discovered": total,
                "indexed": 0,
                "metadataUpdated": 0,
                "unchanged": 0,
                "unavailable": 0,
                "failed": 0,
                "missing": 0,
            }
            seen_keys: list[str] = []
            candidates: list[ZoteroParseCandidate] = []
            for index, item in enumerate(items, start=1):
                raise_if_task_cancelled(cancel_event)
                item_key = self._item_key(item)
                if not item_key:
                    continue
                seen_keys.append(item_key)
                title = str(self._item_data(item).get("title") or item_key)
                self._progress(
                    5 + int(index * 88 / max(1, total)),
                    "syncing",
                    f"正在同步 Zotero 文献 {index}/{total}：{title[:80]}",
                )
                try:
                    outcome = self._sync_item(
                        connector,
                        source,
                        item,
                        cancel_event=cancel_event,
                    )
                    if isinstance(outcome, ZoteroParseCandidate):
                        candidates.append(outcome)
                    else:
                        stats[outcome] += 1
                except TaskCancelled:
                    raise
                except Exception as error:
                    stats["failed"] += 1
                    previous = self.sources.get_item(source_id, item_key) or {}
                    self.sources.upsert_item(
                        source_id,
                        item_key,
                        item_version=self._item_version(item),
                        attachment_key=previous.get("attachment_key"),
                        attachment_version=previous.get("attachment_version"),
                        file_path=previous.get("file_path"),
                        file_hash=previous.get("file_hash"),
                        paper_id=previous.get("paper_id"),
                        status="failed",
                        error_message=str(error),
                    )
                    self._log(f"Zotero 文献同步失败：key={item_key}, error={error}")
            if candidates:
                self._process_candidates(
                    candidates,
                    stats,
                    cancel_event=cancel_event,
                )
            stats["missing"] = self.sources.mark_missing_except(source_id, seen_keys)
            self.sources.set_source_status(source_id, status="ready", synced=True)
            self._progress(96, "saving", "Zotero 同步完成，正在保存结果")
            return {"sourceId": source_id, "projectId": source["projectId"], **stats}
        except TaskCancelled:
            self.sources.set_source_status(source_id, status="ready", error="同步已取消")
            raise
        except Exception as error:
            self.sources.set_source_status(source_id, status="failed", error=str(error))
            raise

    def _load_source_items(self, connector: ZoteroConnector, source: dict[str, Any]) -> list[dict[str, Any]]:
        selected = [str(key).upper() for key in source.get("collectionKeys") or []]
        if source.get("includeSubcollections") and selected:
            collections = connector.list_collections()
            pending = list(selected)
            while pending:
                parent = pending.pop()
                for collection in collections:
                    key = str(collection.get("key") or "")
                    if collection.get("parentCollection") == parent and key not in selected:
                        selected.append(key)
                        pending.append(key)
        raw_items = (
            [item for key in selected for item in connector.list_top_items(key)]
            if selected
            else connector.list_top_items()
        )
        deduplicated: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            data = self._item_data(item)
            item_type = str(data.get("itemType") or "")
            if item_type == "attachment" and not source.get("includeStandaloneAttachments"):
                continue
            key = self._item_key(item)
            if key:
                deduplicated[key] = item
        return list(deduplicated.values())

    def _sync_item(
        self,
        connector: ZoteroConnector,
        source: dict[str, Any],
        item: dict[str, Any],
        *,
        cancel_event=None,
    ) -> str | ZoteroParseCandidate:
        data = self._item_data(item)
        item_key = self._item_key(item)
        item_version = self._item_version(item)
        is_standalone = str(data.get("itemType") or "") == "attachment"
        children = [item] if is_standalone else connector.list_children(item_key)
        attachments = [child for child in children if self._is_pdf_attachment(child)]
        resolved: tuple[dict[str, Any], Path] | None = None
        for attachment in attachments:
            attachment_key = self._item_key(attachment)
            if not attachment_key:
                continue
            try:
                path = connector.resolve_attachment_path(attachment_key)
            except Exception as error:
                self._log(f"Zotero 附件当前不可用：key={attachment_key}, error={error}")
                continue
            if path and path.suffix.lower() == ".pdf":
                resolved = attachment, path
                break
        if not resolved:
            self.sources.upsert_item(
                source["id"], item_key, item_version=item_version,
                status="unavailable", error_message="没有可读取的本地 PDF 附件",
            )
            return "unavailable"

        attachment, pdf_path = resolved
        attachment_key = self._item_key(attachment)
        attachment_version = self._item_version(attachment)
        file_hash = self._hash_file(pdf_path, cancel_event=cancel_event)
        previous = self.sources.get_item(source["id"], item_key)
        paper_id = self._paper_id(source, item_key)
        metadata = self._paper_metadata(data, item_key, attachment_key)
        existing_paper = self.agent.get_saved_paper(paper_id)

        same_file = bool(previous and previous.get("file_hash") == file_hash)
        same_version = bool(
            previous
            and int(previous.get("item_version") or 0) == item_version
            and int(previous.get("attachment_version") or 0) == attachment_version
        )
        if same_file and existing_paper:
            if same_version:
                outcome = "unchanged"
            else:
                self.agent.update_saved_paper(paper_id, {
                    "title": metadata["title"],
                    "authors": metadata["authors"],
                    "abstract": metadata["abstract"],
                    "year": metadata["year"],
                    "doi": metadata["doi"],
                    "url": metadata["url"],
                    "venue": metadata["venue"],
                    "customTags": metadata["custom_tags"],
                    "pdfPath": str(pdf_path),
                    "externalSourceMetadata": {
                        "zoteroItemKey": item_key,
                        "zoteroAttachmentKey": attachment_key,
                        "zoteroItemVersion": item_version,
                        "zoteroAttachmentVersion": attachment_version,
                    },
                })
                outcome = "metadataUpdated"
        else:
            self.sources.upsert_item(
                source["id"], item_key,
                item_version=item_version,
                attachment_key=attachment_key,
                attachment_version=attachment_version,
                file_path=str(pdf_path),
                file_hash=file_hash,
                paper_id=paper_id,
                status="discovered",
            )
            return ZoteroParseCandidate(
                source=source,
                item_key=item_key,
                item_version=item_version,
                attachment_key=attachment_key,
                attachment_version=attachment_version,
                pdf_path=pdf_path,
                file_hash=file_hash,
                paper_id=paper_id,
                metadata=metadata,
            )

        self.projects.add_papers(source["projectId"], [paper_id])
        self.sources.upsert_item(
            source["id"], item_key,
            item_version=item_version,
            attachment_key=attachment_key,
            attachment_version=attachment_version,
            file_path=str(pdf_path),
            file_hash=file_hash,
            paper_id=paper_id,
            status="ready",
        )
        return outcome

    def _process_candidates(
        self,
        candidates: list[ZoteroParseCandidate],
        stats: dict[str, int],
        *,
        cancel_event=None,
    ) -> None:
        """优先批量调用 MinerU；未配置 Token 时保持原有单篇回退行为。"""
        token = settings.mineru_api_token.strip()
        if not token:
            for index, candidate in enumerate(candidates, start=1):
                raise_if_task_cancelled(cancel_event)
                try:
                    self._commit_candidate(candidate, preparsed_result=None, cancel_event=cancel_event)
                    stats["indexed"] += 1
                except TaskCancelled:
                    raise
                except Exception as error:
                    stats["failed"] += 1
                    self._mark_candidate_failed(candidate, error)
                self._progress(
                    45 + int(index * 45 / max(1, len(candidates))),
                    "indexing",
                    f"正在索引 Zotero PDF {index}/{len(candidates)}",
                )
            return

        batch_inputs = [self._batch_input(candidate) for candidate in candidates]
        by_data_id = {item.data_id: candidate for item, candidate in zip(batch_inputs, candidates, strict=True)}
        coordinator = MinerUBatchCoordinator(
            repository=DocumentParseRepository(self.metadata_db_path),
            client=MinerUCloudBatchClient(token=token),
        )

        def report(completed: int, total: int, message: str) -> None:
            self._progress(
                35 + int(completed * 40 / max(1, total)),
                "parsing",
                f"{message}（{completed}/{total}）",
            )

        outcome = coordinator.process(
            batch_inputs,
            cancel_event=cancel_event,
            progress_callback=report,
        )
        for data_id, error in outcome.errors.items():
            candidate = by_data_id.get(data_id)
            if candidate:
                stats["failed"] += 1
                self._mark_candidate_failed(candidate, RuntimeError(error))

        def commit(data_id: str, result: dict[str, Any]) -> str:
            candidate = by_data_id[data_id]
            preparsed = self._preparsed_result(result)
            self._commit_candidate(candidate, preparsed_result=preparsed, cancel_event=cancel_event)
            return data_id

        with ThreadPoolExecutor(max_workers=settings.mineru_index_concurrency) as executor:
            futures = {
                executor.submit(commit, data_id, result): data_id
                for data_id, result in outcome.results.items()
                if data_id in by_data_id
            }
            completed = 0
            for future in as_completed(futures):
                data_id = futures[future]
                candidate = by_data_id[data_id]
                try:
                    future.result()
                    stats["indexed"] += 1
                except TaskCancelled:
                    raise
                except Exception as error:
                    stats["failed"] += 1
                    self._mark_candidate_failed(candidate, error)
                completed += 1
                self._progress(
                    76 + int(completed * 18 / max(1, len(futures))),
                    "indexing",
                    f"正在建立 Markdown 索引 {completed}/{len(futures)}",
                )

    def _commit_candidate(
        self,
        candidate: ZoteroParseCandidate,
        *,
        preparsed_result: dict[str, object] | None,
        cancel_event=None,
    ) -> None:
        raise_if_task_cancelled(cancel_event)
        self.agent.index_linked_pdf_paper(
            pdf_path=candidate.pdf_path,
            record_id=candidate.paper_id,
            source="zotero",
            external_id=candidate.item_key,
            source_metadata={
                "zoteroItemKey": candidate.item_key,
                "zoteroAttachmentKey": candidate.attachment_key,
                "zoteroItemVersion": candidate.item_version,
                "zoteroAttachmentVersion": candidate.attachment_version,
            },
            preparsed_result=preparsed_result,
            cancel_event=cancel_event,
            **candidate.metadata,
        )
        self.projects.add_papers(candidate.source["projectId"], [candidate.paper_id])
        self.sources.upsert_item(
            candidate.source["id"], candidate.item_key,
            item_version=candidate.item_version,
            attachment_key=candidate.attachment_key,
            attachment_version=candidate.attachment_version,
            file_path=str(candidate.pdf_path),
            file_hash=candidate.file_hash,
            paper_id=candidate.paper_id,
            status="ready",
        )

    def _mark_candidate_failed(self, candidate: ZoteroParseCandidate, error: Exception) -> None:
        self.sources.upsert_item(
            candidate.source["id"], candidate.item_key,
            item_version=candidate.item_version,
            attachment_key=candidate.attachment_key,
            attachment_version=candidate.attachment_version,
            file_path=str(candidate.pdf_path),
            file_hash=candidate.file_hash,
            paper_id=candidate.paper_id,
            status="failed",
            error_message=str(error),
        )
        self._log(f"Zotero PDF 处理失败：key={candidate.item_key}, error={error}")

    @staticmethod
    def _batch_input(candidate: ZoteroParseCandidate) -> MinerUBatchInput:
        data_id = f"z_{candidate.attachment_key}_{candidate.file_hash[:16]}"
        return MinerUBatchInput(
            source_id=candidate.source["id"],
            source_item_key=candidate.item_key,
            attachment_key=candidate.attachment_key,
            file_hash=candidate.file_hash,
            pdf_path=candidate.pdf_path,
            output_name=candidate.paper_id,
            data_id=data_id[:128],
            parse_key=build_parse_key(candidate.file_hash),
        )

    @staticmethod
    def _preparsed_result(result: dict[str, Any]) -> dict[str, object]:
        markdown_path = Path(str(result.get("markdownPath") or ""))
        markdown_text = markdown_path.read_text(encoding="utf-8", errors="ignore")
        if not markdown_text.strip():
            raise RuntimeError("MinerU 生成的 Markdown 为空")
        compact_length = len(re.sub(r"\s+", "", markdown_text))
        return {
            "text": markdown_text,
            "metadata": {},
            "parser": "mineru",
            "warning": str(result.get("publishWarning") or ""),
            "indexWarning": (
                "MinerU Markdown 内容较短，请确认原文页数和解析完整性"
                if compact_length < 800 else ""
            ),
            "markdownPath": str(markdown_path),
            "outputDir": str(result.get("outputDir") or markdown_path.parent),
            "mineruResult": result,
        }

    @staticmethod
    def _paper_metadata(data: dict[str, Any], item_key: str, attachment_key: str) -> dict[str, Any]:
        creators = data.get("creators") if isinstance(data.get("creators"), list) else []
        authors = []
        for creator in creators:
            if not isinstance(creator, dict):
                continue
            name = str(creator.get("name") or "").strip()
            if not name:
                name = " ".join(
                    part for part in [str(creator.get("firstName") or "").strip(), str(creator.get("lastName") or "").strip()]
                    if part
                )
            if name:
                authors.append(name)
        tags = data.get("tags") if isinstance(data.get("tags"), list) else []
        custom_tags = [str(tag.get("tag") or "").strip() for tag in tags if isinstance(tag, dict) and str(tag.get("tag") or "").strip()]
        date = str(data.get("date") or "")
        year_match = re.search(r"(?:19|20)\d{2}", date)
        return {
            "title": str(data.get("title") or item_key),
            "authors": authors,
            "abstract": str(data.get("abstractNote") or ""),
            "year": year_match.group(0) if year_match else date[:20],
            "doi": str(data.get("DOI") or ""),
            "url": str(data.get("url") or ""),
            "venue": str(data.get("publicationTitle") or data.get("conferenceName") or ""),
            "custom_tags": custom_tags,
        }

    @staticmethod
    def _item_data(item: dict[str, Any]) -> dict[str, Any]:
        return item.get("data") if isinstance(item.get("data"), dict) else item

    @classmethod
    def _item_key(cls, item: dict[str, Any]) -> str:
        data = cls._item_data(item)
        return str(data.get("key") or item.get("key") or "").strip().upper()

    @classmethod
    def _item_version(cls, item: dict[str, Any]) -> int:
        data = cls._item_data(item)
        return int(data.get("version") or item.get("version") or 0)

    @classmethod
    def _is_pdf_attachment(cls, item: dict[str, Any]) -> bool:
        data = cls._item_data(item)
        if str(data.get("itemType") or "") != "attachment":
            return False
        filename = str(data.get("filename") or data.get("title") or "").lower()
        return str(data.get("contentType") or "").lower() == "application/pdf" or filename.endswith(".pdf")

    @staticmethod
    def _paper_id(source: dict[str, Any], item_key: str) -> str:
        return f"zotero:{source['libraryType']}:{source['libraryId']}:{item_key}"

    @staticmethod
    def _hash_file(path: Path, *, cancel_event=None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                raise_if_task_cancelled(cancel_event)
                digest.update(chunk)
        return digest.hexdigest()

    def _progress(self, progress: int, stage: str, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(progress, stage, message)

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)


__all__ = ["ZoteroSyncService"]
