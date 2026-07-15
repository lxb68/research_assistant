/* 首页首屏：展示产品定位和各研究工作流的快捷入口。 */

"use client";

import { useSyncExternalStore } from "react";
import { APP_THEME_STORAGE_KEY } from "@/lib/theme";

const THEME_CHANGE_EVENT = "research-assistant-theme-change";

function subscribeToTheme(onStoreChange) {
  window.addEventListener(THEME_CHANGE_EVENT, onStoreChange);
  return () => window.removeEventListener(THEME_CHANGE_EVENT, onStoreChange);
}

function getBrowserTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function getServerTheme() {
  return "light";
}

/** 渲染首页介绍和工作流快捷入口。 */
export default function HeroSection({
  onCreateProject,
  onOpenResearchChat,
  onOpenDownload,
  onOpenBrowse,
  onOpenDomainTree,
  onOpenSettings,
}) {
  const activeTheme = useSyncExternalStore(subscribeToTheme, getBrowserTheme, getServerTheme);

  function selectTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(APP_THEME_STORAGE_KEY, theme);
    } catch {
      // 存储不可用时仍保留当前页面的即时主题切换效果。
    }
    window.dispatchEvent(new Event(THEME_CHANGE_EVENT));
  }

  return (
    <section className="hero-section">
      <div className="hero-decoration hero-decoration-primary" />
      <div className="hero-decoration hero-decoration-secondary" />

      <div className="hero-content">
        <div className="hero-theme-switcher" role="group" aria-label="页面主题">
          <button
            type="button"
            className="hero-theme-option hero-theme-option-light"
            aria-pressed={activeTheme === "light"}
            onClick={() => selectTheme("light")}
          >
            <span className="hero-theme-icon hero-theme-icon-light" aria-hidden="true" />
            亮色
          </button>
          <button
            type="button"
            className="hero-theme-option hero-theme-option-dark"
            aria-pressed={activeTheme === "dark"}
            onClick={() => selectTheme("dark")}
          >
            <span className="hero-theme-icon hero-theme-icon-dark" aria-hidden="true" />
            暗色
          </button>
        </div>
        <p className="hero-eyebrow">Research Assistant</p>
        <h1 className="hero-title gradient-text">让研究资料整理更高效</h1>
        <p className="hero-subtitle">
          面向学术和专业场景的智能研究助手，帮助你沉淀资料、组织项目，并把 PDF 解析、知识图谱与领域树生成串成一条顺手的工作流。
        </p>

        <div className="hero-actions">

          <button type="button" className="hero-button hero-button-secondary" onClick={onOpenResearchChat}>
            研究对话
          </button>

          <button type="button" className="hero-button hero-button-secondary" onClick={onOpenDownload}>
            下载数据集
          </button>

          <button type="button" className="hero-button hero-button-secondary" onClick={onOpenBrowse}>
            浏览数据集
          </button>

          <button type="button" className="hero-button hero-button-secondary" onClick={onOpenDomainTree}>
            领域树
          </button>

          <button type="button" className="hero-button hero-button-secondary" onClick={onOpenSettings}>
            设置
          </button>
        </div>
      </div>
    </section>
  );
}
