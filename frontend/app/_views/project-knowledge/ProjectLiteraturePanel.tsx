/* 项目与文献的关联管理。该组件不负责领域分析，避免项目配置与分析视图相互耦合。 */

"use client";

import { useState } from "react";
import type { ResearchProject } from "@/app/_components/ProjectProvider";
import { WORKSPACE_DOMAIN_TREE_PROJECT_ID } from "@/lib/constants";
import type { SavedPaper } from "@/lib/papers";
import { ProjectTree } from "@/app/_views/project-knowledge/ProjectTree";
import { ZoteroSourcePanel } from "@/app/_views/project-knowledge/ZoteroSourcePanel";

type ProjectLiteraturePanelProps = {
  projects: ResearchProject[];
  activeProjectId: string;
  projectError: string | null;
  isLoadingProjects: boolean;
  isGenerating: boolean;
  isCreateProjectOpen: boolean;
  newProjectName: string;
  isCreatingProject: boolean;
  isEditingMembers: boolean;
  sourceProjectId: string;
  isLoadingSourcePapers: boolean;
  isSavingMembers: boolean;
  availablePapers: SavedPaper[];
  memberDraftIds: string[];
  onSelectProject: (projectId: string) => void;
  onToggleCreateProject: () => void;
  onNewProjectNameChange: (name: string) => void;
  onCreateProject: () => void;
  onCancelCreateProject: () => void;
  onToggleMemberEditor: () => void;
  onSourceProjectChange: (projectId: string) => void;
  onTogglePaper: (paperId: string, checked: boolean) => void;
  onSelectAllSourcePapers: () => void;
  onClearSourcePapers: () => void;
  onSaveMembers: () => void;
};

export function ProjectLiteraturePanel({
  projects,
  activeProjectId,
  projectError,
  isLoadingProjects,
  isGenerating,
  isCreateProjectOpen,
  newProjectName,
  isCreatingProject,
  isEditingMembers,
  sourceProjectId,
  isLoadingSourcePapers,
  isSavingMembers,
  availablePapers,
  memberDraftIds,
  onSelectProject,
  onToggleCreateProject,
  onNewProjectNameChange,
  onCreateProject,
  onCancelCreateProject,
  onToggleMemberEditor,
  onSourceProjectChange,
  onTogglePaper,
  onSelectAllSourcePapers,
  onClearSourcePapers,
  onSaveMembers,
}: ProjectLiteraturePanelProps) {
  const [zoteroCollectionRefreshToken, setZoteroCollectionRefreshToken] = useState(0);
  const sourcePaperIds = availablePapers.flatMap((paper) => paper.id ? [paper.id] : []);
  const allSourcePapersSelected = sourcePaperIds.length > 0
    && sourcePaperIds.every((paperId) => memberDraftIds.includes(paperId));

  return (
    <section aria-label="项目文献管理">
      <section className="domain-tree-project-workspace" aria-label="当前研究项目">
        <section className="domain-tree-project-bar">
          <div>
            <span className="project-workspace-eyebrow">项目空间</span>
            <h2>项目与文献</h2>
          </div>
          <div className="domain-tree-project-actions">
            <button type="button" onClick={onToggleCreateProject} disabled={isGenerating}>
              新建项目
            </button>
          </div>
        </section>

        <ProjectTree
          projects={projects}
          activeProjectId={activeProjectId}
          canManage={!isGenerating}
          disabled={isLoadingProjects || isGenerating}
          refreshToken={zoteroCollectionRefreshToken}
          managementPanel={isEditingMembers ? (
            <section className="domain-tree-member-editor">
              <div className="domain-tree-card-head">
                <div>
                  <h2>管理项目文献</h2>
                  <p>选择需要参与当前项目领域树、知识图谱和问答检索的文献。</p>
                </div>
                <div className="project-workspace-management-actions">
                  <span>{memberDraftIds.length} 篇已选择</span>
                  <button type="button" onClick={onToggleMemberEditor}>完成</button>
                </div>
              </div>
              <div className="domain-tree-source-project">
                <label>
                  <span>从项目选择文献</span>
                  <select
                    value={sourceProjectId}
                    onChange={(event) => onSourceProjectChange(event.target.value)}
                    disabled={isLoadingSourcePapers || isSavingMembers}
                  >
                    <option value="">全部文献</option>
                    {projects.map((project) => (
                      <option
                        key={project.id}
                        value={project.id}
                        disabled={project.id === activeProjectId}
                      >
                        {project.name}（{project.paperCount} 篇）
                        {project.id === activeProjectId ? " · 当前项目" : ""}
                      </option>
                    ))}
                  </select>
                  <small>当前项目会显示在列表中，但不能作为自身的文献来源。</small>
                </label>
                <div className="domain-tree-project-actions">
                  <button
                    type="button"
                    onClick={allSourcePapersSelected ? onClearSourcePapers : onSelectAllSourcePapers}
                    disabled={isLoadingSourcePapers || sourcePaperIds.length === 0}
                  >
                    {allSourcePapersSelected ? "取消全选" : "全选来源项目"}
                  </button>
                </div>
              </div>
              <div className="domain-tree-member-list">
                {availablePapers.map((paper) => {
                  const paperId = paper.id || "";
                  return (
                    <label key={paperId}>
                      <input
                        type="checkbox"
                        checked={memberDraftIds.includes(paperId)}
                        onChange={(event) => onTogglePaper(paperId, event.target.checked)}
                      />
                      <span>{paper.title || paperId}</span>
                    </label>
                  );
                })}
                {isLoadingSourcePapers ? <span>正在加载来源项目论文…</span> : null}
                {!isLoadingSourcePapers && availablePapers.length === 0 ? <span>所选来源项目当前没有论文。</span> : null}
              </div>
              <div className="domain-tree-project-actions">
                <button type="button" onClick={onSaveMembers} disabled={isSavingMembers}>
                  {isSavingMembers ? "正在保存…" : "保存项目文献"}
                </button>
              </div>
            </section>
          ) : undefined}
          onToggleManagement={onToggleMemberEditor}
          onSelectProject={onSelectProject}
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

      <ZoteroSourcePanel
        projectId={activeProjectId}
        disabled={isGenerating}
        onCollectionsChanged={() => setZoteroCollectionRefreshToken((current) => current + 1)}
      />

      {projectError ? <div className="domain-tree-error">{projectError}</div> : null}
    </section>
  );
}
