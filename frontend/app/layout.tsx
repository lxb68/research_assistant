/* 定义站点元数据、中文文档语言和全局主题注入入口。 */

import type { Metadata } from "next";
import ThemeRegistry from "../components/ThemeRegistry";
import "./globals.css";

export const metadata: Metadata = {
  // 浏览器标签页和搜索引擎展示的站点标题。
  title: "Research Assistant",
  // 页面描述信息，主要用于 SEO 和分享预览。
  description: "A smart research assistant for academic and professional use",
};

/** 渲染应用根布局并注入全局主题。 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      {/* 所有页面都会渲染到 body 中，这里保留最轻量的全局布局。 */}
      <body>
        <ThemeRegistry>{children}</ThemeRegistry>
      </body>
    </html>
  );
}
