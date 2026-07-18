/* 在客户端维护当前项目选择；服务端始终重新验证项目与论文成员关系。 */

"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { buildApiUrl } from "@/lib/api";
import { ACTIVE_PROJECT_STORAGE_KEY, WORKSPACE_DOMAIN_TREE_PROJECT_ID } from "@/lib/constants";

export type ResearchProject = {
  id: string;
  name: string;
  description?: string;
  status: string;
  paperCount: number;
  createdAt: string;
  updatedAt: string;
};

type ProjectContextValue = {
  projects: ResearchProject[];
  activeProjectId: string;
  activeProject?: ResearchProject;
  isLoadingProjects: boolean;
  projectError: string;
  selectProject: (projectId: string) => void;
  refreshProjects: () => Promise<ResearchProject[]>;
  createProject: (name: string, description?: string) => Promise<ResearchProject>;
};

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({ children }: { children: React.ReactNode }) {
  const [projects, setProjects] = useState<ResearchProject[]>([]);
  const [activeProjectId, setActiveProjectId] = useState(WORKSPACE_DOMAIN_TREE_PROJECT_ID);
  const [isLoadingProjects, setIsLoadingProjects] = useState(true);
  const [projectError, setProjectError] = useState("");

  const selectProject = useCallback((projectId: string) => {
    const normalized = projectId.trim() || WORKSPACE_DOMAIN_TREE_PROJECT_ID;
    setActiveProjectId(normalized);
    window.localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, normalized);
  }, []);

  const refreshProjects = useCallback(async () => {
    setIsLoadingProjects(true);
    setProjectError("");
    try {
      const response = await fetch(buildApiUrl("/api/projects"), { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "读取项目列表失败");
      }
      const values = (payload.projects ?? []) as ResearchProject[];
      setProjects(values);
      setActiveProjectId((current) => {
        const stored = window.localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY) || current;
        const next = values.some((project) => project.id === stored)
          ? stored
          : values[0]?.id || WORKSPACE_DOMAIN_TREE_PROJECT_ID;
        window.localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, next);
        return next;
      });
      return values;
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : "读取项目列表失败");
      return [];
    } finally {
      setIsLoadingProjects(false);
    }
  }, []);

  const createProject = useCallback(async (name: string, description = "") => {
    const response = await fetch(buildApiUrl("/api/projects"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description, paper_ids: [] }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || "创建项目失败");
    }
    const project = payload.project as ResearchProject;
    await refreshProjects();
    selectProject(project.id);
    return project;
  }, [refreshProjects, selectProject]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshProjects();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshProjects]);

  const value = useMemo<ProjectContextValue>(() => ({
    projects,
    activeProjectId,
    activeProject: projects.find((project) => project.id === activeProjectId),
    isLoadingProjects,
    projectError,
    selectProject,
    refreshProjects,
    createProject,
  }), [
    activeProjectId,
    createProject,
    isLoadingProjects,
    projectError,
    projects,
    refreshProjects,
    selectProject,
  ]);

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProjects(): ProjectContextValue {
  const context = useContext(ProjectContext);
  if (!context) {
    throw new Error("useProjects 必须在 ProjectProvider 内使用");
  }
  return context;
}
