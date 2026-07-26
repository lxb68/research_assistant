/* 项目与文献的关联管理。该组件不负责领域分析，避免项目配置与分析视图相互耦合。 */

"use client";

import { useState, type ReactNode } from "react";
import type { ResearchProject } from "@/app/_components/ProjectProvider";
import { WORKSPACE_DOMAIN_TREE_PROJECT_ID } from "@/lib/constants";
import type { SavedPaper } from "@/lib/papers";
import { ProjectTree } from "@/app/_views/project-knowledge/ProjectTree";
import { ZoteroSourcePanel } from "@/app/_views/project-knowledge/ZoteroSourcePanel";

type ProjectLiteraturePanelProps = {
  analysisStats: ReactNode;
  projects: ResearchProject[];
  activeProjectId: string;
  projectError: string | null;
  isLoadingProjects: boolean;
  isGenerating: boolean;
  isCreateProjectOpen: boolean;
  newProjectName: string;
  isCreatingProject: boolean;
  isLoadingMembers: boolean;
  sourceProjectId: string;
  isLoadingSourcePapers: boolean;
  isSavingMembers: boolean;
  availablePapers: SavedPaper[];
  savedMemberIds: string[];
  memberDraftIds: string[];
  onSelectProject: (projectId: string) => void;
  onRenameProject: (projectId: string, name: string) => Promise<unknown>;
  onDeleteProject: (projectId: string) => Promise<void>;
  onToggleCreateProject: () => void;
  onNewProjectNameChange: (name: string) => void;
  onCreateProject: () => void;
  onCancelCreateProject: () => void;
  onResetMembers: () => void;
  onSourceProjectChange: (projectId: string) => void;
  onTogglePaper: (paperId: string, checked: boolean) => void;
  onSelectAllSourcePapers: () => void;
  onClearSourcePapers: () => void;
  onSaveMembers: () => Promise<void>;
};

export function ProjectLiteraturePanel({
  analysisStats,
  projects,
  activeProjectId,
  projectError,
  isLoadingProjects,
  isGenerating,
  isCreateProjectOpen,
  newProjectName,
  isCreatingProject,
  isLoadingMembers,
  sourceProjectId,
  isLoadingSourcePapers,
  isSavingMembers,
  availablePapers,
  savedMemberIds,
  memberDraftIds,
  onSelectProject,
  onRenameProject,
  onDeleteProject,
  onToggleCreateProject,
  onNewProjectNameChange,
  onCreateProject,
  onCancelCreateProject,
  onResetMembers,
  onSourceProjectChange,
  onTogglePaper,
  onSelectAllSourcePapers,
  onClearSourcePapers,
  onSaveMembers,
}: ProjectLiteraturePanelProps) {
  const [zoteroCollectionRefreshToken, setZoteroCollectionRefreshToken] = useState(0);
  const [paperSearch, setPaperSearch] = useState("");
  const activeProject = projects.find((project) => project.id === activeProjectId);
  const sourceProject = projects.find((project) => project.id === sourceProjectId);
  const savedMemberIdSet = new Set(savedMemberIds);
  const memberDraftIdSet = new Set(memberDraftIds);
  const normalizedSearch = paperSearch.trim().toLocaleLowerCase();
  const filteredPapers = normalizedSearch
    ? availablePapers.filter((paper) => (
      `${paper.title || ""} ${paper.id || ""}`.toLocaleLowerCase().includes(normalizedSearch)
    ))
    : availablePapers;
  const sourcePaperIds = availablePapers.flatMap((paper) => paper.id ? [paper.id] : []);
  const allSourcePapersSelected = sourcePaperIds.length > 0
    && sourcePaperIds.every((paperId) => memberDraftIdSet.has(paperId));
  const addedCount = memberDraftIds.filter((paperId) => !savedMemberIdSet.has(paperId)).length;
  const removedCount = savedMemberIds.filter((paperId) => !memberDraftIdSet.has(paperId)).length;
  const hasChanges = addedCount > 0 || removedCount > 0;
  const isLoadingLiterature = isLoadingMembers || isLoadingSourcePapers;
  const managementDisabled = isGenerating || isSavingMembers || isLoadingMembers;

  async function saveMembersAndRefreshProjectTree() {
    await onSaveMembers();
    setZoteroCollectionRefreshToken((current) => current + 1);
  }

  return (
    <section aria-label="项目文献管理">
      <ZoteroSourcePanel
        projectId={activeProjectId}
        disabled={isGenerating}
        onCollectionsChanged={() => setZoteroCollectionRefreshToken((current) => current + 1)}
      />

      <section className="domain-tree-project-workspace" aria-label="当前研究项目">
        <section className="domain-tree-project-bar">
          <div>
            <span className="project-workspace-eyebrow">项目空间</span>
            <h2>项目与文献</h2>
          </div>
          {analysisStats}
        </section>

        <ProjectTree
          projects={projects}
          activeProjectId={activeProjectId}
          disabled={isLoadingProjects || isGenerating}
          refreshToken={zoteroCollectionRefreshToken}
          managementPanel={(
            <section className="domain-tree-member-editor">
              <div className="domain-tree-card-head">
                <div>
                  <h2>管理「{activeProject?.name || "当前项目"}」的文献范围</h2>
                  <p>决定哪些文献参与本项目的问答、领域树和知识图谱。</p>
                </div>
                <div className="project-workspace-management-actions">
                  <span>{isLoadingMembers ? "正在加载文献范围…" : `当前已包含 ${savedMemberIds.length} 篇`}</span>
                  <button type="button" onClick={onResetMembers} disabled={managementDisabled || !hasChanges}>
                    取消更改
                  </button>
                </div>
              </div>
              <div className="domain-tree-source-project">
                <div className="project-literature-filter-fields">
                  <label>
                    <span>来源筛选</span>
                    <select
                      value={sourceProjectId}
                      onChange={(event) => onSourceProjectChange(event.target.value)}
                      disabled={isLoadingLiterature || managementDisabled}
                    >
                      <option value="">全部文献</option>
                      {projects
                        .filter((project) => project.id !== activeProjectId)
                        .map((project) => (
                          <option key={project.id} value={project.id}>
                            {project.name}（{project.paperCount} 篇）
                          </option>
                        ))}
                    </select>
                  </label>
                  <label>
                    <span>搜索文献</span>
                    <input
                      type="search"
                      value={paperSearch}
                      onChange={(event) => setPaperSearch(event.target.value)}
                      placeholder="搜索标题或文献 ID"
                      disabled={isLoadingLiterature || managementDisabled}
                    />
                  </label>
                </div>
                <div className="domain-tree-project-actions">
                  <button
                    type="button"
                    onClick={allSourcePapersSelected ? onClearSourcePapers : onSelectAllSourcePapers}
                    disabled={isLoadingLiterature || managementDisabled || sourcePaperIds.length === 0}
                  >
                    {allSourcePapersSelected
                      ? "全部移除"
                      : "全部加入"}
                  </button>
                </div>
              </div>
              <div className="project-literature-list-summary">
                <span>{sourceProject ? `正在查看：${sourceProject.name}` : "正在查看：全部文献"}</span>
                <span>{filteredPapers.length} 篇可见</span>
              </div>
              <div className="domain-tree-member-list">
                {filteredPapers.map((paper) => {
                  const paperId = paper.id || "";
                  const isSaved = savedMemberIdSet.has(paperId);
                  const isSelected = memberDraftIdSet.has(paperId);
                  const changeState = isSaved && !isSelected
                    ? "removing"
                    : !isSaved && isSelected
                      ? "adding"
                      : "neutral";
                  const membershipState = isSaved
                    ? (isSelected ? "已在当前项目" : "待移除")
                    : (isSelected ? "待添加" : "可添加");
                  return (
                    <label
                      key={paperId}
                      className={`project-literature-paper project-literature-paper--${changeState}`}
                    >
                      <input
                        type="checkbox"
                        checked={isSelected}
                        disabled={managementDisabled}
                        onChange={(event) => onTogglePaper(paperId, event.target.checked)}
                      />
                      <span className="project-literature-paper-copy">
                        <strong>{paper.title || paperId}</strong>
                        <small>
                          <span className={`project-literature-membership-state project-literature-membership-state--${changeState}`}>
                            {membershipState}
                          </span>
                          {sourceProject ? <span>来源筛选：{sourceProject.name}</span> : null}
                        </small>
                      </span>
                    </label>
                  );
                })}
                {isLoadingLiterature ? <span>正在加载文献…</span> : null}
                {!isLoadingLiterature && availablePapers.length === 0 ? <span>该来源当前没有文献。</span> : null}
                {!isLoadingLiterature && availablePapers.length > 0 && filteredPapers.length === 0
                  ? <span>没有与“{paperSearch.trim()}”匹配的文献。</span>
                  : null}
              </div>
              <div className="project-literature-save-bar">
                <div aria-live="polite">
                  <strong>
                    {isLoadingMembers
                      ? "正在读取当前项目的文献范围"
                      : hasChanges
                        ? `本次更改：新增 ${addedCount} 篇，移除 ${removedCount} 篇`
                        : "尚未更改文献范围"}
                  </strong>
                  <span>应用后，当前项目共包含 {memberDraftIds.length} 篇文献。</span>
                </div>
                <button
                  type="button"
                  className="project-literature-save-button"
                  onClick={() => void saveMembersAndRefreshProjectTree()}
                  disabled={managementDisabled || !hasChanges}
                >
                  {isSavingMembers ? "正在应用…" : `应用更改（共 ${memberDraftIds.length} 篇）`}
                </button>
              </div>
            </section>
          )}
          onSelectProject={onSelectProject}
          onCreateProject={() => {
            if (!isCreateProjectOpen) onToggleCreateProject();
          }}
          onRenameProject={onRenameProject}
          onDeleteProject={onDeleteProject}
        />

        {isCreateProjectOpen ? (
          <section className="domain-tree-create-project" role="dialog" aria-labelledby="create-project-title">
            <div>
              <strong id="create-project-title">创建研究项目</strong>
              <span>创建后可在同一区域连接或浏览该项目的 Zotero 文件夹。</span>
            </div>
            <label>
              <span>项目名称</span>
              <input
                value={newProjectName}
                onChange={(event) => onNewProjectNameChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    onCreateProject();
                  }
                }}
                placeholder="例如：医学影像跨领域研究"
                maxLength={200}
                autoFocus
                disabled={isCreatingProject}
              />
            </label>
            <div className="domain-tree-project-actions">
              <button type="button" onClick={onCreateProject} disabled={!newProjectName.trim() || isCreatingProject}>
                {isCreatingProject ? "正在创建…" : "创建项目"}
              </button>
              <button type="button" onClick={onCancelCreateProject} disabled={isCreatingProject}>
                取消
              </button>
            </div>
          </section>
        ) : null}
      </section>

      {activeProjectId === WORKSPACE_DOMAIN_TREE_PROJECT_ID ? (
        <div className="domain-tree-status">
          默认项目会自动接收新增的全局论文；手动移除的文献会保持移除状态，也可随时重新加入。
        </div>
      ) : null}

      {projectError ? <div className="domain-tree-error">{projectError}</div> : null}
    </section>
  );
}
