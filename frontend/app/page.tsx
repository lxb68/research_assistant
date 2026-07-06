"use client";

import { useMemo, useState } from "react";
import DatasetDownloadPage from "./dataset-download/page";
import DatasetBrowser from "@/app/dataset-brower/DatasetBrowser";
import SettingsWorkspace from "@/app/setting/SettingsWorkspace";
import HeroSection from "@/home/HeroSection";

type WorkspaceView = "home" | "download" | "browse" | "settings";

export default function Home() {
  const [activeView, setActiveView] = useState<WorkspaceView>("home");
  const [createDialogOpen, setCreateDialogOpen] = useState(false);

  const navItems = useMemo(
    () => [
      { id: "home" as const, label: "主页" },
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

      {activeView === "download" ? (
        <DatasetDownloadPage embedded onBackHome={() => setActiveView("home")} />
      ) : activeView === "browse" ? (
        <DatasetBrowser />
      ) : activeView === "settings" ? (
        <SettingsWorkspace />
      ) : (
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
      )}
    </div>
  );
}
