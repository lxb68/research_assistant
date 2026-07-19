/* 项目与文献的关联管理。该组件不负责领域分析，避免项目配置与分析视图相互耦合。 */

import type { ResearchProject } from "@/app/_components/ProjectProvider";
import { WORKSPACE_DOMAIN_TREE_PROJECT_ID } from "@/lib/constants";
import type { SavedPaper } from "@/lib/papers";

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
  return (
    <section aria-label="项目文献管理">
      <section className="domain-tree-project-bar" aria-label="当前研究项目">
        <label>
          <span>当前项目</span>
          <select
            value={activeProjectId}
            onChange={(event) => onSelectProject(event.target.value)}
            disabled={isLoadingProjects || isGenerating}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}（{project.paperCount} 篇）
              </option>
            ))}
          </select>
        </label>
        <div className="domain-tree-project-actions">
          <button type="button" onClick={onToggleCreateProject} disabled={isGenerating}>
            新建项目
          </button>
          <button
            type="button"
            onClick={onToggleMemberEditor}
            disabled={activeProjectId === WORKSPACE_DOMAIN_TREE_PROJECT_ID || isGenerating}
          >
            {isEditingMembers ? "收起文献管理" : "管理项目文献"}
          </button>
        </div>
      </section>

      {isCreateProjectOpen ? (
        <section className="domain-tree-create-project" role="dialog" aria-labelledby="create-project-title">
          <div>
            <strong id="create-project-title">创建研究项目</strong>
            <span>新项目初始为空，创建后可选择需要分析的论文。</span>
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

      {activeProjectId === WORKSPACE_DOMAIN_TREE_PROJECT_ID ? (
        <div className="domain-tree-status">默认项目自动包含全局论文，用于兼容升级前的工作区。</div>
      ) : null}

      {isEditingMembers ? (
        <section className="domain-tree-member-editor">
          <div className="domain-tree-card-head">
            <div>
              <h2>项目论文成员</h2>
              <p>只有勾选的论文会参与该项目的领域树、知识图谱和问答检索。</p>
            </div>
            <span>{memberDraftIds.length} 篇已选择</span>
          </div>
          <div className="domain-tree-source-project">
            <label>
              <span>从项目选择文献</span>
              <select
                value={sourceProjectId}
                onChange={(event) => onSourceProjectChange(event.target.value)}
                disabled={isLoadingSourcePapers || isSavingMembers}
              >
                {projects
                  .filter((project) => project.id !== activeProjectId)
                  .map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}（{project.paperCount} 篇）
                    </option>
                  ))}
              </select>
            </label>
            <div className="domain-tree-project-actions">
              <button type="button" onClick={onSelectAllSourcePapers} disabled={isLoadingSourcePapers || availablePapers.length === 0}>
                全选来源项目
              </button>
              <button type="button" onClick={onClearSourcePapers} disabled={isLoadingSourcePapers || availablePapers.length === 0}>
                清除来源项目
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
      ) : null}

      {projectError ? <div className="domain-tree-error">{projectError}</div> : null}
    </section>
  );
}
