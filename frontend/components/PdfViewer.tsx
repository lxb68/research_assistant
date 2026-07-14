/* 使用 PDF.js 在客户端按页渲染论文，并管理缩放、加载和错误状态。 */

"use client";

import { useEffect, useState } from "react";
import * as pdfjsLib from "pdfjs-dist/legacy/build/pdf.mjs";

type PdfDocumentProxy = Awaited<ReturnType<typeof pdfjsLib.getDocument>>["promise"] extends Promise<infer T> ? T : never;
type PdfRenderTask = {
  promise: Promise<void>;
  cancel: () => void;
};

type PdfViewerProps = {
  title: string;
  url: string;
};

type PdfPageCanvasProps = {
  pageNumber: number;
  pdf: PdfDocumentProxy;
  scale: number;
};

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.mjs", import.meta.url).toString();

function PdfPageCanvas({ pageNumber, pdf, scale }: PdfPageCanvasProps) {
  const [pageState, setPageState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    let cancelled = false;
    let activeRenderTask: PdfRenderTask | null = null;

    async function renderPage() {
      setPageState("loading");

      try {
        const page = await pdf.getPage(pageNumber);
        if (cancelled) {
          return;
        }

        const viewport = page.getViewport({ scale });
        const canvas = document.getElementById(`pdf-canvas-${pageNumber}`) as HTMLCanvasElement | null;
        if (!canvas) {
          return;
        }

        const context = canvas.getContext("2d");
        if (!context) {
          throw new Error("无法创建 Canvas 上下文");
        }

        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;

        activeRenderTask = page.render({ canvas, canvasContext: context, viewport });
        await activeRenderTask.promise;

        if (!cancelled) {
          setPageState("ready");
        }
      } catch (error) {
        if (!cancelled) {
          console.error(`第 ${pageNumber} 页渲染失败`, error);
          setPageState("error");
        }
      }
    }

    void renderPage();

    return () => {
      cancelled = true;
      activeRenderTask?.cancel();
    };
  }, [pageNumber, pdf, scale]);

  return (
    <div className="pdf-viewer-page-wrapper">
      {pageState !== "ready" && (
        <div className={`pdf-viewer-page-placeholder${pageState === "error" ? " pdf-viewer-page-error" : ""}`}>
          {pageState === "error" ? `第 ${pageNumber} 页渲染失败` : `正在加载第 ${pageNumber} 页...`}
        </div>
      )}
      <canvas id={`pdf-canvas-${pageNumber}`} />
    </div>
  );
}

export default function PdfViewer({ title, url }: PdfViewerProps) {
  const [pdf, setPdf] = useState<PdfDocumentProxy | null>(null);
  const [numPages, setNumPages] = useState(0);
  const [scale, setScale] = useState(1.1);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    const loadingTask = pdfjsLib.getDocument({ url });

    async function loadPdf() {
      setIsLoading(true);
      setError("");

      try {
        const documentProxy = await loadingTask.promise;
        if (cancelled) {
          return;
        }

        setPdf(documentProxy);
        setNumPages(documentProxy.numPages);
      } catch (loadError) {
        if (!cancelled) {
          console.error("PDF 加载失败", loadError);
          setError(loadError instanceof Error ? loadError.message : "PDF 加载失败");
          setPdf(null);
          setNumPages(0);
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    void loadPdf();

    return () => {
      cancelled = true;
      loadingTask.destroy();
    };
  }, [url]);

  if (isLoading) {
    return (
      <div className="pdf-viewer-message">
        <strong>正在加载 PDF</strong>
        <span>{title}</span>
      </div>
    );
  }

  if (error || !pdf) {
    return (
      <div className="pdf-viewer-error">
        <strong>PDF 渲染失败</strong>
        <span>{error || "未能初始化 PDF.js 查看器"}</span>
        <a href={url} target="_blank" rel="noreferrer">
          直接打开原始 PDF
        </a>
      </div>
    );
  }

  return (
    <div className="pdf-viewer-container">
      <div className="pdf-viewer-toolbar">
        <span className="pdf-viewer-info">
          {title} · 共 {numPages} 页
        </span>
        <div className="pdf-viewer-zoom">
          <button type="button" onClick={() => setScale((current) => Math.max(0.6, current - 0.1))} disabled={scale <= 0.6}>
            -
          </button>
          <span>{Math.round(scale * 100)}%</span>
          <button type="button" onClick={() => setScale((current) => Math.min(2.4, current + 0.1))} disabled={scale >= 2.4}>
            +
          </button>
        </div>
      </div>

      <div className="pdf-viewer-pages">
        {Array.from({ length: numPages }, (_, index) => (
          <PdfPageCanvas key={`${url}-${index + 1}-${scale}`} pageNumber={index + 1} pdf={pdf} scale={scale} />
        ))}
      </div>
    </div>
  );
}
