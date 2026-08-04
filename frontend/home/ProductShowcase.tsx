/* 首页产品实景区：集中呈现静态截图与功能演示素材。 */

import Image from "next/image";
import styles from "./ProductShowcase.module.css";

const DATASET_SCREENSHOT = "/media/home/dataset-center.png";
const WORKFLOW_DEMO = "/media/home/research-agent-demo.gif";
const WORKFLOW_POSTER = "/media/home/research-agent-demo-poster.png";

/** 展示真实产品界面，并为减少动态效果的用户提供静态替代图。 */
export default function ProductShowcase() {
  return (
    <section className={styles.section} aria-labelledby="product-showcase-title">
      <div className={styles.inner}>
        <header className={styles.header}>
          <span className={styles.eyebrow}>真实工作流</span>
          <h2 id="product-showcase-title">从资料入口到研究空间，关键能力清晰可见</h2>
          <p>
            在一个工作区内检索论文、沉淀文献，并继续构建领域树与知识图谱，
            减少研究过程中来回切换工具的成本。
          </p>
        </header>

        <div className={styles.grid}>
          <article className={styles.card}>
            <div className={styles.frameHeader}>
              <span className={styles.windowDots} aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <span>数据集中心 · 实景截图</span>
            </div>
            <div className={styles.mediaViewport}>
              <Image
                src={DATASET_SCREENSHOT}
                alt="Research Agent 数据集中心的论文搜索、来源筛选和搜索进度界面"
                width={1440}
                height={900}
                sizes="(max-width: 760px) 92vw, (max-width: 1180px) 46vw, 560px"
                className={styles.media}
              />
            </div>
            <div className={styles.caption}>
              <span>01</span>
              <div>
                <h3>多来源论文检索</h3>
                <p>统一设置年份、来源、CCF 等级与影响因子条件，实时查看各来源检索进度。</p>
              </div>
            </div>
          </article>

          <article className={styles.card}>
            <div className={styles.frameHeader}>
              <span className={styles.windowDots} aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <span>首页导航 · 功能演示</span>
            </div>
            <div className={styles.mediaViewport}>
              <Image
                src={WORKFLOW_DEMO}
                alt="从 Research Agent 首页打开数据集中心并返回首页的功能演示"
                width={960}
                height={600}
                sizes="(max-width: 760px) 92vw, (max-width: 1180px) 46vw, 560px"
                unoptimized
                className={`${styles.media} ${styles.animatedMedia}`}
              />
              <Image
                src={WORKFLOW_POSTER}
                alt="Research Agent 首页功能入口"
                width={960}
                height={600}
                sizes="(max-width: 760px) 92vw, (max-width: 1180px) 46vw, 560px"
                className={`${styles.media} ${styles.staticFallback}`}
              />
            </div>
            <div className={styles.caption}>
              <span>02</span>
              <div>
                <h3>一站式研究工作区</h3>
                <p>从首页直达研究对话、数据集与项目知识空间，让常用研究路径保持连贯。</p>
              </div>
            </div>
          </article>
        </div>
      </div>
    </section>
  );
}
