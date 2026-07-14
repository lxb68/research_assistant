/* 展示本地论文数据集，并处理 PDF 导入、解析、分块与删除操作。 */

"use client";

import Link from "next/link";
import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { buildApiUrl } from "@/lib/api";
import {
  DEFAULT_MAXIMUM_SPLIT_LENGTH,
  DEFAULT_MINIMUM_SPLIT_LENGTH,
  WORKSPACE_SETTINGS_STORAGE_KEY,
} from "@/lib/constants";
import { SavedPaper } from "@/lib/papers";
import { readNdjsonStream } from "@/lib/stream";
import { splitDelimitedText, uniqueTrimmedValues } from "@/lib/text";

type PdfImportForm = {
  title: string;
  authors: string;
  year: string;
  abstract: string;
  doi: string;
  url: string;
  tags: string;
};

type ImportStreamEvent = {
  type: "log" | "error" | "result" | "done";
  message?: string;
  paper?: SavedPaper;
};

const emptyPdfImportForm: PdfImportForm = {
  title: "",
  authors: "",
  year: "",
  abstract: "",
  doi: "",
  url: "",
  tags: "",
};

function getSplitLengthsFromSettings() {
  // 浏览器存储异常或配置无效时使用后端一致的默认切分长度。
  if (typeof window === "undefined") {
    return {
      minimumLength: DEFAULT_MINIMUM_SPLIT_LENGTH,
      maximumSplitLength: DEFAULT_MAXIMUM_SPLIT_LENGTH,
    };
  }

  try {
    const raw = window.localStorage.getItem(WORKSPACE_SETTINGS_STORAGE_KEY);
    if (!raw) {
      return {
        minimumLength: DEFAULT_MINIMUM_SPLIT_LENGTH,
        maximumSplitLength: DEFAULT_MAXIMUM_SPLIT_LENGTH,
      };
    }

    const parsed = JSON.parse(raw) as Partial<{
      minimumLength: number;
      maximumSplitLength: number;
    }>;

    return {
      minimumLength: Number(parsed.minimumLength) || DEFAULT_MINIMUM_SPLIT_LENGTH,
      maximumSplitLength: Number(parsed.maximumSplitLength) || DEFAULT_MAXIMUM_SPLIT_LENGTH,
    };
  } catch {
    return {
      minimumLength: DEFAULT_MINIMUM_SPLIT_LENGTH,
      maximumSplitLength: DEFAULT_MAXIMUM_SPLIT_LENGTH,
    };
  }
}

export default function DatasetBrowserView() {
  const [papers, setPapers] = useState<SavedPaper[]>([]);
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [importOpen, setImportOpen] = useState(false);
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [pdfImportForm, setPdfImportForm] = useState<PdfImportForm>(emptyPdfImportForm);
  const [isLoading, setIsLoading] = useState(true);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isSplitting, setIsSplitting] = useState(false);
  const [isImporting, setIsImporting] = useState(false);
  const [importLogs, setImportLogs] = useState<string[]>([]);
  const [splitLogs, setSplitLogs] = useState<string[]>([]);
  const [error, setError] = useState("");

  async function loadPapers(keyword = "", options: { initial?: boolean } = {}) {
    if (!options.initial) {
      setIsLoading(true);
      setError("");
    }

    try {
      const url = buildApiUrl("/api/papers");
      url.searchParams.set("limit", "120");
      if (keyword.trim()) {
        url.searchParams.set("keyword", keyword.trim());
      }

      const response = await fetch(url);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "加载本地数据集失败");
      }

      const nextPapers: SavedPaper[] = payload.papers ?? [];
      setPapers(nextPapers);
      setSelectedIds((currentIds) => {
        const availableIds = new Set(nextPapers.map((paper) => paper.id).filter(Boolean));
        return new Set(Array.from(currentIds).filter((id) => availableIds.has(id)));
      });
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "加载本地数据集失败");
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    const run = async () => {
      await loadPapers("", { initial: true });
    };

    void run();
  }, []);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void loadPapers(query);
  }

  function handlePdfFileChange(event: ChangeEvent<HTMLInputElement>) {
    setPdfFile(event.target.files?.[0] ?? null);
  }

  function togglePaperSelection(paperId: string) {
    setSelectedIds((currentIds) => {
      const nextIds = new Set(currentIds);
      if (nextIds.has(paperId)) {
        nextIds.delete(paperId);
      } else {
        nextIds.add(paperId);
      }
      return nextIds;
    });
  }

  async function importPdfPaper(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pdfFile || isImporting) {
      return;
    }

    setIsImporting(true);
    setError("");
    setImportLogs([]);

    try {
      const formData = new FormData();
      formData.set("file", pdfFile);
      formData.set("title", pdfImportForm.title);
      formData.set("authors", splitDelimitedText(pdfImportForm.authors).join(", "));
      formData.set("abstract", pdfImportForm.abstract);
      formData.set("year", pdfImportForm.year);
      formData.set("doi", pdfImportForm.doi);
      formData.set("url", pdfImportForm.url);
      formData.set("custom_tags", splitDelimitedText(pdfImportForm.tags).join(", "));

      const response = await fetch(buildApiUrl("/api/papers/import-pdf/stream"), {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "导入 PDF 文献失败");
      }
      if (!response.body) {
        throw new Error("后端没有返回流式进度");
      }

      let importedPaper: SavedPaper | null = null;
      await readNdjsonStream<ImportStreamEvent>(response.body, (eventPayload) => {
        if (eventPayload.type === "log" && eventPayload.message) {
          setImportLogs((currentLogs) => [...currentLogs, eventPayload.message!]);
        }
        if (eventPayload.type === "error" && eventPayload.message) {
          throw new Error(eventPayload.message);
        }
        if (eventPayload.type === "result" && eventPayload.paper) {
          importedPaper = eventPayload.paper;
          setPapers((currentPapers) => [
            eventPayload.paper!,
            ...currentPapers.filter((paper) => paper.id !== eventPayload.paper!.id),
          ]);
        }
      });

      if (!importedPaper) {
        throw new Error("导入结束，但没有收到论文结果");
      }

      setPdfFile(null);
      setPdfImportForm(emptyPdfImportForm);
      setImportOpen(false);
    } catch (importError) {
      setError(importError instanceof Error ? importError.message : "导入 PDF 文献失败");
    } finally {
      setIsImporting(false);
    }
  }

  async function deleteSelectedPapers() {
    const ids = Array.from(selectedIds);
    if (ids.length === 0 || isDeleting) {
      return;
    }

    if (!window.confirm(`确定删除选中的 ${ids.length} 条记录吗？`)) {
      return;
    }

    setIsDeleting(true);
    setError("");

    try {
      const response = await fetch(buildApiUrl("/api/papers/delete"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ids }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "删除所选记录失败");
      }

      const deletedIds = new Set<string>(payload.deletedIds ?? ids);
      setPapers((currentPapers) => currentPapers.filter((paper) => !paper.id || !deletedIds.has(paper.id)));
      setSelectedIds(new Set());
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "删除所选记录失败");
    } finally {
      setIsDeleting(false);
    }
  }

  async function splitSelectedPapers() {
    const selectedPapers = papers.filter((paper) => paper.id && selectedIds.has(paper.id));
    if (selectedPapers.length === 0 || isSplitting) {
      return;
    }

    const splitSettings = getSplitLengthsFromSettings();
    setIsSplitting(true);
    setError("");
    setSplitLogs([`准备转换 ${selectedPapers.length} 篇论文为 Markdown...`]);

    let successCount = 0;
    let failedCount = 0;

    try {
      for (const [index, paper] of selectedPapers.entries()) {
        const paperId = paper.id;
        if (!paperId) {
          continue;
        }

        const title = paper.title?.trim() || paperId;
        setSplitLogs((currentLogs) => [...currentLogs, `[${index + 1}/${selectedPapers.length}] 开始处理：${title}`]);

        try {
          const response = await fetch(buildApiUrl("/api/mineru/process"), {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              record_id: paperId,
              output_name: paperId,
              split_min_length: splitSettings.minimumLength,
              split_max_length: splitSettings.maximumSplitLength,
            }),
          });

          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            throw new Error(payload.detail || "转换失败");
          }

          successCount += 1;
          if (payload.paper) {
            setPapers((currentPapers) =>
              currentPapers.map((currentPaper) =>
                currentPaper.id === paperId ? { ...currentPaper, ...payload.paper } : currentPaper,
              ),
            );
          }
          setSplitLogs((currentLogs) => [...currentLogs, `[${index + 1}/${selectedPapers.length}] 转换完成：${title}`]);
        } catch (splitError) {
          failedCount += 1;
          const message = splitError instanceof Error ? splitError.message : "转换失败";
          setSplitLogs((currentLogs) => [
            ...currentLogs,
            `[${index + 1}/${selectedPapers.length}] 转换失败：${title} - ${message}`,
          ]);
        }
      }

      await loadPapers(query);
      setSplitLogs((currentLogs) => [...currentLogs, `本轮完成：成功 ${successCount} 篇，失败 ${failedCount} 篇。`]);
    } catch (splitError) {
      setError(splitError instanceof Error ? splitError.message : "批量转换 Markdown 失败");
    } finally {
      setIsSplitting(false);
    }
  }

  return (
    <main className="dataset-browser-page">
      <section className="dataset-browser-panel">
        <form className="dataset-browser-toolbar" onSubmit={handleSubmit}>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按关键词或标题筛选"
          />
          <button type="submit" disabled={isLoading}>
            {isLoading ? "加载中..." : "筛选"}
          </button>
          <button type="button" onClick={() => setImportOpen((open) => !open)}>
            {importOpen ? "收起导入" : "导入 PDF"}
          </button>
          <button type="button" disabled={selectedIds.size === 0 || isSplitting} onClick={splitSelectedPapers}>
            {isSplitting ? "转换中..." : selectedIds.size > 0 ? `文本切分 (${selectedIds.size})` : "文本切分"}
          </button>
          <button
            type="button"
            className="dataset-browser-delete-button"
            disabled={selectedIds.size === 0 || isDeleting}
            onClick={deleteSelectedPapers}
          >
            {isDeleting ? "删除中..." : selectedIds.size > 0 ? `删除 (${selectedIds.size})` : "删除"}
          </button>
        </form>

        {importOpen && (
          <form className="dataset-import-panel" onSubmit={importPdfPaper}>
            <label className="dataset-import-wide">
              <span>PDF 文件</span>
              <input type="file" accept="application/pdf,.pdf" onChange={handlePdfFileChange} />
            </label>
            <label>
              <span>标题</span>
              <input
                value={pdfImportForm.title}
                onChange={(event) => setPdfImportForm((form) => ({ ...form, title: event.target.value }))}
                placeholder="留空则自动解析"
              />
            </label>
            <label>
              <span>作者</span>
              <input
                value={pdfImportForm.authors}
                onChange={(event) => setPdfImportForm((form) => ({ ...form, authors: event.target.value }))}
                placeholder="多个作者用逗号分隔"
              />
            </label>
            <label>
              <span>年份</span>
              <input
                value={pdfImportForm.year}
                onChange={(event) => setPdfImportForm((form) => ({ ...form, year: event.target.value }))}
                placeholder="留空则自动解析"
              />
            </label>
            <label>
              <span>自定义标签</span>
              <input
                value={pdfImportForm.tags}
                onChange={(event) => setPdfImportForm((form) => ({ ...form, tags: event.target.value }))}
                placeholder="例如：医学, CCF B"
              />
            </label>
            <label>
              <span>DOI</span>
              <input
                value={pdfImportForm.doi}
                onChange={(event) => setPdfImportForm((form) => ({ ...form, doi: event.target.value }))}
                placeholder="留空则自动解析"
              />
            </label>
            <label>
              <span>原文链接</span>
              <input
                value={pdfImportForm.url}
                onChange={(event) => setPdfImportForm((form) => ({ ...form, url: event.target.value }))}
              />
            </label>
            <label className="dataset-import-wide">
              <span>摘要</span>
              <textarea
                value={pdfImportForm.abstract}
                onChange={(event) => setPdfImportForm((form) => ({ ...form, abstract: event.target.value }))}
                placeholder="留空则尝试从 PDF 中解析"
              />
            </label>
            <div className="dataset-import-actions">
              <span>{pdfFile ? pdfFile.name : "后端会流式返回导入进度，复杂 PDF 会自动尝试 MinerU。"}</span>
              <button type="submit" disabled={!pdfFile || isImporting}>
                {isImporting ? "导入中..." : "保存导入"}
              </button>
            </div>
            {importLogs.length > 0 && (
              <div className="dataset-import-log">
                {importLogs.map((log, index) => (
                  <p key={`import-log-${index}`}>{log}</p>
                ))}
              </div>
            )}
          </form>
        )}

        {error && <div className="dataset-browser-error">{error}</div>}

        {splitLogs.length > 0 && (
          <div className="dataset-import-log">
            {splitLogs.map((log, index) => (
              <p key={`split-log-${index}`}>{log}</p>
            ))}
          </div>
        )}

        {!error && isLoading ? (
          <div className="dataset-browser-empty">
            <strong>正在加载数据集</strong>
            <span>正在读取本地论文元数据。</span>
          </div>
        ) : !error && papers.length === 0 ? (
          <div className="dataset-browser-empty">
            <strong>暂无已保存论文</strong>
            <span>可以先下载数据集，或者在这里导入 PDF 文献。</span>
          </div>
        ) : (
          <div className="dataset-card-grid">
            {papers.map((paper, index) => {
              const paperId = paper.id || "";
              const isSelected = paperId ? selectedIds.has(paperId) : false;
              const labels = uniqueTrimmedValues([paper.source, paper.year, paper.keyword, ...(paper.customTags ?? [])]);
              const viewUrl = paperId ? `/dataset-brower/view/${encodeURIComponent(paperId)}` : null;
              const externalUrl = paper.url?.trim() || "";

              return (
                <article
                  className={`dataset-card${isSelected ? " dataset-card-selected" : ""}`}
                  key={paper.id || `${paper.title}-${index}`}
                >
                  <div className="dataset-card-meta">
                    {paperId && (
                      <label className="dataset-card-select">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          disabled={isDeleting || isSplitting}
                          onChange={() => togglePaperSelection(paperId)}
                        />
                        <span>选择</span>
                      </label>
                    )}
                    {labels.map((label) => (
                      <span key={`${paperId}-${label}`}>{label}</span>
                    ))}
                    {!paper.metricFiltersIgnored && paper.ccfLevel && <span>CCF {paper.ccfLevel}</span>}
                    {!paper.metricFiltersIgnored &&
                      paper.impactFactor !== undefined &&
                      paper.impactFactor !== null && <span>IF {paper.impactFactor}</span>}
                    <span className={paper.pdfPath ? "dataset-pdf-ready" : "dataset-pdf-missing"}>
                      {paper.pdfPath ? "PDF 可用" : "缺少 PDF"}
                    </span>
                    {paper.markdownPath && <span className="dataset-markdown-ready">Markdown 已生成</span>}
                  </div>
                  <h2>{paper.title || "未命名论文"}</h2>
                  {paper.authors && paper.authors.length > 0 && (
                    <p className="dataset-card-authors">{paper.authors.slice(0, 4).join(", ")}</p>
                  )}
                  {paper.abstract && <p className="dataset-card-abstract">{paper.abstract}</p>}
                  {paper.pdfParseWarning && <p className="dataset-card-warning">{paper.pdfParseWarning}</p>}
                  <div className="dataset-card-actions">
                    {viewUrl && <Link href={viewUrl}>查看原文</Link>}
                    {!viewUrl && externalUrl && (
                      <a href={externalUrl} target="_blank" rel="noreferrer">
                        查看原文
                      </a>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>
    </main>
  );
}
