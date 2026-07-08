"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import PdfViewer from "@/components/PdfViewer";

type ParagraphSummary = {
  index?: number;
  summary?: string;
  charCount?: number;
};

type SplitChunk = {
  summary?: string;
  content?: string;
  charCount?: number;
  partIndex?: number;
  totalParts?: number;
  paragraphSummaries?: ParagraphSummary[];
};

type SavedPaper = {
  id?: string;
  title?: string;
  url?: string;
  pdfPath?: string;
  splitChunkCount?: number;
  splitSectionCount?: number;
  splitMinimumLength?: number;
  splitMaximumLength?: number;
  splitChunks?: SplitChunk[];
};

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4000";

export default function PaperViewerPage() {
  const params = useParams<{ recordId: string }>();
  const recordId = typeof params.recordId === "string" ? params.recordId : "";
  const hasRecordId = Boolean(recordId);
  const [paper, setPaper] = useState<SavedPaper | null>(null);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (!hasRecordId) {
      return;
    }

    const url = new URL(`/api/papers/${encodeURIComponent(recordId)}`, apiBaseUrl);
    fetch(url)
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || "加载论文信息失败");
        }
        setPaper(payload.paper ?? null);
      })
      .catch((loadError) => {
        setError(loadError instanceof Error ? loadError.message : "加载论文信息失败");
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, [hasRecordId, recordId]);

  const pdfViewerUrl = recordId
    ? new URL(`/api/papers/${encodeURIComponent(recordId)}/pdf`, apiBaseUrl).toString()
    : "";
  const externalUrl = paper?.url?.trim() || "";
  const splitChunks = paper?.splitChunks ?? [];

  return (
    <main className="paper-viewer-page">
      <header className="paper-viewer-header">
        <div>
          <p className="paper-viewer-kicker">Local PDF Viewer</p>
          <h1>{paper?.title || "论文查看器"}</h1>
        </div>
        <div className="paper-viewer-actions">
          <Link href="/?view=browse" className="paper-viewer-button">
            返回
          </Link>
          {externalUrl && (
            <a href={externalUrl} target="_blank" rel="noreferrer" className="paper-viewer-button">
              打开外部原文
            </a>
          )}
        </div>
      </header>

      {!hasRecordId ? (
        <section className="paper-viewer-panel paper-viewer-error">
          <p>缺少论文记录 ID。</p>
        </section>
      ) : isLoading ? (
        <section className="paper-viewer-panel">
          <p>正在加载论文内容...</p>
        </section>
      ) : error ? (
        <section className="paper-viewer-panel paper-viewer-error">
          <p>{error}</p>
        </section>
      ) : paper?.pdfPath ? (
        <>
          <section className="paper-viewer-frame-wrap">
            <PdfViewer title={paper.title || "论文 PDF"} url={pdfViewerUrl} />
          </section>

          <section className="paper-viewer-panel paper-viewer-split-panel">
            <div className="paper-viewer-split-header">
              <div>
                <p className="paper-viewer-kicker">Split Chunks</p>
                <h2>文本分割结果</h2>
              </div>
              <div className="paper-viewer-split-stats">
                <span>{paper?.splitSectionCount ?? 0} 个章节</span>
                <span>{paper?.splitChunkCount ?? 0} 个分块</span>
                <span>
                  {paper?.splitMinimumLength ?? "-"} / {paper?.splitMaximumLength ?? "-"} 字符
                </span>
              </div>
            </div>

            {splitChunks.length === 0 ? (
              <p className="paper-viewer-split-empty">当前还没有可展示的文本分割结果。</p>
            ) : (
              <div className="paper-viewer-split-list">
                {splitChunks.map((chunk, index) => (
                  <details
                    className="paper-viewer-split-card"
                    key={`${chunk.summary || "chunk"}-${index}`}
                    open={index === 0}
                  >
                    <summary className="paper-viewer-split-summary">
                      <div>
                        <strong>{chunk.summary || `Chunk ${index + 1}`}</strong>
                        <p>
                          {chunk.charCount ?? 0} 字符
                          {chunk.partIndex && chunk.totalParts
                            ? ` · Part ${chunk.partIndex}/${chunk.totalParts}`
                            : ""}
                        </p>
                      </div>
                    </summary>

                    {chunk.paragraphSummaries && chunk.paragraphSummaries.length > 0 && (
                      <div className="paper-viewer-paragraph-list">
                        {chunk.paragraphSummaries.map((paragraph, paragraphIndex) => (
                          <article
                            className="paper-viewer-paragraph-card"
                            key={`${index}-${paragraph.index || paragraphIndex}`}
                          >
                            <span className="paper-viewer-paragraph-index">
                              段落 {paragraph.index || paragraphIndex + 1}
                            </span>
                            <p>{paragraph.summary || "暂无摘要"}</p>
                            <small>{paragraph.charCount ?? 0} 字符</small>
                          </article>
                        ))}
                      </div>
                    )}

                    <pre className="paper-viewer-split-content">{chunk.content || ""}</pre>
                  </details>
                ))}
              </div>
            )}
          </section>
        </>
      ) : externalUrl ? (
        <section className="paper-viewer-panel">
          <p>这条记录没有本地 PDF，已为你保留外部原文入口。</p>
          <a href={externalUrl} target="_blank" rel="noreferrer" className="paper-viewer-button">
            打开外部原文
          </a>
        </section>
      ) : (
        <section className="paper-viewer-panel paper-viewer-error">
          <p>没有可显示的本地 PDF，也没有外部原文链接。</p>
        </section>
      )}
    </main>
  );
}
