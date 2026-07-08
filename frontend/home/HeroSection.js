"use client";

export default function HeroSection({
  onCreateProject,
  onOpenDownload,
  onOpenBrowse,
  onOpenDomainTree,
  onOpenSettings,
}) {
  return (
    <section className="hero-section">
      <div className="hero-decoration hero-decoration-primary" />
      <div className="hero-decoration hero-decoration-secondary" />

      <div className="hero-content">
        <p className="hero-eyebrow">Research Assistant</p>
        <h1 className="hero-title gradient-text">让研究资料整理更高效</h1>
        <p className="hero-subtitle">
          面向学术和专业场景的智能研究助手，帮助你沉淀资料、组织项目，并把 PDF 解析、知识图谱与领域树生成串成一条顺手的工作流。
        </p>

        <div className="hero-actions">
          <button type="button" className="hero-button hero-button-primary" onClick={onCreateProject}>
            创建项目
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
