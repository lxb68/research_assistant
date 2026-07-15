"""检索、筛选、下载和持久化论文，并维护本地论文元数据。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

from app.core.config import settings
from app.services.ccf_catalog import CcfCatalog
from app.services.paper_search import SUPPORTED_SOURCES
from app.services.model_client import chat_completion
from app.services.model_config import ModelConfigStore
from app.services.providers.arxiv import ARXIV_API_URL
from app.services.providers.ieee import IEEE_API_URL
from app.services.split import (
    DEFAULT_MAX_SPLIT_LENGTH,
    DEFAULT_MIN_SPLIT_LENGTH,
    split_markdown_document,
)
from app.services.sjr_metrics import SjrMetrics
from app.services.venue_metrics import enrich_paper_metrics
import requests


Paper = dict[str, object]
SearchTool = Callable[[str, int], list[dict]]
MAX_SEARCH_ROUNDS = 5


class HunterAgent:
    """论文搜索采集 Agent：搜索、去重、排序、下载 PDF，并保存元数据。"""
    def __init__(
        self,
        *,
        download_dir: str | Path | None = None,
        metadata_db_path: str | Path | None = None,
        sources: list[str] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """配置 arXiv/IEEE 地址、下载目录，并注册搜索/下载工具。"""
        self.arxiv_api_url = ARXIV_API_URL
        self.ieee_api_url = IEEE_API_URL
        self.download_dir = Path(download_dir or settings.hunter_download_dir)
        self.metadata_db_path = Path(metadata_db_path or settings.hunter_metadata_db)
        self.sources = sources or ["arxiv", "pubmed", "crossref", "ieee", "open_access"]
        self.search_tools: dict[str, SearchTool] = dict(SUPPORTED_SOURCES)
        self.download_tool = self._download_pdf
        self.logs: list[str] = []
        self.log_callback = log_callback
        self.ccf_catalog = CcfCatalog()
        self.sjr_metrics = SjrMetrics()
        self.translator = None
        self.translation_cache: dict[str, str] = {}

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_metadata_db()

    def run(
        self,
        keyword: str,
        *,
        sources: list[str] | None = None,
        limit_per_source: int = 10,
        download_pdf: bool = True,
        year_from: int | None = None,
        year_to: int | None = None,
        min_impact_factor: float | None = None,
        ccf_levels: list[str] | None = None,
    ) -> dict:
        """论文搜索主流程：循环扩大候选池，直到保存目标数量或达到最大轮次。"""
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            raise ValueError("搜索关键词不能为空")

        active_sources = sources or self.sources
        if "arxiv" in {source.lower().strip() for source in active_sources} and len(active_sources) > 1:
            active_sources = [
                *[source for source in active_sources if source.lower().strip() != "arxiv"],
                *[source for source in active_sources if source.lower().strip() == "arxiv"],
            ]
        target_per_source = max(1, min(limit_per_source, 200))
        normalized_sources = [source.lower().strip() for source in active_sources]
        total_target_count = target_per_source * len(normalized_sources)
        effective_min_impact_factor = (
            min_impact_factor if min_impact_factor is not None and min_impact_factor > 0 else None
        )
        search_keyword = self._expand_keyword(normalized_keyword)

        self._log(
            f"开始任务 keyword={normalized_keyword!r}, search_keyword={search_keyword!r}, "
            f"sources={active_sources}, target_per_source={target_per_source}, download_pdf={download_pdf}, "
            f"year_from={year_from}, year_to={year_to}, "
            f"min_impact_factor={effective_min_impact_factor}, ccf_levels={ccf_levels}",
        )
        if "arxiv" in {source.lower().strip() for source in active_sources} and (
            effective_min_impact_factor is not None or ccf_levels
        ):
            self._log("arXiv 是预印本来源，后端不会对 arXiv 结果应用 CCF 等级和影响因子过滤")

        try:
            ccf_count = self.ccf_catalog.ensure_loaded()
            self._log(f"CCF 本地数据库可用，条目数 {ccf_count}")
        except Exception as error:
            self._log(f"CCF 本地数据库不可用，继续使用空匹配: {error}")

        saved_papers: list[Paper] = []
        saved_keys: set[str] = set()
        saved_counts_by_source = {source: 0 for source in normalized_sources}
        latest_search_result: dict = {"papers": [], "errors": []}
        latest_deduplicated_papers: list[Paper] = []
        latest_filtered_papers: list[Paper] = []

        for round_index in range(1, MAX_SEARCH_ROUNDS + 1):
            per_source_limit = min(200, target_per_source * round_index)
            self._log(
                f"第 {round_index}/{MAX_SEARCH_ROUNDS} 轮检索：每个数据源最多取 {per_source_limit} 篇，"
                f"当前已保存 {len(saved_papers)}/{total_target_count} 篇，各数据源={saved_counts_by_source}",
            )

            latest_search_result = self._search_papers(
                keyword=search_keyword,
                sources=active_sources,
                limit_per_source=per_source_limit,
            )
            self._log(
                f"搜索完成，候选论文 {len(latest_search_result['papers'])} 篇，"
                f"错误 {len(latest_search_result['errors'])} 个",
            )

            latest_deduplicated_papers = self._deduplicate_papers(latest_search_result["papers"])
            self._log(f"去重完成，剩余 {len(latest_deduplicated_papers)} 篇")

            enriched_papers = [
                enrich_paper_metrics(
                    paper,
                    ccf_catalog=self.ccf_catalog,
                    sjr_metrics=self.sjr_metrics,
                )
                for paper in latest_deduplicated_papers
            ]
            self._log("指标补充完成：已尝试匹配 SJR/影响因子代理指标和 CCF 等级")

            latest_filtered_papers = self._filter_papers(
                enriched_papers,
                search_keyword,
                year_from=year_from,
                year_to=year_to,
                min_impact_factor=effective_min_impact_factor,
                ccf_levels=ccf_levels,
            )
            self._log(f"初筛完成，满足条件候选 {len(latest_filtered_papers)} 篇")

            papers_with_pdf = [
                paper for paper in latest_filtered_papers if paper.get("pdfUrl") or paper.get("pdf_url")
            ]
            papers_without_pdf = [
                paper for paper in latest_filtered_papers if not (paper.get("pdfUrl") or paper.get("pdf_url"))
            ]
            candidate_papers = papers_with_pdf
            if round_index >= MAX_SEARCH_ROUNDS:
                candidate_papers = [*papers_with_pdf, *papers_without_pdf]
                if papers_without_pdf:
                    self._log(
                        f"已达到最大检索轮次，允许保存 {len(papers_without_pdf)} 篇无 PDF 候选的元数据，"
                        "前端会提示用户自行下载原文"
                    )
            elif papers_without_pdf:
                self._log(
                    f"本轮优先下载 PDF，暂缓 {len(papers_without_pdf)} 篇无 PDF 候选，"
                    "若最后仍不足再保存元数据"
                )

            for paper in candidate_papers:
                source = str(paper.get("source", "")).lower()
                if saved_counts_by_source.get(source, 0) >= target_per_source:
                    continue

                paper_key = self._build_dedupe_key(paper)
                if paper_key in saved_keys:
                    continue

                existing_paper = self._find_existing_paper(paper)
                if existing_paper and self._paper_has_local_pdf(existing_paper):
                    title = str(existing_paper.get("title", paper.get("title", "")))[:80]
                    source_target_index = saved_counts_by_source.get(source, 0) + 1
                    self._log(
                        f"[{source}] 已存在且本地 PDF 可用，复用论文 "
                        f"{source_target_index}/{target_per_source}: {title}"
                    )
                    reused_paper = {**existing_paper, "reusedFromDatabase": True}
                    if source == "arxiv":
                        reused_paper = self._clear_preprint_metrics(reused_paper)
                    saved_papers.append(reused_paper)
                    saved_keys.add(paper_key)
                    saved_counts_by_source[source] = saved_counts_by_source.get(source, 0) + 1
                    continue

                if existing_paper:
                    title = str(existing_paper.get("title", paper.get("title", "")))[:80]
                    self._log(
                        f"[{source}] 数据库已有元数据但没有本地 PDF，跳过该候选并继续寻找新论文: {title}"
                    )
                    saved_keys.add(paper_key)
                    continue

                title = str(paper.get("title", ""))[:80]
                source_target_index = saved_counts_by_source.get(source, 0) + 1
                self._log(f"[{source}] 保存论文 {source_target_index}/{target_per_source}: {title}")
                saved_papers.append(
                    self._download_and_save_paper(
                        paper,
                        keyword=normalized_keyword,
                        search_keyword=search_keyword,
                        download_pdf=download_pdf,
                    )
                )
                saved_keys.add(paper_key)
                saved_counts_by_source[source] = saved_counts_by_source.get(source, 0) + 1

            if all(saved_counts_by_source.get(source, 0) >= target_per_source for source in normalized_sources):
                self._log(f"每个数据源均已达到目标论文数量 {target_per_source}，停止检索")
                break

            self._log(
                f"本轮结束仍未达到目标数量，准备扩大候选池；"
                f"当前各数据源保存数量={saved_counts_by_source}",
            )

        self._log(f"任务完成，保存 {len(saved_papers)} 篇")

        return {
            "keyword": normalized_keyword,
            "searchKeyword": search_keyword,
            "sources": active_sources,
            "targetPerSource": target_per_source,
            "targetCount": total_target_count,
            "searchedCount": len(latest_search_result["papers"]),
            "deduplicatedCount": len(latest_deduplicated_papers),
            "filteredCount": len(latest_filtered_papers),
            "savedCount": len(saved_papers),
            "savedCountsBySource": saved_counts_by_source,
            "errors": latest_search_result["errors"],
            "logs": self.logs,
            "papers": saved_papers,
        }

    def _search_papers(
        self,
        *,
        keyword: str,
        sources: list[str],
        limit_per_source: int,
    ) -> dict:
        """调用不同的数据源搜索文献。"""
        papers: list[Paper] = []
        errors: list[dict[str, str]] = []

        for source in sources:
            source_key = source.lower().strip()
            search_tool = self.search_tools.get(source_key)
            self._log(f"开始搜索数据源 {source_key}")

            if search_tool is None:
                message = "不支持的数据源"
                errors.append({"source": source_key, "message": message})
                self._log(f"数据源 {source_key} 失败: {message}")
                continue

            try:
                source_papers = search_tool(keyword, limit_per_source)
            except Exception as error:
                errors.append({"source": source_key, "message": str(error)})
                self._log(f"数据源 {source_key} 搜索失败: {error}")
                continue

            self._log(f"数据源 {source_key} 返回 {len(source_papers)} 篇")
            for paper in source_papers:
                papers.append({**paper, "source": paper.get("source") or source_key})

        return {"papers": papers, "errors": errors}

    def _deduplicate_papers(self, papers: list[Paper]) -> list[Paper]:
        """根据标题、DOI、arXiv ID 等信息去除重复论文。"""
        seen: set[str] = set()
        deduplicated: list[Paper] = []

        for paper in papers:
            dedupe_key = self._build_dedupe_key(paper)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduplicated.append(paper)

        return deduplicated

    def _filter_papers(
        self,
        papers: list[Paper],
        keyword: str,
        *,
        year_from: int | None = None,
        year_to: int | None = None,
        min_impact_factor: float | None = None,
        ccf_levels: list[str] | None = None,
    ) -> list[Paper]:
        """先按年份和相关性筛选，再应用 SJR/影响因子和 CCF 条件。"""
        keyword_tokens = self._tokenize(keyword)
        allowed_ccf_levels = {level.upper() for level in ccf_levels or []}
        allow_non_ccf = "NON_CCF" in allowed_ccf_levels

        filtered: list[Paper] = []
        for paper in papers:
            source_key = str(paper.get("source", "")).lower()
            is_preprint_source = source_key == "arxiv"
            title_preview = str(paper.get("title", ""))[:80]
            paper_year = self._parse_year(paper.get("year") or paper.get("publishedAt"))
            if year_from and (paper_year is None or paper_year < year_from):
                self._log(f"过滤论文：年份不满足，year={paper_year}, title={title_preview}")
                continue
            if year_to and (paper_year is None or paper_year > year_to):
                self._log(f"过滤论文：年份不满足，year={paper_year}, title={title_preview}")
                continue

            title = str(paper.get("title", ""))
            abstract = str(paper.get("abstract", ""))
            searchable_text = f"{title} {abstract}".lower()
            if keyword_tokens:
                matched_tokens = [token for token in keyword_tokens if token in searchable_text]
                relevance_score = len(matched_tokens) / len(keyword_tokens)
                if relevance_score <= 0:
                    self._log(f"过滤论文：相关性为 0，title={title_preview}")
                    continue
            else:
                relevance_score = 0

            impact_factor = paper.get("impactFactor")
            if min_impact_factor is not None and not is_preprint_source:
                if not isinstance(impact_factor, (int, float)) or impact_factor < min_impact_factor:
                    self._log(
                        f"过滤论文：SJR/影响因子代理指标不满足，metric={impact_factor}, "
                        f"min={min_impact_factor}, title={title_preview}",
                    )
                    continue

            ccf_level = str(paper.get("ccfLevel", "")).upper()
            if allowed_ccf_levels and not is_preprint_source:
                is_non_ccf = not ccf_level
                matched_ccf = ccf_level in allowed_ccf_levels or (allow_non_ccf and is_non_ccf)
                if not matched_ccf:
                    self._log(
                        f"过滤论文：CCF 条件不满足，ccf={ccf_level or '非 CCF/未知'}, "
                        f"allowed={sorted(allowed_ccf_levels)}, title={title_preview}",
                    )
                    continue

            filtered.append(
                {
                    **paper,
                    "impactFactor": None if is_preprint_source else paper.get("impactFactor"),
                    "sjr": None if is_preprint_source else paper.get("sjr"),
                    "metricSource": "" if is_preprint_source else paper.get("metricSource", ""),
                    "ccfLevel": "" if is_preprint_source else paper.get("ccfLevel", ""),
                    "ccfSource": "" if is_preprint_source else paper.get("ccfSource", ""),
                    "ccfMatchedName": "" if is_preprint_source else paper.get("ccfMatchedName", ""),
                    "relevanceScore": round(relevance_score, 3),
                    "preprintSource": is_preprint_source,
                    "metricFiltersIgnored": is_preprint_source,
                }
            )

        return sorted(
            filtered,
            key=lambda item: (
                0 if str(item.get("source", "")).lower() == "arxiv" else 1,
                float(item.get("relevanceScore", 0)),
                float(item.get("impactFactor") or 0),
                str(item.get("ccfLevel", "")),
            ),
            reverse=True,
        )

    def _download_and_save_paper(
        self,
        paper: Paper,
        *,
        keyword: str,
        search_keyword: str,
        download_pdf: bool = True,
    ) -> Paper:
        """下载 PDF 并把论文信息写入数据库。"""
        pdf_path = ""
        pdf_error = ""
        pdf_url = str(paper.get("pdfUrl") or paper.get("pdf_url") or "")
        source = str(paper.get("source", "")).lower()
        source_prefix = f"[{source}]" if source else "[unknown]"

        if download_pdf and pdf_url:
            try:
                self._log(f"{source_prefix} 开始下载 PDF: {pdf_url}")
                pdf_path = str(self.download_tool(pdf_url, paper))
                self._log(f"{source_prefix} PDF 下载完成: {pdf_path}")
            except Exception as error:
                pdf_error = str(error)
                self._log(f"{source_prefix} PDF 下载失败: {pdf_error}")
        elif download_pdf:
            self._log(f"{source_prefix} 跳过 PDF 下载：该论文没有 pdfUrl")

        requires_manual_download = download_pdf and (not pdf_path)
        manual_download_reason = ""
        if requires_manual_download and pdf_error:
            manual_download_reason = f"PDF 自动下载失败：{pdf_error}"
        elif requires_manual_download:
            manual_download_reason = "该数据源没有提供可直接下载的 PDF 地址，需要用户根据论文链接手动下载。"

        saved_paper = {
            **paper,
            "keyword": keyword,
            "searchKeyword": search_keyword,
            "pdfPath": pdf_path,
            "pdfDownloadError": pdf_error,
            "requiresManualDownload": requires_manual_download,
            "manualDownloadReason": manual_download_reason,
            "savedAt": datetime.now(timezone.utc).isoformat(),
        }

        return self._save_paper_to_db(saved_paper)

    def _save_paper_to_db(self, paper: Paper) -> Paper:
        """封装数据库保存逻辑。"""
        record = self._json_safe_paper(paper)
        existing_id = str(record.get("id", "")).strip()
        record["id"] = existing_id or self._build_record_id(record)

        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.execute(
                """
                INSERT INTO papers (
                    id,
                    source,
                    title,
                    doi,
                    external_id,
                    url,
                    pdf_url,
                    pdf_path,
                    keyword,
                    relevance_score,
                    metadata_json,
                    saved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    title = excluded.title,
                    doi = excluded.doi,
                    external_id = excluded.external_id,
                    url = excluded.url,
                    pdf_url = excluded.pdf_url,
                    pdf_path = excluded.pdf_path,
                    keyword = excluded.keyword,
                    relevance_score = excluded.relevance_score,
                    metadata_json = excluded.metadata_json,
                    saved_at = excluded.saved_at
                """,
                (
                    record["id"],
                    record.get("source", ""),
                    record.get("title", ""),
                    record.get("doi", ""),
                    record.get("externalId") or record.get("external_id") or "",
                    record.get("url", ""),
                    record.get("pdfUrl") or record.get("pdf_url") or "",
                    record.get("pdfPath", ""),
                    record.get("keyword", ""),
                    record.get("relevanceScore", 0),
                    json.dumps(record, ensure_ascii=False),
                    record.get("savedAt", ""),
                ),
            )
            connection.commit()

        source = str(record.get("source", "")).lower() or "unknown"
        self._log(f"[{source}] 元数据已保存: {record['id']}")
        return record

    def attach_local_pdf(
        self,
        *,
        pdf_path: str | Path,
        record_id: str | None = None,
        doi: str | None = None,
        title: str | None = None,
    ) -> Paper:
        """把用户手动下载的本地 PDF 路径绑定到已有论文元数据记录。"""
        local_pdf_path = Path(pdf_path).expanduser().resolve()
        if not local_pdf_path.exists() or not local_pdf_path.is_file():
            raise ValueError(f"PDF 文件不存在: {local_pdf_path}")
        if local_pdf_path.suffix.lower() != ".pdf":
            raise ValueError("只能绑定 .pdf 文件")

        record = self._find_existing_paper_by_fields(record_id=record_id, doi=doi, title=title)
        if not record:
            raise ValueError("没有找到匹配的论文元数据记录，请提供 record_id、doi 或 title")

        record["pdfPath"] = str(local_pdf_path)
        record["requiresManualDownload"] = False
        record["manualDownloadReason"] = ""
        record["pdfDownloadError"] = ""
        record["updatedAt"] = datetime.now(timezone.utc).isoformat()
        return self._save_paper_to_db(record)

    def cleanup_records_without_local_pdf(self) -> dict:
        """删除数据库中没有对应本地 PDF 文件的论文元数据记录。"""
        removed_records: list[Paper] = []
        kept_records = 0

        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, source, title, doi, external_id, url, pdf_url, pdf_path, keyword,
                       relevance_score, metadata_json, saved_at
                FROM papers
                """,
            ).fetchall()

            for row in rows:
                record = self._row_to_paper(row)
                if self._paper_has_local_pdf(record):
                    kept_records += 1
                    continue

                removed_records.append(record)

            if removed_records:
                connection.executemany(
                    "DELETE FROM papers WHERE id = ?",
                    [(record.get("id", ""),) for record in removed_records],
                )
                connection.commit()

        self._log(
            f"清理无 PDF 元数据完成：删除 {len(removed_records)} 条，保留 {kept_records} 条",
        )
        return {
            "removedCount": len(removed_records),
            "keptCount": kept_records,
            "removedRecords": removed_records,
        }

    def deduplicate_saved_papers(self, record_id: str | None = None) -> dict:
        """合并本地数据库中的重复论文记录。"""
        papers = self.list_saved_papers(limit=500)
        groups: dict[str, list[Paper]] = {}

        for paper in papers:
            dedupe_group_key = self._build_duplicate_group_key(paper)
            if not dedupe_group_key:
                continue
            groups.setdefault(dedupe_group_key, []).append(paper)

        merged_count = 0
        removed_ids: list[str] = []
        canonical_papers: list[Paper] = []
        preferred_id = str(record_id or "").strip()

        for group_papers in groups.values():
            if len(group_papers) < 2:
                continue
            if preferred_id and not any(str(paper.get("id", "")).strip() == preferred_id for paper in group_papers):
                continue

            canonical = self._select_canonical_paper(group_papers, preferred_id=preferred_id)
            duplicates = [
                paper
                for paper in group_papers
                if str(paper.get("id", "")).strip() != str(canonical.get("id", "")).strip()
            ]
            if not duplicates:
                continue

            merged = self._merge_paper_group(canonical, duplicates)
            self._save_paper_to_db(merged)
            duplicate_ids = [
                str(paper.get("id", "")).strip()
                for paper in duplicates
                if str(paper.get("id", "")).strip()
            ]
            self._delete_paper_rows_only(duplicate_ids)

            merged_count += len(duplicates)
            removed_ids.extend(duplicate_ids)
            canonical_papers.append(merged)

        self._log(
            f"重复论文合并完成：合并 {merged_count} 条重复记录，保留 {len(canonical_papers)} 条主记录"
        )
        return {
            "mergedCount": merged_count,
            "removedIds": removed_ids,
            "canonicalPapers": canonical_papers,
        }

    def list_saved_papers(self, *, limit: int = 100, keyword: str | None = None) -> list[Paper]:
        """读取已保存的论文元数据，用于前端浏览本地数据集。"""
        clauses: list[str] = []
        params: list[object] = []

        if keyword:
            clauses.append("(keyword LIKE ? OR title LIKE ?)")
            keyword_pattern = f"%{keyword}%"
            params.extend([keyword_pattern, keyword_pattern])

        query = (
            "SELECT id, source, title, doi, external_id, url, pdf_url, pdf_path, keyword, "
            "relevance_score, metadata_json, saved_at FROM papers"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY saved_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))

        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()

        return [self._row_to_paper(row) for row in rows]

    def delete_saved_papers(self, ids: list[str]) -> dict:
        """按论文记录 ID 批量删除已保存的元数据记录，不删除本地 PDF 文件。"""
        normalized_ids = sorted({str(record_id).strip() for record_id in ids if str(record_id).strip()})
        if not normalized_ids:
            return {"deletedCount": 0, "deletedIds": []}

        placeholders = ", ".join("?" for _ in normalized_ids)
        deleted_records: list[Paper] = []
        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                (
                    "SELECT id, source, title, doi, external_id, url, pdf_url, pdf_path, keyword, "
                    "relevance_score, metadata_json, saved_at FROM papers "
                    f"WHERE id IN ({placeholders})"
                ),
                normalized_ids,
            ).fetchall()
            deleted_records = [self._row_to_paper(row) for row in rows]
            deleted_ids = [str(record.get("id", "")).strip() for record in deleted_records if str(record.get("id", "")).strip()]
            if deleted_ids:
                delete_placeholders = ", ".join("?" for _ in deleted_ids)
                connection.execute(
                    f"DELETE FROM papers WHERE id IN ({delete_placeholders})",
                    deleted_ids,
                )
                connection.commit()

        self._log(f"批量删除论文元数据：请求 {len(normalized_ids)} 条，删除 {len(deleted_ids)} 条")
        removed_pdf_count = 0
        removed_markdown_count = 0
        for record in deleted_records:
            pdf_path = str(record.get("pdfPath") or record.get("pdf_path") or "").strip()
            if self._delete_local_pdf_if_managed(pdf_path):
                removed_pdf_count += 1
            if self._delete_markdown_output_if_managed(record):
                removed_markdown_count += 1

        return {
            "deletedCount": len(deleted_ids),
            "deletedIds": deleted_ids,
            "deletedPdfCount": removed_pdf_count,
            "deletedMarkdownCount": removed_markdown_count,
        }

    def _delete_paper_rows_only(self, ids: list[str]) -> None:
        """删除论文。"""
        normalized_ids = [str(record_id).strip() for record_id in ids if str(record_id).strip()]
        if not normalized_ids:
            return

        placeholders = ", ".join("?" for _ in normalized_ids)
        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.execute(
                f"DELETE FROM papers WHERE id IN ({placeholders})",
                normalized_ids,
            )
            connection.commit()

    def get_saved_paper(self, record_id: str) -> Paper | None:
        """按记录 ID 读取一条已保存论文。"""
        normalized_id = record_id.strip()
        if not normalized_id:
            return None

        return self._find_existing_paper_by_fields(record_id=normalized_id)

    def update_saved_paper(self, record_id: str, updates: dict[str, object]) -> Paper:
        """更新指定论文记录中允许修改的字段。"""
        normalized_id = record_id.strip()
        if not normalized_id:
            raise ValueError("record_id is required")

        record = self.get_saved_paper(normalized_id)
        if not record:
            raise ValueError(f"Paper record not found: {normalized_id}")

        record.update(updates)
        record["updatedAt"] = datetime.now(timezone.utc).isoformat()
        return self._save_paper_to_db(record)

    def refresh_paper_metadata_from_markdown(
        self,
        record_id: str,
        *,
        markdown_path: str | Path | None = None,
    ) -> Paper:
        """从解析后的 Markdown 刷新论文元数据。"""
        normalized_id = record_id.strip()
        if not normalized_id:
            raise ValueError("record_id is required")

        record = self.get_saved_paper(normalized_id)
        if not record:
            raise ValueError(f"Paper record not found: {normalized_id}")

        resolved_markdown = self._resolve_markdown_for_metadata_refresh(record, markdown_path)
        markdown_text = resolved_markdown.read_text(encoding="utf-8", errors="ignore")
        if not markdown_text.strip():
            raise ValueError(f"Markdown file is empty: {resolved_markdown}")

        parsed = self._parse_markdown_metadata(markdown_text, record)
        updates = self._build_metadata_refresh_updates(record, parsed, resolved_markdown)
        if not updates:
            self._log(f"Markdown 元数据回写：未发现可更新字段 record_id={normalized_id}")
            return record

        self._log(
            f"Markdown 元数据回写：record_id={normalized_id}, "
            f"fields={sorted(updates.keys())}, markdown={resolved_markdown}"
        )
        return self.update_saved_paper(normalized_id, updates)

    def split_saved_paper_from_markdown(
        self,
        record_id: str,
        *,
        markdown_path: str | Path | None = None,
        min_split_length: int | None = None,
        max_split_length: int | None = None,
    ) -> Paper:
        """读取论文 Markdown 并重新生成结构化分块。"""
        normalized_id = record_id.strip()
        if not normalized_id:
            raise ValueError("record_id is required")

        record = self.get_saved_paper(normalized_id)
        if not record:
            raise ValueError(f"Paper record not found: {normalized_id}")

        resolved_markdown = self._resolve_markdown_for_metadata_refresh(record, markdown_path)
        markdown_text = resolved_markdown.read_text(encoding="utf-8", errors="ignore")
        if not markdown_text.strip():
            raise ValueError(f"Markdown file is empty: {resolved_markdown}")

        effective_min = int(min_split_length or settings.split_min_length or DEFAULT_MIN_SPLIT_LENGTH)
        effective_max = int(max_split_length or settings.split_max_length or DEFAULT_MAX_SPLIT_LENGTH)
        if effective_min <= 0 or effective_max <= 0:
            raise ValueError("Split lengths must be positive")
        if effective_min > effective_max:
            raise ValueError("split_min_length cannot be greater than split_max_length")

        split_result = split_markdown_document(
            markdown_text,
            min_split_length=effective_min,
            max_split_length=effective_max,
        )
        updates = {
            "splitStrategy": "document-structure",
            "splitMinimumLength": effective_min,
            "splitMaximumLength": effective_max,
            "splitOutline": split_result["outline"],
            "splitSections": split_result["sections"],
            "splitChunks": split_result["chunks"],
            "splitSectionCount": split_result["sectionCount"],
            "splitChunkCount": split_result["chunkCount"],
            "splitSourceMarkdownPath": str(resolved_markdown),
            "splitUpdatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._log(
            f"Markdown 文本切分完成：record_id={normalized_id}, "
            f"sections={split_result['sectionCount']}, chunks={split_result['chunkCount']}, "
            f"min={effective_min}, max={effective_max}"
        )
        return self.update_saved_paper(normalized_id, updates)

    def resolve_pdf_path(self, pdf_path: str | Path) -> Path | None:
        """解析用于打开的 PDF 路径，兼容手动绑定的绝对路径。"""
        if not pdf_path:
            self._log("解析 PDF 路径：输入为空")
            return None

        raw_path = str(pdf_path).strip()
        if not raw_path:
            self._log("解析 PDF 路径：去空白后为空")
            return None

        try:
            candidate = Path(raw_path).expanduser().resolve()
        except Exception as error:
            self._log(f"解析 PDF 路径失败：raw_path={raw_path!r}, error={error}")
            return None

        if not candidate.exists():
            self._log(f"解析 PDF 路径失败：文件不存在 {candidate}")
            return None
        if not candidate.is_file():
            self._log(f"解析 PDF 路径失败：不是文件 {candidate}")
            return None
        if candidate.suffix.lower() != ".pdf":
            self._log(f"解析 PDF 路径失败：不是 PDF 文件 {candidate}")
            return None

        try:
            candidate.relative_to(self.download_dir.resolve())
            self._log(f"解析 PDF 路径成功：命中托管目录 PDF {candidate}")
        except Exception:
            self._log(f"解析 PDF 路径成功：命中手动绑定 PDF {candidate}")

        return candidate

    def find_local_pdf_for_paper(self, paper: Paper) -> Path | None:
        """优先用记录中的 pdfPath，失效时在 storage/papers 下回查文件。"""
        record_id = str(paper.get("id", "")).strip()
        stored_pdf_path = str(paper.get("pdfPath", "")).strip()
        self._log(
            f"开始查找本地 PDF：record_id={record_id or '<missing>'}, "
            f"stored_pdf_path={stored_pdf_path or '<empty>'}"
        )

        saved_path = self.resolve_pdf_path(stored_pdf_path)
        if saved_path:
            return saved_path

        normalized_record_id = record_id.lower()
        title_token = self._normalize_title(str(paper.get("title", "")))
        external_id = str(paper.get("externalId") or paper.get("external_id") or "").strip().lower()
        doi = self._normalize_identifier(str(paper.get("doi", "")))

        best_match: Path | None = None
        best_score = -1
        for candidate in self.download_dir.rglob("*.pdf"):
            stem = candidate.stem.lower()
            score = 0
            if normalized_record_id and normalized_record_id in stem:
                score += 4
            if external_id and external_id in stem:
                score += 3
            if doi and doi.replace("/", "_") in stem:
                score += 3
            normalized_stem = self._normalize_title(stem)
            if title_token and normalized_stem and (title_token in normalized_stem or normalized_stem in title_token):
                score += 2
            if score > best_score:
                best_score = score
                best_match = candidate

        if best_match and best_score > 0:
            resolved_match = best_match.resolve()
            self._log(
                f"查找本地 PDF：通过托管目录回退命中 record_id={record_id or '<missing>'}, "
                f"path={resolved_match}, score={best_score}"
            )
            return resolved_match
        self._log(
            f"查找本地 PDF：未命中任何文件 record_id={record_id or '<missing>'}, "
            f"title_token={title_token or '<empty>'}, external_id={external_id or '<empty>'}, doi={doi or '<empty>'}"
        )
        return None

    def import_paper(
        self,
        *,
        raw_text: str = "",
        title: str = "",
        authors: list[str] | None = None,
        abstract: str = "",
        year: str = "",
        doi: str = "",
        url: str = "",
        pdf_url: str = "",
        custom_tags: list[str] | None = None,
    ) -> Paper:
        """导入一条用户提供的文献记录，并尽量从原始题录文本自动解析字段。"""
        parsed = self._parse_imported_reference(raw_text)
        clean_tags = [tag.strip() for tag in (custom_tags or []) if tag.strip()]
        clean_authors = [author.strip() for author in (authors or []) if author.strip()]
        manual_override_fields = self._collect_manual_override_fields(
            title=title,
            authors=clean_authors,
            abstract=abstract,
            year=year,
            doi=doi,
            url=url,
        )
        imported_paper: Paper = {
            "source": "manual",
            "title": title.strip() or str(parsed.get("title", "")),
            "authors": clean_authors or parsed.get("authors", []),
            "abstract": abstract.strip() or str(parsed.get("abstract", "")),
            "year": year.strip() or str(parsed.get("year", "")),
            "doi": doi.strip() or str(parsed.get("doi", "")),
            "url": url.strip() or str(parsed.get("url", "")),
            "pdfUrl": pdf_url.strip(),
            "keyword": clean_tags[0] if clean_tags else "手动导入",
            "customTags": clean_tags,
            "rawImportText": raw_text.strip(),
            "importedManually": True,
            "manualOverrideFields": manual_override_fields,
            "relevanceScore": 0,
            "savedAt": datetime.now(timezone.utc).isoformat(),
        }

        if not str(imported_paper.get("title", "")).strip():
            raise ValueError("导入文献需要标题，或提供可解析出标题的题录文本")

        enriched_paper = enrich_paper_metrics(
            imported_paper,
            ccf_catalog=self.ccf_catalog,
            sjr_metrics=self.sjr_metrics,
        )
        saved_paper = self._save_paper_to_db(enriched_paper)
        self._log(f"手动导入论文元数据: {saved_paper.get('id')}")
        return saved_paper

    def import_pdf_paper(
        self,
        *,
        pdf_bytes: bytes,
        filename: str,
        title: str = "",
        authors: list[str] | None = None,
        abstract: str = "",
        year: str = "",
        doi: str = "",
        url: str = "",
        custom_tags: list[str] | None = None,
    ) -> Paper:
        """导入 PDF 文件，优先用 PyMuPDF 解析，必要时尝试 MinerU 精细解析。"""
        if not pdf_bytes:
            raise ValueError("PDF 文件内容为空")
        if not filename.lower().endswith(".pdf"):
            raise ValueError("只能导入 PDF 文件")

        import_dir = self.download_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name).strip("._") or "paper.pdf"
        pdf_path = import_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{safe_name}"
        pdf_path.write_bytes(pdf_bytes)

        extracted = self._extract_pdf_text(pdf_path)
        parsed = self._parse_pdf_metadata(extracted["text"], extracted["metadata"])
        if extracted["parser"] == "pymupdf" and self._parsed_pdf_metadata_is_sparse(parsed):
            self._log("PyMuPDF 解析的元数据较少，尝试使用 MinerU 精细解析")
            mineru_text = self._extract_pdf_text_with_mineru(pdf_path)
            if mineru_text:
                parsed = self._parse_pdf_metadata(mineru_text, extracted["metadata"])
                extracted = {
                    **extracted,
                    "text": mineru_text,
                    "parser": "mineru",
                    "warning": "PyMuPDF 解析的元数据较少，尝试使用 MinerU 精细解析",
                }
        clean_tags = [tag.strip() for tag in (custom_tags or []) if tag.strip()]
        clean_authors = [author.strip() for author in (authors or []) if author.strip()]
        manual_override_fields = self._collect_manual_override_fields(
            title=title,
            authors=clean_authors,
            abstract=abstract,
            year=year,
            doi=doi,
            url=url,
        )

        imported_paper: Paper = {
            "source": "manual_pdf",
            "title": title.strip() or str(parsed.get("title", "")),
            "authors": clean_authors or parsed.get("authors", []),
            "abstract": abstract.strip() or str(parsed.get("abstract", "")),
            "year": year.strip() or str(parsed.get("year", "")),
            "doi": doi.strip() or str(parsed.get("doi", "")),
            "venue": str(parsed.get("venue", "")),
            "journal": str(parsed.get("journal", "")),
            "containerTitle": str(parsed.get("containerTitle", "")),
            "url": url.strip(),
            "pdfPath": str(pdf_path),
            "keyword": clean_tags[0] if clean_tags else "PDF导入",
            "customTags": clean_tags,
            "importedManually": True,
            "manualOverrideFields": manual_override_fields,
            "pdfParsedBy": extracted["parser"],
            "pdfParseWarning": extracted["warning"],
            "pdfTextPreview": extracted["text"][:3000],
            "relevanceScore": 0,
            "savedAt": datetime.now(timezone.utc).isoformat(),
        }

        if not str(imported_paper.get("title", "")).strip():
            imported_paper["title"] = Path(filename).stem

        enriched_paper = enrich_paper_metrics(
            imported_paper,
            ccf_catalog=self.ccf_catalog,
            sjr_metrics=self.sjr_metrics,
        )
        saved_paper = self._save_paper_to_db(enriched_paper)
        self._log(f"PDF 导入论文元数据: {saved_paper.get('id')} parser={extracted['parser']}")
        return saved_paper

    def _extract_pdf_text(self, pdf_path: Path) -> dict[str, object]:
        """用 PyMuPDF 快速提取 PDF 文本；文本不足时尝试 MinerU。"""
        metadata: dict[str, object] = {}
        text = ""
        warning = ""

        try:
            import fitz  # type: ignore[import-not-found]

            with fitz.open(pdf_path) as document:
                metadata = dict(document.metadata or {})
                pages_text = [page.get_text("text") for page in document]
                text = "\n".join(part.strip() for part in pages_text if part.strip())
        except ImportError as error:
            warning = "PyMuPDF 未安装，无法快速解析 PDF"
            self._log(warning)
        except Exception as error:
            warning = f"PyMuPDF 解析失败: {error}"
            self._log(warning)

        if self._pdf_text_looks_usable(text):
            self._log(f"PDF 元数据={metadata}，文本长度={len(text)}")
            return {"text": text, "metadata": metadata, "parser": "pymupdf", "warning": warning}

        mineru_text = self._extract_pdf_text_with_mineru(pdf_path)
        if mineru_text:
            combined_warning = warning or "PyMuPDF 提取文本较少，已使用 MinerU 精细解析"
            self._log(f"PDF 元数据={metadata}，MinerU 文本长度={len(mineru_text)}")
            return {"text": mineru_text, "metadata": metadata, "parser": "mineru", "warning": combined_warning}

        fallback_warning = warning or "PyMuPDF 提取文本较少，且 MinerU 不可用"
        return {"text": text, "metadata": metadata, "parser": "pymupdf", "warning": fallback_warning}

    def _pdf_text_looks_usable(self, text: str) -> bool:
        """判断 PDF 提取文本是否达到可用质量。"""
        compact = re.sub(r"\s+", "", text)
        return len(compact) >= 800

    def _parsed_pdf_metadata_is_sparse(self, parsed: Paper) -> bool:
        """判断 PDF 解析得到的元数据是否过少。"""
        filled_count = 0
        for value in (
            str(parsed.get("title", "")).strip(),
            str(parsed.get("abstract", "")).strip(),
            str(parsed.get("year", "")).strip(),
            str(parsed.get("venue", "")).strip(),
            str(parsed.get("journal", "")).strip(),
        ):
            if value:
                self._log(f"PDF 元数据字段已填充: {value[:80]}")
                filled_count += 1

        authors = parsed.get("authors", [])
        if isinstance(authors, list) and any(str(author).strip() for author in authors):
            self._log(f"PDF 元数据字段已填充: authors={authors}")
            filled_count += 1
        self._log(f"PDF 元数据解析字段填充数: {filled_count}/6")
        return filled_count < 3

    def _extract_pdf_text_with_mineru(self, pdf_path: Path) -> str:
        """如果本机安装了 MinerU/magic-pdf CLI，则调用它生成 markdown/text 后读取。"""
        command = shutil.which("mineru") or shutil.which("magic-pdf")
        if not command:
            self._log("MinerU 未安装，跳过精细 PDF 解析")
            return ""

        with tempfile.TemporaryDirectory(prefix="mineru_") as output_dir:
            candidates = [
                [command, "-p", str(pdf_path), "-o", output_dir],
                [command, "-i", str(pdf_path), "-o", output_dir],
                [command, str(pdf_path), "-o", output_dir],
            ]
            for args in candidates:
                try:
                    subprocess.run(args, check=True, capture_output=True, text=True, timeout=120)
                    output_path = Path(output_dir)
                    text_parts = [
                        path.read_text(encoding="utf-8", errors="ignore")
                        for path in output_path.rglob("*")
                        if path.suffix.lower() in {".md", ".txt"} and path.is_file()
                    ]
                    extracted = "\n".join(part.strip() for part in text_parts if part.strip())
                    if extracted:
                        return extracted
                except Exception as error:
                    self._log(f"MinerU 命令尝试失败 ({' '.join(args)}): {error}")

        return ""

    def _parse_pdf_metadata(self, text: str, metadata: dict[str, object]) -> Paper:
        """从 PDF 文本和元数据中提取论文常见字段。"""
        parsed = self._parse_imported_reference(text)
        meta_title = str(metadata.get("title") or "").strip()
        meta_author = str(metadata.get("author") or "").strip()
        meta_subject = str(metadata.get("subject") or "").strip()
        creation_date = str(metadata.get("creationDate") or "")

        if meta_title and not self._metadata_value_is_noise(meta_title):
            parsed["title"] = meta_title
        if meta_author:
            parsed["authors"] = [
                author.strip()
                for author in re.split(r"\s*(?:,|;|\band\b|，|、)\s*", meta_author)
                if author.strip()
            ]
        if meta_subject and not parsed.get("abstract"):
            parsed["abstract"] = meta_subject

        date_year = re.search(r"(19|20)\d{2}", creation_date)
        if date_year and not parsed.get("year"):
            parsed["year"] = date_year.group(0)

        abstract_match = re.search(
            r"\babstract\b\s*[:.\-]?\s*(.+?)(?:\n\s*(?:keywords|introduction|1\s+introduction)\b|$)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if abstract_match:
            parsed["abstract"] = re.sub(r"\s+", " ", abstract_match.group(1)).strip()[:4000]

        if not parsed.get("title"):
            for line in text.splitlines()[:30]:
                candidate = line.strip()
                if 12 <= len(candidate) <= 220 and not re.match(r"^(abstract|keywords|doi)\b", candidate, re.I):
                    parsed["title"] = candidate
                    break

        venue = self._extract_venue_candidate(text)
        if venue:
            parsed["venue"] = venue
            parsed["containerTitle"] = venue
            if re.search(r"\bjournal\b|transactions|letters|review\b", venue, re.IGNORECASE):
                parsed["journal"] = venue

        return parsed

    def _resolve_markdown_for_metadata_refresh(
        self,
        paper: Paper,
        markdown_path: str | Path | None,
    ) -> Path:
        """解析Markdown、元数据。"""
        if markdown_path:
            resolved = self._resolve_managed_markdown_path(markdown_path)
        else:
            resolved = self._resolve_managed_markdown_path(
                str(paper.get("markdownPath") or paper.get("markdown_path") or "").strip()
            )

        if not resolved or not resolved.exists() or not resolved.is_file():
            raise ValueError("Managed markdown file not found for this paper")
        return resolved

    def _parse_markdown_metadata(self, markdown_text: str, paper: Paper) -> Paper:
        """解析Markdown、元数据。"""
        cleaned_text = self._strip_markdown_for_metadata(markdown_text)
        parsed = self._parse_imported_reference(cleaned_text)

        abstract_match = re.search(
            r"\babstract\b\s*[:.\-]?\s*(.+?)(?:\n\s*(?:keywords|introduction|1\s+introduction)\b|$)",
            cleaned_text,
            re.IGNORECASE | re.DOTALL,
        )
        if abstract_match:
            parsed["abstract"] = re.sub(r"\s+", " ", abstract_match.group(1)).strip()[:4000]

        if not parsed.get("title"):
            for line in cleaned_text.splitlines()[:20]:
                candidate = line.strip()
                if 12 <= len(candidate) <= 220 and not re.match(r"^(abstract|keywords|doi)\b", candidate, re.I):
                    parsed["title"] = candidate
                    break

        venue = self._extract_venue_candidate(cleaned_text)
        if venue:
            parsed["venue"] = venue
            parsed["containerTitle"] = venue
            if re.search(r"\bjournal\b|transactions|letters|review\b", venue, re.IGNORECASE):
                parsed["journal"] = venue

        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", cleaned_text, re.IGNORECASE)
        if doi_match:
            parsed["doi"] = doi_match.group(0).rstrip(".,;")

        year = self._infer_year_from_text(cleaned_text)
        if year:
            parsed["year"] = year

        parsed["pdfTextPreview"] = cleaned_text[:3000]
        parsed["metadataSource"] = "markdown"
        parsed["metadataSourcePath"] = str(
            paper.get("markdownPath") or paper.get("markdown_path") or ""
        ).strip()
        return parsed

    def _build_metadata_refresh_updates(
        self,
        current: Paper,
        parsed: Paper,
        markdown_path: Path,
    ) -> dict[str, object]:
        """构建元数据。"""
        updates: dict[str, object] = {}
        manual_override_fields = {
            str(field).strip()
            for field in current.get("manualOverrideFields", [])
            if str(field).strip()
        }
        comparable_fields = (
            "title",
            "abstract",
            "year",
            "venue",
            "journal",
            "containerTitle",
            "doi",
            "pdfTextPreview",
        )

        for field in comparable_fields:
            new_value = str(parsed.get(field, "")).strip()
            old_value = str(current.get(field, "")).strip()
            if (
                new_value
                and new_value != old_value
                and self._should_overwrite_from_markdown(field, old_value, manual_override_fields)
            ):
                updates[field] = new_value

        parsed_authors = [
            str(author).strip()
            for author in parsed.get("authors", [])
            if str(author).strip()
        ]
        current_authors = [
            str(author).strip()
            for author in current.get("authors", [])
            if str(author).strip()
        ]
        if (
            parsed_authors
            and parsed_authors != current_authors
            and self._should_overwrite_from_markdown("authors", current_authors, manual_override_fields)
        ):
            updates["authors"] = parsed_authors

        if str(current.get("pdfParsedBy", "")).strip() != "markdown":
            updates["pdfParsedBy"] = "markdown"
        if str(current.get("pdfParseWarning", "")).strip():
            updates["pdfParseWarning"] = ""

        updates["markdownMetadataUpdatedAt"] = datetime.now(timezone.utc).isoformat()
        updates["markdownMetadataSource"] = str(markdown_path)
        return updates

    def _collect_manual_override_fields(self, **field_values: object) -> list[str]:
        """收集用户明确填写、应优先保留的元数据字段。"""
        manual_fields: list[str] = []
        for field, value in field_values.items():
            if isinstance(value, list):
                if any(str(item).strip() for item in value):
                    manual_fields.append(field)
                continue

            if str(value or "").strip():
                manual_fields.append(field)

        return manual_fields

    def _should_overwrite_from_markdown(
        self,
        field: str,
        current_value: object,
        manual_override_fields: set[str],
    ) -> bool:
        """判断 Markdown 元数据是否应覆盖当前字段。"""
        if field not in manual_override_fields:
            return True

        if isinstance(current_value, list):
            return not any(str(item).strip() for item in current_value)

        return not str(current_value or "").strip()

    def _strip_markdown_for_metadata(self, markdown_text: str) -> str:
        """清理Markdown、元数据。"""
        text = markdown_text.replace("\r\n", "\n")
        text = re.sub(r"```.*?```", "\n", text, flags=re.DOTALL)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"\|", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _infer_year_from_text(self, text: str) -> str:
        """推断文本。"""
        years: list[int] = []
        max_year = datetime.now(timezone.utc).year + 1
        for match in re.finditer(r"\b(19|20)\d{2}\b", text[:4000]):
            year = int(match.group(0))
            if 1900 <= year <= max_year:
                years.append(year)

        if not years:
            return ""

        return str(min(years))

    def _metadata_value_is_noise(self, value: str) -> bool:
        """判断 PDF 元数据中的标题或作者是否是无意义的占位符。"""
        lowered = value.strip().lower()
        return lowered in {"untitled", "unknown", "microsoft word", "pdf", "document"} or len(lowered) < 6

    def _extract_venue_candidate(self, text: str) -> str:
        """从 PDF 前部文本中粗提取会议或期刊名，用于 CCF/SJR 指标匹配。"""
        head_text = "\n".join(text.splitlines()[:120])
        venue_patterns = [
            r"(?:proceedings of|in proceedings of)\s+(.{8,160})",
            r"((?:ACM|IEEE|USENIX|AAAI|ACL|EMNLP|NeurIPS|ICML|ICLR|CVPR|ICCV|ECCV|SIGIR|KDD|WWW|CHI|SIGMOD|VLDB|ICSE|FSE|ASE|PLDI|POPL|SOSP|OSDI|NDSS|CCS|S\&P)[^\n]{0,140})",
            r"((?:Journal|Transactions|Letters|Review) of [^\n]{6,140})",
            r"((?:IEEE|ACM) Transactions on [^\n]{6,140})",
        ]

        for pattern in venue_patterns:
            match = re.search(pattern, head_text, re.IGNORECASE)
            if match:
                candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;")
                if 6 <= len(candidate) <= 180:
                    return candidate
        return ""

    def _delete_local_pdf_if_managed(self, pdf_path: str) -> bool:
        """删除PDF。"""
        resolved_path = self._resolve_managed_pdf_path(pdf_path)
        if not resolved_path or not resolved_path.exists() or not resolved_path.is_file():
            return False

        resolved_path.unlink()
        parent = resolved_path.parent
        if parent != self.download_dir and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        return True

    def _delete_markdown_output_if_managed(self, paper: Paper) -> bool:
        """删除Markdown、输出结果。"""
        output_dir = self._resolve_managed_markdown_output_dir(paper)
        if output_dir and output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=False)
            parent = output_dir.parent
            markdown_root = Path(settings.mineru_output_dir).resolve()
            if parent != markdown_root and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
            return True

        markdown_path = self._resolve_managed_markdown_path(
            str(paper.get("markdownPath") or paper.get("markdown_path") or "").strip()
        )
        if markdown_path and markdown_path.exists() and markdown_path.is_file():
            markdown_path.unlink()
            return True

        return False

    def _resolve_managed_pdf_path(self, pdf_path: str | Path) -> Path | None:
        """ 如果 pdf_path 在 download_dir 下，则返回绝对路径，否则返回 None。"""
        if not pdf_path:
            return None

        try:
            candidate = Path(pdf_path).expanduser().resolve()
            download_root = self.download_dir.resolve()
            candidate.relative_to(download_root)
            self._log(f"解析本地 PDF 路径: {pdf_path} -> {candidate}")
            return candidate
        except Exception:
            self._log(f"无法解析本地 PDF 路径: {pdf_path}")
            return None

    def _resolve_managed_markdown_output_dir(self, paper: Paper) -> Path | None:
        """解析Markdown、输出结果。"""
        output_dir = str(
            paper.get("markdownOutputDir")
            or paper.get("outputDir")
            or paper.get("markdown_output_dir")
            or ""
        ).strip()
        if output_dir:
            resolved_dir = self._resolve_managed_markdown_path(output_dir, expect_dir=True)
            if resolved_dir:
                return resolved_dir

        markdown_path = str(paper.get("markdownPath") or paper.get("markdown_path") or "").strip()
        resolved_markdown = self._resolve_managed_markdown_path(markdown_path)
        if resolved_markdown:
            return resolved_markdown.parent

        return None

    def _resolve_managed_markdown_path(self, target_path: str | Path, *, expect_dir: bool = False) -> Path | None:
        """解析Markdown、路径。"""
        if not target_path:
            return None

        try:
            candidate = Path(target_path).expanduser().resolve()
            markdown_root = Path(settings.mineru_output_dir).resolve()
            candidate.relative_to(markdown_root)
            if expect_dir and not candidate.is_dir():
                return None
            self._log(f"解析 markdown 路径: {target_path} -> {candidate}")
            return candidate
        except Exception:
            self._log(f"无法解析 markdown 路径: {target_path}")
            return None

    def _parse_imported_reference(self, raw_text: str) -> Paper:
        """从粘贴的题录文本中用轻量规则提取常见字段。"""
        text = raw_text.strip()
        if not text:
            return {}

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        joined = " ".join(lines)
        parsed: Paper = {}

        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", joined, re.IGNORECASE)
        if doi_match:
            parsed["doi"] = doi_match.group(0).rstrip(".,;")

        url_match = re.search(r"https?://\S+", joined)
        if url_match:
            parsed["url"] = url_match.group(0).rstrip(".,;)")

        abstract_match = re.search(r"\babstract\s*[:.-]\s*(.+)$", joined, re.IGNORECASE)
        if abstract_match:
            parsed["abstract"] = abstract_match.group(1).strip()

        year_match = re.search(r"\b(19|20)\d{2}\b", joined)
        if year_match:
            parsed["year"] = year_match.group(0)

        title_line = ""
        for line in lines:
            lowered = line.lower()
            if lowered.startswith(("doi", "http", "abstract", "keywords")):
                continue
            title_line = line
            break
        if title_line:
            parsed["title"] = title_line.strip(" .")

        if len(lines) > 1:
            author_line = lines[1]
            if not re.search(r"\babstract\b|\bdoi\b|https?://", author_line, re.IGNORECASE):
                parsed["authors"] = [
                    author.strip(" .")
                    for author in re.split(r"\s*(?:,|;|\band\b|，|、)\s*", author_line)
                    if author.strip(" .")
                ]

        return parsed

    def _find_existing_paper(self, paper: Paper) -> Paper | None:
        """根据候选论文的稳定 ID 查询数据库中是否已有记录。"""
        record_id = self._build_record_id(paper)
        return self._find_existing_paper_by_fields(record_id=record_id)

    def _find_existing_paper_by_fields(
        self,
        *,
        record_id: str | None = None,
        doi: str | None = None,
        title: str | None = None,
    ) -> Paper | None:
        """按 ID、DOI 或标题查找已保存论文记录。"""
        clauses: list[str] = []
        params: list[str] = []

        if record_id:
            clauses.append("id = ?")
            params.append(record_id)
        if doi:
            clauses.append("doi = ?")
            params.append(self._normalize_identifier(doi))
        if title:
            clauses.append("title = ?")
            params.append(title)

        if not clauses:
            return None

        query = (
            "SELECT id, source, title, doi, external_id, url, pdf_url, pdf_path, keyword, "
            "relevance_score, metadata_json, saved_at FROM papers WHERE "
            + " OR ".join(clauses)
            + " LIMIT 1"
        )

        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(query, params).fetchone()
            self._log(f"数据库行={row}，查询参数={params}")

        if not row:
            return None

        return self._row_to_paper(row)

    def _row_to_paper(self, row: sqlite3.Row) -> Paper:
        """把 papers 表行转换为前端可使用的论文对象。"""
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}

        return {
            **metadata,
            "id": row["id"],
            "source": row["source"],
            "title": row["title"],
            "doi": row["doi"],
            "externalId": row["external_id"],
            "url": row["url"],
            "pdfUrl": row["pdf_url"],
            "pdfPath": row["pdf_path"],
            "keyword": row["keyword"],
            "relevanceScore": row["relevance_score"],
            "savedAt": row["saved_at"],
        }

    def _build_duplicate_group_key(self, paper: Paper) -> str:
        """为论文生成跨来源去重分组键。"""
        pdf_path = self._normalize_path_for_compare(str(paper.get("pdfPath") or paper.get("pdf_path") or ""))
        if pdf_path:
            return f"pdf:{pdf_path}"

        markdown_path = self._normalize_path_for_compare(
            str(paper.get("markdownPath") or paper.get("markdown_path") or "")
        )
        if markdown_path:
            return f"markdown:{markdown_path}"

        doi = self._normalize_identifier(str(paper.get("doi", "")))
        if doi:
            return f"doi:{doi}"

        return ""

    def _select_canonical_paper(self, papers: list[Paper], *, preferred_id: str = "") -> Paper:
        """选择论文。"""
        preferred_id = preferred_id.strip()
        if preferred_id:
            for paper in papers:
                if str(paper.get("id", "")).strip() == preferred_id:
                    return paper

        def sort_key(paper: Paper) -> tuple[int, int, str, str]:
            """生成用于选择规范论文记录的排序键。"""
            return (
                self._paper_quality_score(paper),
                1 if str(paper.get("pdfParsedBy", "")).strip() == "markdown" else 0,
                self._paper_recency_marker(paper),
                str(paper.get("id", "")).strip(),
            )

        return max(papers, key=sort_key)

    def _merge_paper_group(self, canonical: Paper, duplicates: list[Paper]) -> Paper:
        """合并论文。"""
        merged = dict(canonical)
        merge_fields = (
            "title",
            "abstract",
            "year",
            "doi",
            "venue",
            "journal",
            "containerTitle",
            "url",
            "pdfUrl",
            "pdfPath",
            "markdownPath",
            "markdownOutputDir",
            "sourcePdfPath",
            "pdfParsedBy",
            "pdfParseWarning",
            "pdfTextPreview",
            "keyword",
            "markdownMetadataUpdatedAt",
            "markdownMetadataSource",
            "updatedAt",
        )

        for duplicate in duplicates:
            for field in merge_fields:
                if not str(merged.get(field, "")).strip() and str(duplicate.get(field, "")).strip():
                    merged[field] = duplicate.get(field)

            merged["authors"] = self._merge_string_lists(merged.get("authors", []), duplicate.get("authors", []))
            merged["customTags"] = self._merge_string_lists(
                merged.get("customTags", []),
                duplicate.get("customTags", []),
            )
            merged["manualOverrideFields"] = self._merge_string_lists(
                merged.get("manualOverrideFields", []),
                duplicate.get("manualOverrideFields", []),
            )

        merged["id"] = canonical.get("id", "")
        merged["updatedAt"] = datetime.now(timezone.utc).isoformat()
        return merged

    def _paper_quality_score(self, paper: Paper) -> int:
        """计算论文记录完整度和本地资源质量得分。"""
        score = 0
        for field in (
            "title",
            "abstract",
            "year",
            "doi",
            "venue",
            "journal",
            "containerTitle",
            "url",
            "pdfUrl",
            "pdfPath",
            "markdownPath",
        ):
            if str(paper.get(field, "")).strip():
                score += 1

        for field in ("authors", "customTags", "manualOverrideFields"):
            if any(str(item).strip() for item in paper.get(field, [])):
                score += 1

        if str(paper.get("pdfParsedBy", "")).strip() == "markdown":
            score += 2
        if str(paper.get("markdownMetadataUpdatedAt", "")).strip():
            score += 2

        return score

    def _paper_recency_marker(self, paper: Paper) -> str:
        """生成论文记录的新旧排序标记。"""
        return max(
            str(paper.get("markdownMetadataUpdatedAt", "")).strip(),
            str(paper.get("updatedAt", "")).strip(),
            str(paper.get("savedAt", "")).strip(),
        )

    def _merge_string_lists(self, left: object, right: object) -> list[str]:
        """合并多个字符串列表并保持去重顺序。"""
        values: list[str] = []
        for source in (left, right):
            if not isinstance(source, list):
                continue
            for item in source:
                cleaned = str(item).strip()
                if cleaned and cleaned not in values:
                    values.append(cleaned)
        return values

    def _normalize_path_for_compare(self, raw_path: str) -> str:
        """规范化路径。"""
        candidate = raw_path.strip()
        if not candidate:
            return ""

        try:
            return str(Path(candidate).expanduser().resolve()).lower()
        except Exception:
            return candidate.lower()

    def _paper_has_local_pdf(self, paper: Paper) -> bool:
        """判断数据库记录是否已经绑定了可用的本地 PDF。"""
        pdf_path = str(paper.get("pdfPath") or paper.get("pdf_path") or "")
        if not pdf_path:
            return False

        path = Path(pdf_path)
        return path.exists() and path.is_file() and path.suffix.lower() == ".pdf"

    def _init_metadata_db(self) -> None:
        """初始化论文元数据 SQLite 表。"""
        with sqlite3.connect(self.metadata_db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    doi TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    pdf_url TEXT NOT NULL DEFAULT '',
                    pdf_path TEXT NOT NULL DEFAULT '',
                    keyword TEXT NOT NULL DEFAULT '',
                    relevance_score REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL,
                    saved_at TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            connection.commit()

    def _download_pdf(self, pdf_url: str, paper: Paper) -> Path:
        """下载 PDF 文件到本地论文目录。"""
        request = Request(
            pdf_url,
            headers={
                "User-Agent": "research-assistant/0.1",
                "Accept": "application/pdf,*/*",
            },
        )

        try:
            with urlopen(request, timeout=settings.request_timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                pdf_bytes = response.read()
        except HTTPError as error:
            raise RuntimeError(f"PDF 下载失败，HTTP {error.code}: {error.reason}") from error
        except URLError as error:
            raise RuntimeError(f"PDF 下载失败: {error.reason}") from error

        if not pdf_bytes:
            raise RuntimeError("PDF 下载失败：响应内容为空")

        if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
            raise RuntimeError(f"PDF 下载失败：响应类型不是 PDF ({content_type})")

        filename = self._build_pdf_filename(pdf_url, paper)
        pdf_path = self.download_dir / filename
        pdf_path.write_bytes(pdf_bytes)
        return pdf_path

    def _build_dedupe_key(self, paper: Paper) -> str:
        """生成论文去重键，优先使用 DOI 和 arXiv ID。"""
        doi = self._normalize_identifier(str(paper.get("doi", "")))
        if doi:
            return f"doi:{doi}"

        external_id = str(paper.get("externalId") or paper.get("external_id") or "")
        arxiv_id = self._extract_arxiv_id(external_id)
        if arxiv_id:
            return f"arxiv:{arxiv_id}"

        title = self._normalize_title(str(paper.get("title", "")))
        if title:
            return f"title:{title}"

        fallback = json.dumps(self._json_safe_paper(paper), ensure_ascii=False, sort_keys=True)
        return f"hash:{hashlib.sha256(fallback.encode('utf-8')).hexdigest()}"

    def _build_record_id(self, paper: Paper) -> str:
        """根据去重键生成稳定记录 ID。"""
        dedupe_key = self._build_dedupe_key(paper)
        return hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16]

    def _build_pdf_filename(self, pdf_url: str, paper: Paper) -> str:
        """生成安全的 PDF 文件名。"""
        title = self._normalize_title(str(paper.get("title", "")))[:80]
        external_id = self._extract_arxiv_id(str(paper.get("externalId", "")))
        url_name = Path(urlparse(pdf_url).path).stem
        raw_name = external_id or title or url_name or self._build_record_id(paper)
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw_name).strip("._")
        return f"{safe_name or self._build_record_id(paper)}.pdf"

    def _json_safe_paper(self, paper: Paper) -> Paper:
        """把论文对象转换成 JSON 可写入的基础类型。"""
        return {
            str(key): self._json_safe_value(value)
            for key, value in paper.items()
        }

    def _json_safe_value(self, value: object) -> object:
        """把数据库值转换为可安全 JSON 序列化的类型。"""
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            return [self._json_safe_value(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): self._json_safe_value(item)
                for key, item in value.items()
            }
        return str(value)

    def _clear_preprint_metrics(self, paper: Paper) -> Paper:
        """清除不适用于预印本的期刊和会议指标。"""
        return {
            **paper,
            "impactFactor": None,
            "sjr": None,
            "metricSource": "",
            "ccfLevel": "",
            "ccfSource": "",
            "ccfMatchedName": "",
            "preprintSource": True,
            "metricFiltersIgnored": True,
        }

    def _extract_arxiv_id(self, value: str) -> str:
        """从 arXiv URL 或 ID 中提取稳定编号。"""
        match = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", value)
        if match:
            return match.group(1)

        return ""

    def _normalize_identifier(self, value: str) -> str:
        """规范化 DOI 等标识符。"""
        return value.strip().lower().replace("https://doi.org/", "").replace("doi:", "")

    def _normalize_title(self, value: str) -> str:
        """规范化标题，用于兜底去重。"""
        normalized = re.sub(r"\s+", " ", value.strip().lower())
        return re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", "", normalized)

    def _tokenize(self, value: str) -> list[str]:
        """把关键词拆成用于初筛的 token。"""
        return [
            token
            for token in re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", value.lower())
            if len(token) >= 2
        ]


    def _translate_tencent_cloud(self, text: str, timeout: int = 10) -> str | None:
        """调用腾讯云接口把中文检索词翻译为英文。"""
        secret_id = settings.tencent_translation_secret_id
        secret_key = settings.tencent_translation_secret_key
        if not secret_id or not secret_key:
            self._log("腾讯云翻译跳过：未配置 TENCENTCLOUD_SECRET_ID/TENCENTCLOUD_SECRET_KEY")
            return None

        host = "tmt.tencentcloudapi.com"
        action = "TextTranslate"
        version = "2018-03-21"
        service = "tmt"
        timestamp = int(time.time())
        date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        payload = json.dumps(
            {
                "SourceText": text,
                "Source": "zh",
                "Target": "en",
                "ProjectId": 0,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        canonical_headers = (
            "content-type:application/json; charset=utf-8\n"
            f"host:{host}\n"
            f"x-tc-action:{action.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                canonical_headers,
                signed_headers,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            ],
        )
        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ],
        )
        secret_date = hmac.new(
            f"TC3{secret_key}".encode("utf-8"),
            date.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        secret_service = hmac.new(secret_date, service.encode("utf-8"), hashlib.sha256).digest()
        secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
        signature = hmac.new(
            secret_signing,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "TC3-HMAC-SHA256 "
            f"Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = Request(
            f"https://{host}",
            data=payload.encode("utf-8"),
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json; charset=utf-8",
                "Host": host,
                "X-TC-Action": action,
                "X-TC-Version": version,
                "X-TC-Timestamp": str(timestamp),
                "X-TC-Region": settings.tencent_translation_region,
            },
        )

        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            self._log(f"腾讯云翻译失败: {error}")
            return None

        response_data = data.get("Response", {})
        error_data = response_data.get("Error")
        if error_data:
            self._log(f"腾讯云翻译失败: {error_data.get('Code', '')} {error_data.get('Message', '')}")
            return None

        translated = str(response_data.get("TargetText", "")).strip()
        if not translated:
            self._log(f"腾讯云翻译失败：响应格式异常 {data}")
            return None

        self._log(f"腾讯云翻译结果: {text} -> {translated}")
        return translated

    # 在线翻译方案
    def _translate_online(self, text: str,timeout: int = 10) -> str | None:
        """调用在线翻译服务转换检索词。"""
        try:
            if self.translator is None:
                os.environ.setdefault("translators_default_region", "CN")
                import translators as ts

                self.translator = ts
            result = self.translator.translate_text(text, from_language='zh', to_language='en',timeout=timeout)
        
            self._log(f"在线翻译结果: {text} -> {result}")
            return result
        except Exception as e:
            # 记录日志，返回 None
            self._log(f"在线翻译失败: {e}")
            return None
    
    def _translate_with_llm(self, text: str) -> str | None:
        """调用大模型翻译检索词。"""
        model = ModelConfigStore().build_model_payload()
        if not model:
            self._log("大模型翻译跳过：尚未配置可用模型")
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "Translate Chinese academic search keywords into concise English. "
                    "Return only the translated query, with no explanation."
                ),
            },
            {"role": "user", "content": text},
        ]

        try:
            translated = chat_completion(
                model,
                messages,
                temperature=0,
                timeout=settings.request_timeout,
            )
        except Exception as error:
            self._log(f"大模型翻译失败: {error}")
            return None

        translated = translated.strip("\"'` \n\r\t")
        if not translated:
            self._log("大模型翻译失败：返回内容为空")
            return None

        self._log(f"大模型翻译结果: {text} -> {translated}")
        return translated

    def _is_chinese(self, text: str) -> bool:
        """判断文本是否包含中文字符。"""
        return any('\u4e00' <= ch <= '\u9fff' for ch in text)

    def translate_search_query(self, query: str) -> str:
        """将中文研究问题转换为英文论文检索词；供 RAG 与编排器统一复用。"""
        normalized = str(query).strip()
        if not normalized:
            raise ValueError("检索问题不能为空")
        return self._expand_keyword(normalized)

    def _expand_keyword(self, keyword: str) -> str:
        """把研究关键词扩展为适合多来源检索的英文词组。"""
        normalized = keyword.strip().lower()

        # 1. 若不包含中文，直接返回原词
        if not self._is_chinese(normalized):
            return keyword

        # 2. 查询缓存
        if normalized in self.translation_cache:
            self._log(f"使用缓存翻译: {normalized} -> {self.translation_cache[normalized]}")
            return self.translation_cache[normalized]

        # 3. 尝试在线翻译
        translated = self._translate_tencent_cloud(normalized, timeout=settings.request_timeout)
        if translated:
            self.translation_cache[normalized] = translated
            self._log(f"腾讯云翻译成功: {normalized} -> {translated}")
            return translated

        # 4. 尝试大模型翻译
        translated = self._translate_with_llm(normalized)
        if translated:
            self.translation_cache[normalized] = translated
            self._log(f"大模型翻译成功: {normalized} -> {translated}")
            return translated
        
        # 5.使用在线翻译托底
        translated = self._translate_online(normalized, timeout=settings.request_timeout)
        if translated:
            self.translation_cache[normalized] = translated
            self._log(f"在线翻译成功: {normalized} -> {translated}")
            return translated

        # 5. 全部失败，返回原词
        self._log(f"翻译失败，使用原词: {normalized}")
        return keyword

    def _log(self, message: str) -> None:
        """输出 HunterAgent 状态到后端终端，同时返回给前端。"""
        self.logs.append(message)
        if self.log_callback:
            self.log_callback(message)
        print(f"[HunterAgent] {message}", flush=True)

    def _parse_year(self, value: object) -> int | None:
        """从年份或日期字符串中解析年份。"""
        match = re.search(r"(19|20)\d{2}", str(value or ""))
        if not match:
            return None
        return int(match.group(0))
