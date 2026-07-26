/* 统一浏览项目、项目文件夹和文献；项目业务操作仍由上层组件负责。 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  disabled: boolean;
  refreshToken: number;
  managementPanel: ReactNode;
  onSelectProject: (projectId: string) => void;
  onCreateProject: () => void;
  onRenameProject: (projectId: string, name: string) => Promise<unknown>;
  onDeleteProject: (projectId: string) => Promise<void>;
};

type ProjectContextMenu = {
  x: number;
  y: number;
  project: ResearchProject | null;
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
  disabled,
  refreshToken,
  managementPanel,
  onSelectProject,
  onCreateProject,
  onRenameProject,
  onDeleteProject,
}: ProjectTreeProps) {
  const [trees, setTrees] = useState<ZoteroCollectionTree[]>([]);
  const [projectPapers, setProjectPapers] = useState<SavedPaper[]>([]);
  const [visiblePapers, setVisiblePapers] = useState<SavedPaper[]>([]);
  const [selectedScopeId, setSelectedScopeId] = useState("project-papers");
  const [selectedScopeName, setSelectedScopeName] = useState("全部文献");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [loadedProjectId, setLoadedProjectId] = useState("");
  const [contextMenu, setContextMenu] = useState<ProjectContextMenu | null>(null);
  const [renameProject, setRenameProject] = useState<ResearchProject | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleteProject, setDeleteProject] = useState<ResearchProject | null>(null);
  const [projectActionError, setProjectActionError] = useState("");
  const [isProjectActionBusy, setIsProjectActionBusy] = useState(false);
  const collectionRequestRef = useRef<AbortController | null>(null);
  const projectRequestIdRef = useRef(0);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);

  const loadProjectContents = useCallback(async (signal?: AbortSignal) => {
    if (!activeProjectId) return;
    const targetProjectId = activeProjectId;
    const requestId = projectRequestIdRef.current + 1;
    projectRequestIdRef.current = requestId;
    collectionRequestRef.current?.abort();
    collectionRequestRef.current = null;
    setIsLoading(true);
    setError("");
    setTrees([]);
    setVisiblePapers([]);
    setSelectedScopeId("project-papers");
    setSelectedScopeName("全部文献");
    try {
      const [papersResponse, treesResponse] = await Promise.all([
        fetch(buildApiUrl(`/api/projects/${encodeURIComponent(targetProjectId)}/papers`), {
          cache: "no-store",
          signal,
        }),
        fetch(buildApiUrl(`/api/projects/${encodeURIComponent(targetProjectId)}/zotero-collections/tree`), {
          cache: "no-store",
          signal,
        }),
      ]);
      const [papersPayload, treesPayload] = await Promise.all([
        responsePayload(papersResponse, "读取项目文献失败"),
        responsePayload(treesResponse, "读取项目文件夹失败"),
      ]);
      if (projectRequestIdRef.current !== requestId) return;
      const nextPapers = (papersPayload.papers ?? []) as SavedPaper[];
      setProjectPapers(nextPapers);
      setVisiblePapers(nextPapers);
      setTrees((treesPayload.trees ?? []) as ZoteroCollectionTree[]);
      setLoadedProjectId(targetProjectId);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      if (projectRequestIdRef.current !== requestId) return;
      setTrees([]);
      setProjectPapers([]);
      setVisiblePapers([]);
      setLoadedProjectId("");
      setError(reason instanceof Error ? reason.message : "读取项目空间失败");
    } finally {
      if (!signal?.aborted && projectRequestIdRef.current === requestId) setIsLoading(false);
    }
  }, [activeProjectId]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadProjectContents(controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      projectRequestIdRef.current += 1;
      controller.abort();
    };
  }, [loadProjectContents, refreshToken]);

  useEffect(() => () => {
    collectionRequestRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!contextMenu) return;
    function closeContextMenu(event: MouseEvent) {
      if (
        event.target instanceof Node
        && contextMenuRef.current?.contains(event.target)
      ) {
        return;
      }
      setContextMenu(null);
    }
    function closeForViewportChange() {
      setContextMenu(null);
    }
    function closeWithKeyboard(event: KeyboardEvent) {
      if (event.key === "Escape") setContextMenu(null);
    }
    document.addEventListener("mousedown", closeContextMenu);
    document.addEventListener("keydown", closeWithKeyboard);
    window.addEventListener("blur", closeForViewportChange);
    window.addEventListener("resize", closeForViewportChange);
    window.addEventListener("scroll", closeForViewportChange, true);
    return () => {
      document.removeEventListener("mousedown", closeContextMenu);
      document.removeEventListener("keydown", closeWithKeyboard);
      window.removeEventListener("blur", closeForViewportChange);
      window.removeEventListener("resize", closeForViewportChange);
      window.removeEventListener("scroll", closeForViewportChange, true);
    };
  }, [contextMenu]);

  useEffect(() => {
    if ((!renameProject && !deleteProject) || isProjectActionBusy) return;
    function closeDialog(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setRenameProject(null);
      setDeleteProject(null);
      setProjectActionError("");
    }
    document.addEventListener("keydown", closeDialog);
    return () => document.removeEventListener("keydown", closeDialog);
  }, [deleteProject, isProjectActionBusy, renameProject]);

  async function selectCollection(node: ZoteroCollectionTreeNode) {
    collectionRequestRef.current?.abort();
    const controller = new AbortController();
    collectionRequestRef.current = controller;
    const scopeId = `${node.sourceId}:${node.key}`;
    setSelectedScopeId(scopeId);
    setSelectedScopeName(node.name);
    setVisiblePapers([]);
    setIsLoading(true);
    setError("");
    try {
      const response = await fetch(buildApiUrl(
        `/api/projects/${encodeURIComponent(activeProjectId)}/zotero-collections/`
        + `${encodeURIComponent(node.sourceId)}/${encodeURIComponent(node.key)}/papers`
        + "?includeDescendants=true",
      ), { cache: "no-store", signal: controller.signal });
      const payload = await responsePayload(response, "读取文件夹文献失败");
      if (collectionRequestRef.current !== controller) return;
      setVisiblePapers((payload.papers ?? []) as SavedPaper[]);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      if (collectionRequestRef.current !== controller) return;
      setVisiblePapers([]);
      setError(reason instanceof Error ? reason.message : "读取文件夹文献失败");
    } finally {
      if (collectionRequestRef.current === controller) {
        collectionRequestRef.current = null;
        setIsLoading(false);
      }
    }
  }

  function selectAllProjectPapers() {
    setSelectedScopeId("project-papers");
    setSelectedScopeName("全部文献");
    setVisiblePapers(projectPapers);
  }

  function openContextMenu(
    event: React.MouseEvent,
    project: ResearchProject | null,
  ) {
    if (disabled) return;
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({
      x: Math.min(event.clientX, Math.max(8, window.innerWidth - 224)),
      y: Math.min(event.clientY, Math.max(8, window.innerHeight - 176)),
      project,
    });
  }

  function requestCreateProject() {
    setContextMenu(null);
    onCreateProject();
  }

  function requestRenameProject(project: ResearchProject) {
    setContextMenu(null);
    setRenameProject(project);
    setRenameValue(project.name);
    setProjectActionError("");
  }

  function requestDeleteProject(project: ResearchProject) {
    setContextMenu(null);
    setDeleteProject(project);
    setProjectActionError("");
  }

  async function submitRenameProject(event: React.FormEvent) {
    event.preventDefault();
    if (!renameProject || !renameValue.trim() || isProjectActionBusy) return;
    setIsProjectActionBusy(true);
    setProjectActionError("");
    try {
      await onRenameProject(renameProject.id, renameValue.trim());
      setRenameProject(null);
      setRenameValue("");
    } catch (reason) {
      setProjectActionError(reason instanceof Error ? reason.message : "修改项目名称失败");
    } finally {
      setIsProjectActionBusy(false);
    }
  }

  async function confirmDeleteProject() {
    if (!deleteProject || isProjectActionBusy) return;
    setIsProjectActionBusy(true);
    setProjectActionError("");
    try {
      await onDeleteProject(deleteProject.id);
      setDeleteProject(null);
    } catch (reason) {
      setProjectActionError(reason instanceof Error ? reason.message : "删除项目失败");
    } finally {
      setIsProjectActionBusy(false);
    }
  }

  const activeProject = projects.find((project) => project.id === activeProjectId);
  const activeProjectPaperCount = loadedProjectId === activeProjectId
    ? projectPapers.length
    : activeProject?.paperCount;
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
        <nav
          className="project-root-tree"
          aria-label="项目、文件夹树"
          onContextMenu={(event) => openContextMenu(event, null)}
        >
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
                  onContextMenu={(event) => openContextMenu(event, project)}
                >
                  <span className="project-root-tree-marker" aria-hidden>—</span>
                  <span className={`project-root-tree-kind ${
                    zoteroProject ? "is-zotero" : systemProject ? "is-system" : "is-user"
                  }`}>
                    {zoteroProject ? "Zotero" : systemProject ? "默认" : "新建"}
                  </span>
                  <span className="project-root-tree-name">{project.name}</span>
                  <small>{isActive ? (activeProjectPaperCount ?? "—") : project.paperCount} 篇</small>
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
          {selectedScopeId === "project-papers" ? managementPanel : (
            <>
              <header>
                <div>
                  <strong>{selectedScopeName}</strong>
                  <small>{activeProject?.name ?? "当前项目"}</small>
                </div>
                <div>
                  <span>{isLoading ? "正在读取…" : `${visiblePapers.length} 篇`}</span>
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
              {isLoading ? (
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
      {contextMenu && typeof document !== "undefined" ? createPortal(
        <div
          ref={contextMenuRef}
          className="project-context-menu"
          role="menu"
          aria-label={contextMenu.project ? `${contextMenu.project.name} 项目操作` : "项目树操作"}
          style={{ left: contextMenu.x, top: contextMenu.y }}
          onContextMenu={(event) => event.preventDefault()}
        >
          <button type="button" role="menuitem" onClick={requestCreateProject}>
            新建项目
          </button>
          {contextMenu.project ? (
            <>
              <div className="project-context-menu-separator" role="separator" />
              <button
                type="button"
                role="menuitem"
                onClick={() => requestRenameProject(contextMenu.project as ResearchProject)}
              >
                修改项目名称
              </button>
              <button
                type="button"
                role="menuitem"
                className="is-danger"
                disabled={contextMenu.project.id === WORKSPACE_DOMAIN_TREE_PROJECT_ID}
                title={
                  contextMenu.project.id === WORKSPACE_DOMAIN_TREE_PROJECT_ID
                    ? "默认研究项目不能删除"
                    : undefined
                }
                onClick={() => requestDeleteProject(contextMenu.project as ResearchProject)}
              >
                删除项目
              </button>
            </>
          ) : null}
        </div>,
        document.body,
      ) : null}
      {renameProject && typeof document !== "undefined" ? createPortal(
        <div
          className="project-action-dialog-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !isProjectActionBusy) {
              setRenameProject(null);
            }
          }}
        >
          <form
            className="project-action-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="rename-project-title"
            onSubmit={(event) => void submitRenameProject(event)}
          >
            <div>
              <span>项目操作</span>
              <h2 id="rename-project-title">修改项目名称</h2>
              <p>只修改项目显示名称，不影响文献关联和分析结果。</p>
            </div>
            {projectActionError ? <div className="project-action-dialog-error">{projectActionError}</div> : null}
            <label>
              <span>项目名称</span>
              <input
                autoFocus
                required
                maxLength={200}
                value={renameValue}
                disabled={isProjectActionBusy}
                onChange={(event) => setRenameValue(event.target.value)}
              />
            </label>
            <div className="project-action-dialog-actions">
              <button type="submit" disabled={!renameValue.trim() || isProjectActionBusy}>
                {isProjectActionBusy ? "正在保存…" : "保存名称"}
              </button>
              <button type="button" disabled={isProjectActionBusy} onClick={() => setRenameProject(null)}>
                取消
              </button>
            </div>
          </form>
        </div>,
        document.body,
      ) : null}
      {deleteProject && typeof document !== "undefined" ? createPortal(
        <div
          className="project-action-dialog-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !isProjectActionBusy) {
              setDeleteProject(null);
            }
          }}
        >
          <section
            className="project-action-dialog is-danger"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-project-title"
          >
            <div>
              <span>安全删除</span>
              <h2 id="delete-project-title">删除「{deleteProject.name}」？</h2>
              <p>项目将从项目树隐藏；原文献、项目成员关系和领域树分析产物都会保留。</p>
            </div>
            {projectActionError ? <div className="project-action-dialog-error">{projectActionError}</div> : null}
            <div className="project-action-dialog-actions">
              <button
                type="button"
                className="is-danger"
                disabled={isProjectActionBusy}
                onClick={() => void confirmDeleteProject()}
              >
                {isProjectActionBusy ? "正在删除…" : "确认删除"}
              </button>
              <button type="button" disabled={isProjectActionBusy} onClick={() => setDeleteProject(null)}>
                取消
              </button>
            </div>
          </section>
        </div>,
        document.body,
      ) : null}
    </section>
  );
}
