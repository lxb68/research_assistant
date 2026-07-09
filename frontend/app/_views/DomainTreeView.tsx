"use client";

import { useEffect, useMemo, useState } from "react";
import { buildApiUrl } from "@/lib/api";
import { SavedPaper } from "@/lib/papers";

type DomainTreeNode = {
  label: string;
  child?: DomainTreeNode[];
};

type GraphNode = {
  id: string;
  name: string;
  type: string;
};

type GraphEdge = {
  source: string;
  target: string;
  relation: string;
};

type DomainTreeResult = {
  projectId: string;
  generatedAt?: string;
  action?: string;
  language?: string;
  documentCount?: number;
  domainTree: DomainTreeNode[];
  knowledgeGraph?: {
    nodes?: GraphNode[];
    edges?: GraphEdge[];
  };
  manifest?: {
    documents?: Array<{
      recordId: string;
      title: string;
      markdownPath?: string;
      tocEntryCount?: number;
    }>;
  };
  catalogText?: string;
};

type ModelConfigStatus = {
  configured?: boolean;
  model?: string;
  baseUrl?: string;
  maskedApiKey?: string;
};

type DomainTreePageProps = {
  embedded?: boolean;
  isActiveView?: boolean;
  onOpenSettings?: () => void;
};

function renderTree(nodes: DomainTreeNode[], parentKey = "root"): React.ReactNode {
  return (
    <div className="domain-tree-node-list">
      {nodes.map((node, index) => {
        const key = `${parentKey}-${index}-${node.label}`;
        return (
          <article key={key} className="domain-tree-node-card">
            <div className="domain-tree-node-label">{node.label}</div>
            {node.child && node.child.length > 0 ? renderTree(node.child, key) : null}
          </article>
        );
      })}
    </div>
  );
}

export default function DomainTreePage({
  embedded = false,
  isActiveView = true,
  onOpenSettings,
}: DomainTreePageProps = {}) {
  const [papers, setPapers] = useState<SavedPaper[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [result, setResult] = useState<DomainTreeResult | null>(null);
  const [isLoadingPapers, setIsLoadingPapers] = useState(true);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isLoadingExisting, setIsLoadingExisting] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [modelStatus, setModelStatus] = useState<ModelConfigStatus | null>(null);
  const [isLoadingModelStatus, setIsLoadingModelStatus] = useState(true);

  const markdownReadyPapers = useMemo(
    () => papers.filter((paper) => Boolean(paper.id && (paper.markdownPath || paper.markdownOutputDir))),
    [papers],
  );

  const selectedPaper = useMemo(
    () => markdownReadyPapers.find((paper) => paper.id === selectedProjectId) ?? null,
    [markdownReadyPapers, selectedProjectId],
  );

  const graphStats = useMemo(() => {
    const nodes = result?.knowledgeGraph?.nodes ?? [];
    const edges = result?.knowledgeGraph?.edges ?? [];
    const typeSummary = nodes.reduce<Record<string, number>>((summary, node) => {
      summary[node.type] = (summary[node.type] ?? 0) + 1;
      return summary;
    }, {});

    return {
      nodeCount: nodes.length,
      edgeCount: edges.length,
      typeSummary,
      sampleEdges: edges.slice(0, 12),
    };
  }, [result]);

  useEffect(() => {
    let cancelled = false;

    async function loadModelStatus() {
      setIsLoadingModelStatus(true);
      try {
        const response = await fetch(buildApiUrl("/api/settings/model-config"));
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || "加载模型配置失败");
        }
        if (!cancelled) {
          setModelStatus(payload);
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "加载模型配置失败");
        }
      } finally {
        if (!cancelled) {
          setIsLoadingModelStatus(false);
        }
      }
    }

    if (embedded && !isActiveView) {
      return;
    }

    void loadModelStatus();
    return () => {
      cancelled = true;
    };
  }, [embedded, isActiveView]);

  useEffect(() => {
    let cancelled = false;

    async function loadPapers() {
      setIsLoadingPapers(true);
      setError("");

      try {
        const url = buildApiUrl("/api/papers");
        url.searchParams.set("limit", "200");
        const response = await fetch(url);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || "加载论文列表失败");
        }

        if (cancelled) {
          return;
        }

        const nextPapers: SavedPaper[] = payload.papers ?? [];
        setPapers(nextPapers);

        const firstMarkdownReady = nextPapers.find(
          (paper) => paper.id && (paper.markdownPath || paper.markdownOutputDir),
        );
        setSelectedProjectId((current) => current || firstMarkdownReady?.id || "");
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "加载论文列表失败");
        }
      } finally {
        if (!cancelled) {
          setIsLoadingPapers(false);
        }
      }
    }

    if (embedded && !isActiveView) {
      return;
    }

    void loadPapers();
    return () => {
      cancelled = true;
    };
  }, [embedded, isActiveView]);

  useEffect(() => {
    let cancelled = false;

    async function loadExistingResult(projectId: string) {
      setIsLoadingExisting(true);
      setError("");

      try {
        const response = await fetch(buildApiUrl(`/api/domain-tree/${encodeURIComponent(projectId)}`));
        const payload = await response.json().catch(() => ({}));

        if (response.status === 404) {
          if (!cancelled) {
            setResult(null);
            setStatus("当前论文还没有生成领域树，点击下方按钮即可开始。");
          }
          return;
        }

        if (!response.ok) {
          throw new Error(payload.detail || "读取已有领域树失败");
        }

        if (!cancelled) {
          setResult(payload);
          setStatus("已加载已有领域树结果。");
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "读取已有领域树失败");
        }
      } finally {
        if (!cancelled) {
          setIsLoadingExisting(false);
        }
      }
    }

    if (!selectedProjectId) {
      return;
    }

    void loadExistingResult(selectedProjectId);
    return () => {
      cancelled = true;
    };
  }, [selectedProjectId]);

  async function handleGenerate() {
    if (!selectedProjectId || isGenerating) {
      return;
    }

    if (!modelStatus?.configured) {
      setError("请先配置模型参数");
      setStatus("");
      return;
    }

    setIsGenerating(true);
    setError("");
    setStatus("正在根据 Markdown 目录与提示词生成领域树和知识图谱...");

    try {
      const response = await fetch(buildApiUrl("/api/domain-tree/generate"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          project_id: selectedProjectId,
          action: "rebuild",
          language: "中文",
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "生成领域树失败");
      }

      setResult(payload);
      setStatus("领域树与知识图谱生成完成。");
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : "生成领域树失败");
    } finally {
      setIsGenerating(false);
    }
  }

  return (
    <main className="domain-tree-page">
      <section className="domain-tree-panel">
        <header className="domain-tree-header">
          <div className="domain-tree-stats">
            <span>{markdownReadyPapers.length} 篇可生成</span>
            <span>{graphStats.nodeCount} 个图节点</span>
            <span>{graphStats.edgeCount} 条关系</span>
          </div>
        </header>

        <section className="domain-tree-selected-paper">
          <div>
            <strong>模型配置</strong>
            <p>
              {isLoadingModelStatus
                ? "正在读取模型参数..."
                : modelStatus?.configured
                  ? `${modelStatus.model || "未命名模型"} · ${modelStatus.maskedApiKey || "已配置密钥"}`
                  : "尚未配置模型参数"}
            </p>
          </div>
          <span>{modelStatus?.configured ? "可直接生成" : "请先完成设置"}</span>
        </section>

        {!modelStatus?.configured && !isLoadingModelStatus ? (
          <div className="domain-tree-empty">
            <strong>请先设置模型参数</strong>
            <span>在设置页面填写模型名称、Base URL 和 API Key 后，才可进行领域树构建。</span>
            {onOpenSettings ? (
              <button type="button" className="domain-tree-inline-button" onClick={onOpenSettings}>
                前往设置页面
              </button>
            ) : null}
          </div>
        ) : null}

        <section className="domain-tree-controls">
          <label className="domain-tree-select-wrap">
            <span>选择论文</span>
            <select
              value={selectedProjectId}
              onChange={(event) => {
                const nextProjectId = event.target.value;
                setSelectedProjectId(nextProjectId);
                if (!nextProjectId) {
                  setResult(null);
                  setStatus("");
                }
              }}
              disabled={isLoadingPapers || markdownReadyPapers.length === 0}
            >
              <option value="">请选择已生成 Markdown 的论文</option>
              {markdownReadyPapers.map((paper) => (
                <option key={paper.id} value={paper.id}>
                  {paper.title || paper.id}
                </option>
              ))}
            </select>
          </label>

          <button
            type="button"
            className="domain-tree-generate-button"
            disabled={!selectedProjectId || isGenerating || !modelStatus?.configured}
            onClick={() => {
              void handleGenerate();
            }}
          >
            {isGenerating ? "生成中..." : "生成领域树"}
          </button>
        </section>

        {selectedPaper ? (
          <section className="domain-tree-selected-paper">
            <div>
              <strong>{selectedPaper.title || selectedPaper.id}</strong>
              <p>
                {selectedPaper.source || "未知来源"}
                {selectedPaper.year ? ` · ${selectedPaper.year}` : ""}
                {selectedPaper.keyword ? ` · ${selectedPaper.keyword}` : ""}
              </p>
            </div>
            <span>{selectedPaper.markdownPath ? "Markdown 已就绪" : "目录结果已就绪"}</span>
          </section>
        ) : null}

        {status ? <div className="domain-tree-status">{status}</div> : null}
        {error ? (
          <div className="domain-tree-error">
            <span>{error}</span>
            {!modelStatus?.configured && onOpenSettings ? (
              <button type="button" className="domain-tree-inline-button" onClick={onOpenSettings}>
                去设置
              </button>
            ) : null}
          </div>
        ) : null}

        {isLoadingPapers || isLoadingExisting ? (
          <div className="domain-tree-empty">
            <strong>正在准备数据...</strong>
            <span>正在读取论文列表或已有领域树结果。</span>
          </div>
        ) : markdownReadyPapers.length === 0 ? (
          <div className="domain-tree-empty">
            <strong>暂无可生成领域树的论文</strong>
            <span>请先在“浏览数据集”中完成 PDF 转 Markdown，再回来生成。</span>
          </div>
        ) : !result ? (
          <div className="domain-tree-empty">
            <strong>尚未生成领域树</strong>
            <span>选中一篇已解析论文后，点击“生成领域树”开始处理。</span>
          </div>
        ) : (
          <div className="domain-tree-results">
            <section className="domain-tree-card domain-tree-tree-card">
              <div className="domain-tree-card-head">
                <div>
                  <p>领域树</p>
                  <h2>{result.projectId}</h2>
                </div>
                <div className="domain-tree-meta">
                  <span>{result.documentCount ?? 0} 篇文档</span>
                  <span>{result.generatedAt ? new Date(result.generatedAt).toLocaleString() : "刚刚生成"}</span>
                </div>
              </div>
              {renderTree(result.domainTree)}
            </section>

            <section className="domain-tree-side-column">
              <article className="domain-tree-card">
                <div className="domain-tree-card-head">
                  <div>
                    <p>知识图谱</p>
                    <h2>结构概览</h2>
                  </div>
                </div>
                <div className="domain-tree-graph-summary">
                  {Object.entries(graphStats.typeSummary).map(([type, count]) => (
                    <div key={type} className="domain-tree-chip">
                      <strong>{count}</strong>
                      <span>{type}</span>
                    </div>
                  ))}
                </div>
                <div className="domain-tree-edge-list">
                  {graphStats.sampleEdges.map((edge, index) => (
                    <div key={`${edge.source}-${edge.target}-${index}`} className="domain-tree-edge-item">
                      <strong>{edge.relation}</strong>
                      <span>{edge.source}</span>
                      <span>{edge.target}</span>
                    </div>
                  ))}
                </div>
              </article>

              <article className="domain-tree-card">
                <div className="domain-tree-card-head">
                  <div>
                    <p>输入目录</p>
                    <h2>Catalog 预览</h2>
                  </div>
                </div>
                <pre className="domain-tree-catalog-preview">{result.catalogText || "暂无目录内容"}</pre>
              </article>
            </section>
          </div>
        )}
      </section>
    </main>
  );
}
