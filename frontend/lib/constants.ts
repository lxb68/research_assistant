/* 前后端共享的浏览器存储键、项目标识和默认切分参数。 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4000";

export const WORKSPACE_SETTINGS_STORAGE_KEY = "research-agent.settings";
export const WORKSPACE_DOMAIN_TREE_PROJECT_ID = "workspace-domain-tree";

export const DEFAULT_MINIMUM_SPLIT_LENGTH = 1500;
export const DEFAULT_MAXIMUM_SPLIT_LENGTH = 2000;
