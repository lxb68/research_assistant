/* 为独立功能页提供品牌入口和当前路由导航。 */

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const pages = [
  { href: "/research-chat", label: "研究对话" },
  { href: "/datasets", label: "数据集中心" },
  { href: "/domain-tree", label: "项目知识空间" },
  { href: "/setting", label: "设置" },
];

/** 为独立功能页渲染统一页头和导航。 */
export default function StandalonePageShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="standalone-page-shell">
      <header className="workspace-topbar standalone-page-topbar">
        <Link className="workspace-brand" href="/">
          <span className="workspace-logo">R</span>
          <span>Research Agent</span>
        </Link>
        <nav className="workspace-tabs" aria-label="研究工作台">
          {pages.map((page) => (
            <Link
              className={`workspace-tab standalone-page-tab ${pathname === page.href ? "workspace-tab-active" : ""}`}
              href={page.href}
              key={page.href}
            >
              {page.label}
            </Link>
          ))}
        </nav>
      </header>
      <div className="standalone-page-content">{children}</div>
    </div>
  );
}
