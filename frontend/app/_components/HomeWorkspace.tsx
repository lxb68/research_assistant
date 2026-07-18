/* 管理首页内嵌工作区视图，并同步浏览器地址与返回导航。 */

"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import DatasetBrowser from "@/app/_views/DatasetBrowserView";
import DatasetDownloadPage from "@/app/_views/DatasetDownloadView";
import DomainTreePage from "@/app/_views/DomainTreeView";
import SettingsWorkspace from "@/app/_views/SettingsView";
import HeroSection from "@/home/HeroSection";
import { useProjects } from "@/app/_components/ProjectProvider";

type WorkspaceView = "home" | "download" | "browse" | "domain-tree" | "settings";

/** 把查询参数转换为受支持的工作区视图。 */
function parseWorkspaceView(value: string | null): WorkspaceView {
  // 只接受白名单视图，未知参数统一回退到首页。
  if (value === "download" || value === "browse" || value === "domain-tree" || value === "settings") {
    return value;
  }
  return "home";
}

type HomeWorkspaceProps = {
  initialView: string | null;
};

/** 管理首页内嵌视图和导航状态。 */
export default function HomeWorkspace({ initialView }: HomeWorkspaceProps) {
  const router = useRouter();
  const { createProject } = useProjects();
  const [manualView, setManualView] = useState<WorkspaceView | null>(null);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectMessage, setProjectMessage] = useState("");
  const activeView = manualView ?? parseWorkspaceView(initialView);

  async function handleCreateProject() {
    if (!projectName.trim()) return;
    try {
      await createProject(projectName.trim());
      setProjectName("");
      setCreateDialogOpen(false);
      setManualView("domain-tree");
    } catch (error) {
      setProjectMessage(error instanceof Error ? error.message : "创建项目失败");
    }
  }

  const navItems = useMemo(
    () => [
      { id: "download" as const, label: "下载数据集" },
      { id: "browse" as const, label: "浏览数据集" },
      { id: "domain-tree" as const, label: "项目知识空间" },
      { id: "settings" as const, label: "设置" },
    ],
    [],
  );

  return (
    <div className="workspace-shell">
      {activeView !== "home" && (
        <header className="workspace-topbar">
          <button type="button" className="workspace-brand" onClick={() => setManualView("home")}>
            <span className="workspace-logo">R</span>
            <span>Research Agent</span>
          </button>

          <nav className="workspace-tabs" aria-label="数据集工作台">
            <button type="button" className="workspace-tab" onClick={() => router.push("/research-chat")}>
              研究对话
            </button>
            {navItems.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`workspace-tab ${activeView === item.id ? "workspace-tab-active" : ""}`}
                onClick={() => setManualView(item.id)}
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
              onOpenResearchChat={() => router.push("/research-chat")}
              onOpenDownload={() => setManualView("download")}
              onOpenBrowse={() => setManualView("browse")}
              onOpenDomainTree={() => setManualView("domain-tree")}
              onOpenSettings={() => setManualView("settings")}
            />

            {createDialogOpen && (
              <section className="home-notice" role="status">
                <label>
                  项目名称
                  <input
                    value={projectName}
                    onChange={(event) => setProjectName(event.target.value)}
                    placeholder="例如：医学影像跨领域研究"
                  />
                </label>
                <button type="button" onClick={() => void handleCreateProject()} disabled={!projectName.trim()}>
                  创建并配置文献
                </button>
                <button type="button" onClick={() => setCreateDialogOpen(false)}>取消</button>
                {projectMessage ? <span>{projectMessage}</span> : null}
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
            activeView === "domain-tree" ? "workspace-view-panel-active" : "workspace-view-panel-hidden"
          }`}
        >
          <DomainTreePage
            embedded
            isActiveView={activeView === "domain-tree"}
            onOpenSettings={() => setManualView("settings")}
          />
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
