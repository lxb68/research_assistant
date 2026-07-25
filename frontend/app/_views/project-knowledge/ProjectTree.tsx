/* 统一浏览项目、项目文件夹和文献；项目业务操作仍由上层组件负责。 */

"use client";

import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import AutoStoriesRounded from "@mui/icons-material/AutoStoriesRounded";
import FolderRounded from "@mui/icons-material/FolderRounded";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import type { ResearchProject } from "@/app/_components/ProjectProvider";
import { buildApiUrl } from "@/lib/api";
import { WORKSPACE_DOMAIN_TREE_PROJECT_ID } from "@/lib/constants";
import type { SavedPaper } from "@/lib/papers";

type ZoteroCollectionTreeNode = {
  sourceId: string;
  key: string;
  name: string;
  path: string;
  paperCount: number;
  children: ZoteroCollectionTreeNode[];
};

type ZoteroCollectionTree = {
  sourceId: string;
  roots: ZoteroCollectionTreeNode[];
};

type ProjectTreeProps = {
  projects: ResearchProject[];
  activeProjectId: string;
  canManage: boolean;
  disabled: boolean;
  refreshToken: number;
  managementPanel?: ReactNode;
  onToggleManagement: () => void;
  onSelectProject: (projectId: string) => void;
};

async function responsePayload(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(typeof payload.detail === "string" ? payload.detail : fallback);
  }
  return payload;
}

function isZoteroProject(project: ResearchProject): boolean {
  return project.description?.trim().startsWith("由 Zotero") ?? false;
}

function CollectionNode({
  node,
  selectedScopeId,
  onSelect,
}: {
  node: ZoteroCollectionTreeNode;
  selectedScopeId: string;
  onSelect: (node: ZoteroCollectionTreeNode) => void;
}) {
  const scopeId = `${node.sourceId}:${node.key}`;
  return (
    <li>
      <button
        type="button"
        className={`project-workspace-folder${selectedScopeId === scopeId ? " is-active" : ""}`}
        onClick={() => onSelect(node)}
        title={node.path}
      >
        <FolderRounded />
        <span>{node.name}</span>
        <small>{node.paperCount}</small>
      </button>
      {node.children.length ? (
        <ul>
          {node.children.map((child) => (
            <CollectionNode
              key={`${child.sourceId}:${child.key}`}
              node={child}
              selectedScopeId={selectedScopeId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function ProjectTree({
  projects,
  activeProjectId,
  canManage,
  disabled,
  refreshToken,
  managementPanel,
  onToggleManagement,
  onSelectProject,
}: ProjectTreeProps) {
  const [trees, setTrees] = useState<ZoteroCollectionTree[]>([]);
  const [projectPapers, setProjectPapers] = useState<SavedPaper[]>([]);
  const [visiblePapers, setVisiblePapers] = useState<SavedPaper[]>([]);
  const [selectedScopeId, setSelectedScopeId] = useState("project-papers");
  const [selectedScopeName, setSelectedScopeName] = useState("全部文献");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");

  const loadProjectContents = useCallback(async (signal?: AbortSignal) => {
    if (!activeProjectId) return;
    setIsLoading(true);
    setError("");
    try {
      const [papersResponse, treesResponse] = await Promise.all([
        fetch(buildApiUrl(`/api/projects/${encodeURIComponent(activeProjectId)}/papers`), {
          cache: "no-store",
          signal,
        }),
        fetch(buildApiUrl(`/api/projects/${encodeURIComponent(activeProjectId)}/zotero-collections/tree`), {
          cache: "no-store",
          signal,
        }),
      ]);
      const [papersPayload, treesPayload] = await Promise.all([
        responsePayload(papersResponse, "读取项目文献失败"),
        responsePayload(treesResponse, "读取项目文件夹失败"),
      ]);
      const nextPapers = (papersPayload.papers ?? []) as SavedPaper[];
      setProjectPapers(nextPapers);
      setVisiblePapers(nextPapers);
      setTrees((treesPayload.trees ?? []) as ZoteroCollectionTree[]);
      setSelectedScopeId("project-papers");
      setSelectedScopeName("全部文献");
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setTrees([]);
      setProjectPapers([]);
      setVisiblePapers([]);
      setError(reason instanceof Error ? reason.message : "读取项目空间失败");
    } finally {
      if (!signal?.aborted) setIsLoading(false);
    }
  }, [activeProjectId]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadProjectContents(controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadProjectContents, refreshToken]);

  async function selectCollection(node: ZoteroCollectionTreeNode) {
    const scopeId = `${node.sourceId}:${node.key}`;
    setSelectedScopeId(scopeId);
    setSelectedScopeName(node.name);
    setIsLoading(true);
    setError("");
    try {
      const response = await fetch(buildApiUrl(
        `/api/projects/${encodeURIComponent(activeProjectId)}/zotero-collections/`
        + `${encodeURIComponent(node.sourceId)}/${encodeURIComponent(node.key)}/papers`
        + "?includeDescendants=true",
      ), { cache: "no-store" });
      const payload = await responsePayload(response, "读取文件夹文献失败");
      setVisiblePapers((payload.papers ?? []) as SavedPaper[]);
    } catch (reason) {
      setVisiblePapers([]);
      setError(reason instanceof Error ? reason.message : "读取文件夹文献失败");
    } finally {
      setIsLoading(false);
    }
  }

  function selectAllProjectPapers() {
    setSelectedScopeId("project-papers");
    setSelectedScopeName("全部文献");
    setVisiblePapers(projectPapers);
  }

  const activeProject = projects.find((project) => project.id === activeProjectId);
  const collectionRoots = trees.flatMap((tree) => tree.roots);
  const visibleCollectionRoots = (
    activeProject
    && collectionRoots.length === 1
    && collectionRoots[0].name.trim().toLocaleLowerCase() === activeProject.name.trim().toLocaleLowerCase()
  )
    ? collectionRoots[0].children
    : collectionRoots;

  return (
    <section className="project-workspace-browser" aria-label="项目空间与项目文件夹">
      <div className="project-workspace-grid">
        <nav className="project-root-tree" aria-label="项目、文件夹树">
          <div className="project-root-tree-entry is-root">
            <div className="project-root-tree-node project-root-tree-container">
              <span className="project-root-tree-marker" aria-hidden>◆</span>
              <span className="project-root-tree-name">根项目</span>
            </div>
          </div>
          {projects.map((project) => {
            const isActive = project.id === activeProjectId;
            const zoteroProject = isZoteroProject(project);
            const systemProject = project.id === WORKSPACE_DOMAIN_TREE_PROJECT_ID;
            return (
              <div className="project-root-tree-entry" key={project.id}>
                <button
                  type="button"
                  className={`project-root-tree-node${isActive ? " is-active" : ""}`}
                  onClick={() => {
                    if (isActive) selectAllProjectPapers();
                    else onSelectProject(project.id);
                  }}
                  disabled={disabled}
                  aria-current={isActive ? "page" : undefined}
                >
                  <span className="project-root-tree-marker" aria-hidden>—</span>
                  <span className={`project-root-tree-kind ${
                    zoteroProject ? "is-zotero" : systemProject ? "is-system" : "is-user"
                  }`}>
                    {zoteroProject ? "Zotero" : systemProject ? "默认" : "新建"}
                  </span>
                  <span className="project-root-tree-name">{project.name}</span>
                  <small>{isActive ? projectPapers.length : project.paperCount} 篇</small>
                </button>

                {isActive && visibleCollectionRoots.length ? (
                  <ul className="project-workspace-folder-tree">
                    {visibleCollectionRoots.map((node) => (
                      <CollectionNode
                        key={`${node.sourceId}:${node.key}`}
                        node={node}
                        selectedScopeId={selectedScopeId}
                        onSelect={(value) => void selectCollection(value)}
                      />
                    ))}
                  </ul>
                ) : null}
              </div>
            );
          })}
          {!projects.length ? <div className="project-root-tree-empty">正在读取项目…</div> : null}
        </nav>

        <section className="project-workspace-papers" aria-label="所选项目或文件夹的文献">
          {managementPanel ? managementPanel : selectedScopeId === "project-papers" ? (
            <section className="project-workspace-management-entry">
              <div>
                <span className="project-workspace-eyebrow">当前项目</span>
                <h3>{activeProject?.name ?? "项目"}</h3>
                <p>
                  {canManage
                    ? "在这里维护参与领域树、知识图谱和问答检索的项目文献。"
                    : "该项目的文献由系统自动维护，无需手动管理。"}
                </p>
              </div>
              {canManage ? (
                <button type="button" onClick={onToggleManagement}>
                  管理文献
                </button>
              ) : null}
            </section>
          ) : (
            <>
              <header>
                <div>
                  <strong>{selectedScopeName}</strong>
                  <small>{activeProject?.name ?? "当前项目"}</small>
                </div>
                <div>
                  <span>{visiblePapers.length} 篇</span>
                  <button
                    type="button"
                    onClick={() => void loadProjectContents()}
                    disabled={isLoading}
                    aria-label="刷新项目空间"
                  >
                    <RefreshRounded className={isLoading ? "zotero-spin" : ""} />
                  </button>
                </div>
              </header>

              {error ? <div className="project-zotero-error">{error}</div> : null}
              {isLoading && !visiblePapers.length ? (
                <div className="zotero-collection-papers-empty">正在读取文献…</div>
              ) : visiblePapers.length ? (
                <ul>
                  {visiblePapers.map((paper) => (
                    <li key={paper.id}>
                      <AutoStoriesRounded />
                      <div>
                        <strong>{paper.title || "未命名文献"}</strong>
                        <small>
                          {[paper.authors?.slice(0, 3).join("、"), paper.year, paper.venue]
                            .filter(Boolean)
                            .join(" · ") || "暂无题录信息"}
                        </small>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="zotero-collection-papers-empty">
                  <FolderRounded />
                  当前项目或文件夹暂无文献
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </section>
  );
}
