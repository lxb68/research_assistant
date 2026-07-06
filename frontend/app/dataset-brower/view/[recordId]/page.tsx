"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import PdfViewer from "@/components/PdfViewer";

type SavedPaper = {
  id?: string;
  title?: string;
  url?: string;
  pdfPath?: string;
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

  const pdfViewerUrl = recordId ? new URL(`/api/papers/${encodeURIComponent(recordId)}/pdf`, apiBaseUrl).toString() : "";
  const externalUrl = paper?.url?.trim() || "";

  return (
    <main className="paper-viewer-page">
      <header className="paper-viewer-header">
        <div>
          <p className="paper-viewer-kicker">Local PDF Viewer</p>
          <h1>{paper?.title || "论文查看器"}</h1>
        </div>
        <div className="paper-viewer-actions">
          <Link href="/" className="paper-viewer-button">
            返回首页
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
        <section className="paper-viewer-frame-wrap">
          <PdfViewer title={paper.title || "论文 PDF"} url={pdfViewerUrl} />
        </section>
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
