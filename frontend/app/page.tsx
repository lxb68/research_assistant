"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import DatasetBrowser from "@/app/dataset-brower/page";
import SettingsWorkspace from "@/app/setting/page";
import HeroSection from "@/home/HeroSection";
import DatasetDownloadPage from "./dataset-download/page";

type WorkspaceView = "home" | "download" | "browse" | "settings";

function parseWorkspaceView(value: string | null): WorkspaceView {
  if (value === "download" || value === "browse" || value === "settings") {
    return value;
  }
  return "home";
}

export default function Home() {
  const searchParams = useSearchParams();
  const [activeView, setActiveView] = useState<WorkspaceView>(() => parseWorkspaceView(searchParams.get("view")));
  const [createDialogOpen, setCreateDialogOpen] = useState(false);

  useEffect(() => {
    setActiveView(parseWorkspaceView(searchParams.get("view")));
  }, [searchParams]);

  const navItems = useMemo(
    () => [
      { id: "download" as const, label: "下载数据集" },
      { id: "browse" as const, label: "浏览数据集" },
      { id: "settings" as const, label: "设置" },
    ],
    [],
  );

  return (
    <div className="workspace-shell">
      {activeView !== "home" && (
        <header className="workspace-topbar">
          <button type="button" className="workspace-brand" onClick={() => setActiveView("home")}>
            <span className="workspace-logo">R</span>
            <span>Research Agent</span>
          </button>

          <nav className="workspace-tabs" aria-label="数据集工作台">
            {navItems.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`workspace-tab ${activeView === item.id ? "workspace-tab-active" : ""}`}
                onClick={() => setActiveView(item.id)}
              >
                {item.label}
              </button>
            ))}
          </nav>
        </header>
      )}

      <div className="workspace-view-stack">
        <div
          className={`workspace-view-panel ${
            activeView === "home" ? "workspace-view-panel-active" : "workspace-view-panel-hidden"
          }`}
        >
          <main className="home-page">
            <HeroSection
              onCreateProject={() => setCreateDialogOpen(true)}
              onOpenDownload={() => setActiveView("download")}
              onOpenBrowse={() => setActiveView("browse")}
              onOpenSettings={() => setActiveView("settings")}
            />

            {createDialogOpen && (
              <section className="home-notice" role="status">
                创建项目功能待接入。
                <button type="button" onClick={() => setCreateDialogOpen(false)}>
                  关闭
                </button>
              </section>
            )}
          </main>
        </div>

        <div
          className={`workspace-view-panel ${
            activeView === "download" ? "workspace-view-panel-active" : "workspace-view-panel-hidden"
          }`}
        >
          <DatasetDownloadPage embedded isActiveView={activeView === "download"} />
        </div>

        <div
          className={`workspace-view-panel ${
            activeView === "browse" ? "workspace-view-panel-active" : "workspace-view-panel-hidden"
          }`}
        >
          <DatasetBrowser />
        </div>

        <div
          className={`workspace-view-panel ${
            activeView === "settings" ? "workspace-view-panel-active" : "workspace-view-panel-hidden"
          }`}
        >
          <SettingsWorkspace />
        </div>
      </div>
    </div>
  );
}
