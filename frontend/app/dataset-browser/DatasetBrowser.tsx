"use client";

import { FormEvent, useEffect, useState } from "react";

type SavedPaper = {
  id?: string;
  source?: string;
  title?: string;
  authors?: string[];
  abstract?: string;
  year?: string;
  keyword?: string;
  url?: string;
  pdfUrl?: string;
  pdfPath?: string;
  savedAt?: string;
  ccfLevel?: string;
  impactFactor?: number | null;
  metricFiltersIgnored?: boolean;
};

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4000";

export default function DatasetBrowser() {
  const [papers, setPapers] = useState<SavedPaper[]>([]);
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [isDeleting, setIsDeleting] = useState(false);
  const [error, setError] = useState("");

  async function loadPapers(keyword = "") {
    setIsLoading(true);
    setError("");

    try {
      const url = new URL("/api/papers", apiBaseUrl);
      url.searchParams.set("limit", "120");
      if (keyword.trim()) {
        url.searchParams.set("keyword", keyword.trim());
      }

      const response = await fetch(url);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "加载本地数据集失败");
      }

      const nextPapers = payload.papers ?? [];
      setPapers(nextPapers);
      setSelectedIds((currentIds) => {
        const availableIds = new Set(nextPapers.map((paper: SavedPaper) => paper.id).filter(Boolean));
        return new Set(Array.from(currentIds).filter((id) => availableIds.has(id)));
      });
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "加载本地数据集失败");
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    const url = new URL("/api/papers", apiBaseUrl);
    url.searchParams.set("limit", "120");

    fetch(url)
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || "加载本地数据集失败");
        }
        setPapers(payload.papers ?? []);
      })
      .catch((loadError) => {
        setError(loadError instanceof Error ? loadError.message : "加载本地数据集失败");
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, []);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    loadPapers(query);
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

  async function deleteSelectedPapers() {
    const ids = Array.from(selectedIds);
    if (ids.length === 0 || isDeleting) {
      return;
    }

    const confirmed = window.confirm(`确定删除选中的 ${ids.length} 条记录吗？`);
    if (!confirmed) {
      return;
    }

    setIsDeleting(true);
    setError("");

    try {
      const response = await fetch(new URL("/api/papers/delete", apiBaseUrl), {
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
            {isLoading ? "加载中" : "筛选"}
          </button>
          <button
            type="button"
            className="dataset-browser-delete-button"
            disabled={selectedIds.size === 0 || isDeleting}
            onClick={deleteSelectedPapers}
          >
            {isDeleting ? "删除中" : selectedIds.size > 0 ? `删除 (${selectedIds.size})` : "删除"}
          </button>
        </form>

        {error && <div className="dataset-browser-error">{error}</div>}

        {!error && isLoading ? (
          <div className="dataset-browser-empty">
            <strong>正在加载数据集</strong>
            <span>正在读取本地论文元数据。</span>
          </div>
        ) : !error && papers.length === 0 ? (
          <div className="dataset-browser-empty">
            <strong>暂无已保存论文</strong>
            <span>先去下载数据集，完成后这里会出现可浏览的记录。</span>
          </div>
        ) : (
          <div className="dataset-card-grid">
            {papers.map((paper, index) => {
              const paperId = paper.id || "";
              const isSelected = paperId ? selectedIds.has(paperId) : false;

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
                          disabled={isDeleting}
                          onChange={() => togglePaperSelection(paperId)}
                        />
                        <span>选择</span>
                      </label>
                    )}
                    {paper.source && <span>{paper.source}</span>}
                    {paper.year && <span>{paper.year}</span>}
                    {paper.keyword && <span>{paper.keyword}</span>}
                    {!paper.metricFiltersIgnored && paper.ccfLevel && <span>CCF {paper.ccfLevel}</span>}
                    {!paper.metricFiltersIgnored &&
                      paper.impactFactor !== undefined &&
                      paper.impactFactor !== null && <span>IF {paper.impactFactor}</span>}
                    <span className={paper.pdfPath ? "dataset-pdf-ready" : "dataset-pdf-missing"}>
                      {paper.pdfPath ? "PDF 已下载" : "缺少 PDF"}
                    </span>
                  </div>
                  <h2>{paper.title || "未命名论文"}</h2>
                  {paper.authors && paper.authors.length > 0 && (
                    <p className="dataset-card-authors">{paper.authors.slice(0, 4).join(", ")}</p>
                  )}
                  {paper.abstract && <p className="dataset-card-abstract">{paper.abstract}</p>}
                  <div className="dataset-card-actions">
                    {paper.url && (
                      <a href={paper.url} target="_blank" rel="noreferrer">
                        查看原文
                      </a>
                    )}
                    {paper.pdfUrl && (
                      <a href={paper.pdfUrl} target="_blank" rel="noreferrer">
                        PDF 链接
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
