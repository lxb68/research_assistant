from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import hashlib
import json
import re
import sqlite3

from app.core.config import settings
from app.services.ccf_catalog import CcfCatalog
from app.services.paper_search import SUPPORTED_SOURCES
from app.services.providers.arxiv import ARXIV_API_URL
from app.services.providers.ieee import IEEE_API_URL
from app.services.sjr_metrics import SjrMetrics
from app.services.venue_metrics import enrich_paper_metrics


Paper = dict[str, object]
SearchTool = Callable[[str, int], list[dict]]
MAX_SEARCH_ROUNDS = 5


class HunterAgent:
    """论文搜索采集 Agent：搜索、去重、排序、下载 PDF，并保存元数据。"""

    keyword_aliases = {
        "大模型": "large language model",
        "大型语言模型": "large language model",
        "语言模型": "language model",
        "生成式人工智能": "generative artificial intelligence",
        "生成式ai": "generative ai",
    }

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
                    saved_papers.append({**existing_paper, "reusedFromDatabase": True})
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
        record["id"] = self._build_record_id(record)

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
        safe_paper: Paper = {}

        for key, value in paper.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe_paper[key] = value
            elif isinstance(value, list):
                safe_paper[key] = [str(item) for item in value]
            else:
                safe_paper[key] = str(value)

        return safe_paper

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

    def _expand_keyword(self, keyword: str) -> str:
        """把常见中文关键词扩展为英文检索词，提高英文论文源命中率。"""
        normalized = keyword.strip().lower()
        return self.keyword_aliases.get(normalized, keyword)

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
