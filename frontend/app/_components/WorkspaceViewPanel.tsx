/* 保留已访问工作区的本地状态，并在重新激活或容器变宽时通知尺寸敏感组件。 */

"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { WORKSPACE_PANEL_RESIZE_EVENT } from "@/lib/responsive";

type WorkspaceViewPanelProps = {
  isActive: boolean;
  label: string;
  children: ReactNode;
};

/** 封装工作区可见性和尺寸通知，避免每个业务页面重复监听 window.resize。 */
export function WorkspaceViewPanel({ isActive, label, children }: WorkspaceViewPanelProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!isActive) return;
    const panel = panelRef.current;
    if (!panel) return;

    let animationFrame = 0;
    const notifyResize = () => {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        const rect = panel.getBoundingClientRect();
        panel.dispatchEvent(new CustomEvent(WORKSPACE_PANEL_RESIZE_EVENT, {
          bubbles: true,
          detail: { width: rect.width, height: rect.height },
        }));
        // 兼容已依赖浏览器 resize 事件的第三方可视化和浮层组件。
        window.dispatchEvent(new Event("resize"));
      });
    };

    notifyResize();
    const observer = new ResizeObserver(notifyResize);
    observer.observe(panel);
    return () => {
      observer.disconnect();
      window.cancelAnimationFrame(animationFrame);
    };
  }, [isActive]);

  return (
    <div
      ref={panelRef}
      className={`workspace-view-panel ${isActive ? "workspace-view-panel-active" : "workspace-view-panel-hidden"}`}
      hidden={!isActive}
      inert={!isActive}
      aria-hidden={!isActive}
      aria-label={label}
      data-active={isActive ? "true" : "false"}
    >
      {children}
    </div>
  );
}

