/* 文献地图独立视图：管理地图状态、筛选和手动构建，不耦合领域树状态。 */

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { SavedPaper } from "@/lib/papers";
import {
  buildLiteratureMap,
  fetchLiteratureMap,
  LiteratureMapCard,
  LiteratureMapSnapshot,
} from "@/lib/literature-map";
import styles from "./LiteratureMapPanel.module.css";

type LiteratureMapPanelProps = {
  projectId: string;
  papers: SavedPaper[];
  modelConfigured?: boolean;
};

const STATUS_LABELS: Record<LiteratureMapSnapshot["status"], string> = {
  empty: "尚未生成",
  ready: "已更新",
  stale: "有文献变化",
  building: "正在构建",
  failed: "构建失败",
};

function actionLabel(snapshot: LiteratureMapSnapshot | null, isBuilding: boolean): string {
  if (isBuilding || snapshot?.status === "building") return "正在构建...";
  if (!snapshot || snapshot.status === "empty") return "生成文献地图";
  if (snapshot.status === "stale") return "更新文献地图";
  return "重新构建";
}

function confidenceLabel(confidence?: number): string {
  if (confidence === undefined) return "";
  return `${Math.round(confidence * 100)}%`;
}

export function LiteratureMapPanel({
  projectId,
  papers,
  modelConfigured,
}: LiteratureMapPanelProps) {
  const [snapshot, setSnapshot] = useState<LiteratureMapSnapshot | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isBuilding, setIsBuilding] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selectedPaperId, setSelectedPaperId] = useState("");

  const loadMap = useCallback(async (signal?: AbortSignal, quiet = false) => {
    if (!quiet) {
      setIsLoading(true);
      setError("");
    }
    try {
      const result = await fetchLiteratureMap(projectId, signal);
      setSnapshot(result);
    } catch (loadError) {
      if (signal?.aborted) return;
      setError(loadError instanceof Error ? loadError.message : "加载文献地图失败");
    } finally {
      if (!quiet && !signal?.aborted) setIsLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    void fetchLiteratureMap(projectId, controller.signal)
      .then((result) => {
        setSnapshot(result);
        setError("");
      })
      .catch((loadError: unknown) => {
        if (controller.signal.aborted) return;
        setError(loadError instanceof Error ? loadError.message : "加载文献地图失败");
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false);
      });
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (snapshot?.status !== "building") return;
    const timer = window.setInterval(() => {
      void loadMap(undefined, true);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [loadMap, snapshot?.status]);

  const visibleCards = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) return snapshot?.cards ?? [];
    return (snapshot?.cards ?? []).filter((card) => (
      `${card.title} ${card.year ?? ""} ${Object.values(card.facets).flat().join(" ")}`
        .toLocaleLowerCase()
        .includes(normalizedQuery)
    ));
  }, [query, snapshot?.cards]);

  const selectedCard: LiteratureMapCard | undefined = useMemo(
    () => visibleCards.find((card) => card.paperId === selectedPaperId) ?? visibleCards[0],
    [selectedPaperId, visibleCards],
  );

  const selectedRelations = useMemo(
    () => (snapshot?.relations ?? []).filter(
      (relation) => relation.sourcePaperId === selectedCard?.paperId,
    ),
    [selectedCard?.paperId, snapshot?.relations],
  );

  async function handleBuild() {
    setIsBuilding(true);
    setError("");
    try {
      const force = snapshot?.status === "ready" || snapshot?.status === "failed";
      const result = await buildLiteratureMap(projectId, { force });
      if (result) setSnapshot(result);
      await loadMap(undefined, true);
    } catch (buildError) {
      setError(buildError instanceof Error ? buildError.message : "文献地图构建失败");
    } finally {
      setIsBuilding(false);
    }
  }

  const paperCount = snapshot?.paperCount ?? 0;
  const readyPaperCount = papers.filter(
    (paper) => Boolean(paper.id && (paper.markdownPath || paper.markdownOutputDir)),
  ).length;

  return (
    <section className={styles.page} aria-label="文献地图">
      <header className={styles.header}>
        <div>
          <p>文献地图</p>
          <h2>论文脉络</h2>
        </div>
        <div className={styles.metrics} aria-label="文献地图统计">
          <span><strong>{paperCount}</strong> 文献</span>
          <span><strong>{snapshot?.claimCount ?? 0}</strong> 声明</span>
          <span><strong>{snapshot?.relationCount ?? 0}</strong> 关系</span>
        </div>
        <div className={styles.actions}>
          <span className={`${styles.status} ${styles[snapshot?.status ?? "empty"]}`}>
            {isLoading ? "正在加载" : STATUS_LABELS[snapshot?.status ?? "empty"]}
          </span>
          <button
            type="button"
            className={styles.primaryButton}
            disabled={
              isLoading
              || isBuilding
              || snapshot?.status === "building"
              || modelConfigured !== true
              || readyPaperCount === 0
            }
            onClick={() => void handleBuild()}
          >
            {actionLabel(snapshot, isBuilding)}
          </button>
        </div>
      </header>

      {error ? <div className={styles.error}>{error}</div> : null}
      {modelConfigured === false ? <div className={styles.notice}>请先完成模型配置。</div> : null}
      {modelConfigured === true && readyPaperCount === 0 ? (
        <div className={styles.notice}>暂无可分析文献。</div>
      ) : null}

      {!isLoading && snapshot?.cards.length ? (
        <div className={styles.browser}>
          <aside className={styles.sidebar}>
            <label className={styles.search}>
              <span>筛选文献</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="标题、年份或研究维度"
              />
            </label>
            <div className={styles.paperList}>
              {visibleCards.map((card) => (
                <button
                  type="button"
                  key={card.paperId}
                  className={`${styles.paperButton}${
                    selectedCard?.paperId === card.paperId ? ` ${styles.selected}` : ""
                  }`}
                  onClick={() => setSelectedPaperId(card.paperId)}
                >
                  <strong>{card.title}</strong>
                  <span>{card.year || "年份未知"} · {card.claims.length} 条声明</span>
                </button>
              ))}
              {visibleCards.length === 0 ? <span className={styles.empty}>没有匹配文献。</span> : null}
            </div>
          </aside>

          <div className={styles.detail}>
            {selectedCard ? (
              <>
                <div className={styles.cardHeader}>
                  <div>
                    <span>{selectedCard.year || "年份未知"}</span>
                    <h3>{selectedCard.title}</h3>
                  </div>
                  <span>{selectedCard.claims.length} 条声明</span>
                </div>

                {selectedCard.summary ? <p className={styles.summary}>{selectedCard.summary}</p> : null}

                {Object.keys(selectedCard.facets).length ? (
                  <div className={styles.facets}>
                    {Object.entries(selectedCard.facets).map(([name, values]) => (
                      <div key={name}>
                        <strong>{name}</strong>
                        <span>{values.join(" · ")}</span>
                      </div>
                    ))}
                  </div>
                ) : null}

                <section className={styles.section}>
                  <div className={styles.sectionTitle}>
                    <h4>核心声明</h4>
                    <span>{selectedCard.claims.length}</span>
                  </div>
                  <div className={styles.claimList}>
                    {selectedCard.claims.map((claim) => (
                      <article key={claim.id} className={styles.claim}>
                        <div className={styles.claimLine}>
                          <span>{claim.kind || "声明"}</span>
                          <p>
                            <strong>{claim.subject}</strong>
                            {" "}{claim.predicate}{" "}
                            <strong>{claim.object}</strong>
                          </p>
                          {claim.confidence !== undefined ? (
                            <small>{confidenceLabel(claim.confidence)}</small>
                          ) : null}
                        </div>
                        {claim.evidence[0] ? (
                          <blockquote>
                            “{claim.evidence[0].quote}”
                            <cite>{claim.evidence[0].section || claim.evidence[0].ref}</cite>
                          </blockquote>
                        ) : null}
                      </article>
                    ))}
                    {selectedCard.claims.length === 0 ? (
                      <span className={styles.empty}>暂无可验证声明。</span>
                    ) : null}
                  </div>
                </section>

                <section className={styles.section}>
                  <div className={styles.sectionTitle}>
                    <h4>关联脉络</h4>
                    <span>{selectedRelations.length}</span>
                  </div>
                  <div className={styles.relationList}>
                    {selectedRelations.map((relation) => (
                      <div key={relation.id} className={styles.relation}>
                        <span>{relation.relationType}</span>
                        <strong>{relation.targetLabel || relation.targetId}</strong>
                        {relation.confidence !== undefined ? (
                          <small>{confidenceLabel(relation.confidence)}</small>
                        ) : null}
                      </div>
                    ))}
                    {selectedRelations.length === 0 ? (
                      <span className={styles.empty}>暂无关联关系。</span>
                    ) : null}
                  </div>
                </section>
              </>
            ) : null}
          </div>
        </div>
      ) : null}

      {!isLoading && !snapshot?.cards.length && modelConfigured === true && readyPaperCount > 0 ? (
        <div className={styles.emptyState}>
          <strong>尚未生成文献地图</strong>
          <button
            type="button"
            className={styles.primaryButton}
            disabled={isBuilding}
            onClick={() => void handleBuild()}
          >
            {actionLabel(snapshot, isBuilding)}
          </button>
        </div>
      ) : null}

      {isLoading ? <div className={styles.loading}>正在加载文献地图...</div> : null}
    </section>
  );
}
