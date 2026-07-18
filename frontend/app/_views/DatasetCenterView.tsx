/* 统一承载文献获取与本地文献管理，并仅通过刷新信号协调两个独立组件。 */

"use client";

import { useState } from "react";
import DatasetBrowserView from "@/app/_views/DatasetBrowserView";
import DatasetDownloadView from "@/app/_views/DatasetDownloadView";

export type DatasetCenterTab = "download" | "library";

type DatasetCenterViewProps = {
  initialTab?: DatasetCenterTab;
  isActiveView?: boolean;
};

/** 管理数据集中心的页签和文献库刷新，不承载子组件的业务状态。 */
export default function DatasetCenterView({
  initialTab = "download",
  isActiveView = true,
}: DatasetCenterViewProps) {
  const [activeTab, setActiveTab] = useState<DatasetCenterTab>(initialTab);
  const [libraryRefreshToken, setLibraryRefreshToken] = useState(0);

  return (
    <main className="dataset-center-page">
      <nav className="dataset-center-tabs" aria-label="数据集中心">
        <button
          type="button"
          className={activeTab === "download" ? "is-active" : ""}
          onClick={() => setActiveTab("download")}
        >
          检索与下载
        </button>
        <button
          type="button"
          className={activeTab === "library" ? "is-active" : ""}
          onClick={() => setActiveTab("library")}
        >
          本地文献库
        </button>
      </nav>

      <section hidden={activeTab !== "download"} aria-label="检索与下载">
        <DatasetDownloadView
          embedded
          isActiveView={isActiveView && activeTab === "download"}
          onDatasetChanged={() => setLibraryRefreshToken((current) => current + 1)}
        />
      </section>
      <section hidden={activeTab !== "library"} aria-label="本地文献库">
        <DatasetBrowserView refreshToken={libraryRefreshToken} />
      </section>
    </main>
  );
}
