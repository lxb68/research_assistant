/* 统一前端响应式断点；CSS 中的媒体查询与容器查询必须使用相同数值。 */

export const RESPONSIVE_BREAKPOINTS = {
  xs: 0,
  sm: 520,
  md: 760,
  lg: 960,
  xl: 1200,
} as const;

export const WORKSPACE_PANEL_RESIZE_EVENT = "research-agent:workspace-panel-resize";
