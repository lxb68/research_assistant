"use client";

import { useState } from "react";
import HeroSection from "@/home/HeroSection";

export default function Home() {
  const [createDialogOpen, setCreateDialogOpen] = useState(false);

  return (
    <main className="home-page">
      {/* 首页首屏区域：负责展示产品定位和主要操作入口。 */}
      <HeroSection onCreateProject={() => setCreateDialogOpen(true)} />

      {/* 创建项目弹窗还没有接入，这里先用提示占位，避免状态变量空置。 */}
      {createDialogOpen && (
        <section className="home-notice" role="status">
          创建项目功能待接入。
          <button type="button" onClick={() => setCreateDialogOpen(false)}>
            关闭
          </button>
        </section>
      )}
    </main>
  );
}
