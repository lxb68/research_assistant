/* 生成、读取和修订领域树，并将树节点关联到论文证据片段。 */

"use client";

import { useEffect, useMemo, useState } from "react";
import { buildApiUrl } from "@/lib/api";
import { SplitChunk, SavedPaper } from "@/lib/papers";
import { WORKSPACE_DOMAIN_TREE_PROJECT_ID } from "@/lib/constants";
import { useProjects } from "@/app/_components/ProjectProvider";
import { DomainTreePanel } from "@/app/_views/project-knowledge/DomainTreePanel";
import { KnowledgeGraphPanel } from "@/app/_views/project-knowledge/KnowledgeGraphPanel";
import { ProjectLiteraturePanel } from "@/app/_views/project-knowledge/ProjectLiteraturePanel";

type DomainTreeNode = {
  label: string;
  child?: DomainTreeNode[];
};

type GraphNode = {
  id: string;
  name: string;
  type: string;
  entityType?: string;
  aliases?: string[];
  attributes?: SemanticAttribute[];
  evidenceIds?: string[];
};

type GraphEdge = {
  source: string;
  target: string;
  relation: string;
  predicate?: string;
  relationType?: string;
  confidence?: number;
  evidenceIds?: string[];
};

type SemanticAttribute = {
  name: string;
  value: string;
  unit?: string;
  evidenceId?: string;
};

type SemanticEntity = {
  id: string;
  name: string;
  type: string;
  aliases?: string[];
  attributes?: SemanticAttribute[];
  evidenceIds?: string[];
  documentIds?: string[];
};

type SemanticRelation = {
  id: string;
  source: string;
  target: string;
  predicate: string;
  relationType: "general" | "causal" | "comparison" | "experimental" | "property" | string;
  confidence: number;
  evidenceIds?: string[];
  documentIds?: string[];
};

type SemanticEvidence = {
  id: string;
  documentId: string;
  section?: string;
  chunkIndex?: number;
  lineStart?: number;
  quote: string;
  kind?: string;
};

type CitationContext = {
  section?: string;
  lineStart?: number;
  quote: string;
};

type GraphCitation = {
  id: string;
  documentId: string;
  referenceNumber: number;
  marker: string;
  title: string;
  rawReference: string;
  year?: number | null;
  doi?: string;
  url?: string;
  matchedDocumentId?: string;
  contexts?: CitationContext[];
};

type SemanticExtractionStats = {
  mode?: string;
  documentCount?: number;
  processedChunkCount?: number;
  failedChunkCount?: number;
  entityCount?: number;
  semanticRelationCount?: number;
  citationCount?: number;
  evidenceCount?: number;
};

type DomainTreeManifestDocument = {
  recordId: string;
  title: string;
  markdownPath?: string;
  markdownDir?: string;
  tocEntryCount?: number;
  catalogText?: string;
};

type DomainTreeResult = {
  projectId: string;
  generatedAt?: string;
  action?: string;
  language?: string;
  requestedLanguage?: string;
  graphStatus?: "building" | "ready" | "failed" | "cancelled" | string;
  documentCount?: number;
  generationMode?: "llm" | "heuristic" | "unknown";
  degraded?: boolean;
  degradeReason?: string;
  warnings?: string[];
  domainTree: DomainTreeNode[];
  knowledgeGraph?: {
    nodes?: GraphNode[];
    edges?: GraphEdge[];
    entities?: SemanticEntity[];
    semanticRelations?: SemanticRelation[];
    evidence?: SemanticEvidence[];
    citations?: GraphCitation[];
    extraction?: SemanticExtractionStats;
  };
  manifest?: {
    documents?: DomainTreeManifestDocument[];
  };
  catalogText?: string;
};

type ModelConfigStatus = {
  configured?: boolean;
  model?: string;
  baseUrl?: string;
  maskedApiKey?: string;
};

type DomainTreeAction = "revise" | "rebuild" | "keep";
type DomainTreeViewMode = "project" | "tree" | "graph";
type DomainTreeLanguage = "auto" | "中文" | "English";

type DomainTreeJob = {
  jobId: string;
  projectId: string;
  action: DomainTreeAction;
  status: "queued" | "running" | "cancelling" | "completed" | "failed" | "cancelled" | "interrupted";
  stage: string;
  message: string;
  progress?: {
    documentCount?: number;
    totalChunks?: number;
    currentChunk?: number;
    completedChunks?: number;
    processedChunks?: number;
    failedChunks?: number;
    retryAttempt?: number;
    cacheHits?: number;
    cacheMisses?: number;
    maxWorkers?: number;
    domainTreeReady?: boolean;
    generationMode?: "llm" | "heuristic";
    degraded?: boolean;
    degradeReason?: string;
  };
  partialResult?: DomainTreeResult | null;
  result?: DomainTreeResult | null;
  error?: string;
};

type DomainTreePageProps = {
  embedded?: boolean;
  isActiveView?: boolean;
  onOpenSettings?: () => void;
};

type PaperDetail = SavedPaper & {
  splitChunks?: SplitChunk[];
};

type ChunkMatch = {
  paperId: string;
  paperTitle: string;
  score: number;
  chunk: SplitChunk;
};

type ReadableGraphDomain = {
  id: string;
  name: string;
  subdomains: string[];
  topics: string[];
  documents: string[];
};

type ReadableGraphDocument = {
  id: string;
  title: string;
  domains: string[];
  topics: string[];
  sections: string[];
};

const RELATION_TYPE_LABELS: Record<string, string> = {
  general: "一般关系",
  causal: "因果关系",
  comparison: "比较关系",
  experimental: "实验关系",
  property: "属性关系",
};

const EXCLUDED_CHUNK_CATEGORIES = new Set(["references", "front_matter", "back_matter"]);

const ACTION_LABELS: Record<DomainTreeAction, string> = {
  revise: "修改领域树",
  rebuild: "重建领域树",
  keep: "保持不变",
};

const ACTION_DESCRIPTIONS: Record<DomainTreeAction, string> = {
  revise: "根据新增或删除的文档调整当前领域树，只影响发生变更的部分。",
  rebuild: "基于当前全部文档重新生成一棵全新的领域树。",
  keep: "继续沿用当前领域树结构，不根据本次文献变化做任何修改。",
};

const LANGUAGE_OPTIONS: Array<{
  value: DomainTreeLanguage;
  label: string;
  description: string;
}> = [
  {
    value: "auto",
    label: "跟随文献语言（推荐）",
    description: "自动判断当前文献集合的主要语言。",
  },
  { value: "中文", label: "中文", description: "领域树标签使用中文。" },
  { value: "English", label: "English", description: "Domain tree labels use English." },
];

/** 规范化领域树标签以便匹配。 */
function cleanLabel(label: string) {
  return label.replace(/^\d+(?:\.\d+)*\s*/, "").trim();
}

/** 把领域标签拆为可检索词元。 */
function tokenizeLabel(label: string) {
  return cleanLabel(label)
    .toLowerCase()
    .split(/[^a-z0-9\u4e00-\u9fa5+#.-]+/i)
    .map((token) => token.trim())
    .filter((token) => token.length >= 2);
}

/** 汇总分块中可参与检索的文本。 */
function buildChunkSearchText(chunk: SplitChunk) {
  const headings = (chunk.headings ?? [])
    .map((heading) => heading.heading?.trim() || "")
    .filter(Boolean)
    .join(" ");
  const paragraphSummaries = (chunk.paragraphSummaries ?? [])
    .map((item) => item.summary?.trim() || "")
    .filter(Boolean)
    .join(" ");
  return `${headings}\n${chunk.summary || ""}\n${paragraphSummaries}\n${chunk.content || ""}`.toLowerCase();
}

/** 去除空字符串和重复字符串。 */
function uniqueStrings(values: string[]) {
  return Array.from(new Set(values.filter(Boolean)));
}

/** 计算领域标签与论文分块的匹配得分。 */
function scoreChunk(label: string, chunk: SplitChunk) {
  // 标签短语命中优先于分词命中，标题命中再给予额外权重。
  const normalizedLabel = cleanLabel(label).toLowerCase();
  const tokens = tokenizeLabel(label);
  if (!normalizedLabel || tokens.length === 0) {
    return 0;
  }

  const headingsText = (chunk.headings ?? [])
    .map((heading) => heading.heading?.trim() || "")
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  const summaryText = `${chunk.summary || ""} ${
    (chunk.paragraphSummaries ?? [])
      .map((item) => item.summary?.trim() || "")
      .filter(Boolean)
      .join(" ")
  }`.toLowerCase();
  const searchText = buildChunkSearchText(chunk);

  let score = 0;
  if (headingsText.includes(normalizedLabel)) {
    score += 8;
  }
  if (summaryText.includes(normalizedLabel)) {
    score += 5;
  }
  if (searchText.includes(normalizedLabel)) {
    score += 3;
  }

  for (const token of tokens) {
    if (headingsText.includes(token)) {
      score += 3;
      continue;
    }
    if (summaryText.includes(token)) {
      score += 2;
      continue;
    }
    if (searchText.includes(token)) {
      score += 1;
    }
  }

  return score;
}

/** 递归渲染领域树节点。 */
function renderTree(
  nodes: DomainTreeNode[],
  options: {
    selectedKey: string;
    onSelectSecondary: (key: string, label: string) => void;
  },
  parentKey = "root",
): React.ReactNode {
  // 递归渲染任意深度的领域树，同时保持稳定节点路径。
  return (
    <div className="domain-tree-node-list">
      {nodes.map((node, index) => {
        const key = `${parentKey}-${index}-${node.label}`;
        return (
          <article key={key} className="domain-tree-node-card">
            <div className="domain-tree-node-label">{node.label}</div>
            {node.child && node.child.length > 0 ? (
              <div className="domain-tree-secondary-list">
                {node.child.map((child, childIndex) => {
                  const childKey = `${key}-child-${childIndex}-${child.label}`;
                  return (
                    <button
                      key={childKey}
                      type="button"
                      className={`domain-tree-secondary-button${
                        options.selectedKey === childKey ? " is-active" : ""
                      }`}
                      onClick={() => options.onSelectSecondary(childKey, child.label)}
                    >
                      {child.label}
                    </button>
                  );
                })}
              </div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

/** 管理领域树生成、修订和证据关联。 */
export default function DomainTreePage(props: DomainTreePageProps = {}) {
  const { activeProjectId } = useProjects();
  return <DomainTreeProjectPage key={activeProjectId} {...props} />;
}

/** 每次切换项目时重新挂载，避免旧项目的任务和图谱状态残留。 */
function DomainTreeProjectPage({
  embedded = false,
  isActiveView = true,
  onOpenSettings,
}: DomainTreePageProps) {
  const {
    projects,
    activeProjectId,
    isLoadingProjects,
    projectError,
    selectProject,
    createProject,
    refreshProjects,
  } = useProjects();
  const [papers, setPapers] = useState<SavedPaper[]>([]);
  const [availablePapers, setAvailablePapers] = useState<SavedPaper[]>([]);
  const [memberDraftIds, setMemberDraftIds] = useState<string[]>([]);
  const [isEditingMembers, setIsEditingMembers] = useState(false);
  const [isSavingMembers, setIsSavingMembers] = useState(false);
  const [sourceProjectId, setSourceProjectId] = useState(WORKSPACE_DOMAIN_TREE_PROJECT_ID);
  const [isLoadingSourcePapers, setIsLoadingSourcePapers] = useState(false);
  const [isCreateProjectOpen, setIsCreateProjectOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [isCreatingProject, setIsCreatingProject] = useState(false);
  const [paperDetails, setPaperDetails] = useState<Record<string, PaperDetail>>({});
  const [result, setResult] = useState<DomainTreeResult | null>(null);
  const [isLoadingPapers, setIsLoadingPapers] = useState(true);
  const [isGenerating, setIsGenerating] = useState(false);
  const [activeJobId, setActiveJobId] = useState("");
  const [activeJob, setActiveJob] = useState<DomainTreeJob | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isLoadingExisting, setIsLoadingExisting] = useState(false);
  const [isLoadingChunks, setIsLoadingChunks] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [chunkError, setChunkError] = useState("");
  const [modelStatus, setModelStatus] = useState<ModelConfigStatus | null>(null);
  const [isLoadingModelStatus, setIsLoadingModelStatus] = useState(true);
  const [manualGenerationMode, setManualGenerationMode] = useState<DomainTreeAction | null>(null);
  const [generationLanguage, setGenerationLanguage] = useState<DomainTreeLanguage>("auto");
  const [viewMode, setViewMode] = useState<DomainTreeViewMode>("project");
  const [selectedSecondaryKey, setSelectedSecondaryKey] = useState("");
  const [selectedSecondaryLabel, setSelectedSecondaryLabel] = useState("");
  const [matchedChunks, setMatchedChunks] = useState<ChunkMatch[]>([]);

  const markdownReadyPapers = useMemo(
    () => papers.filter((paper) => Boolean(paper.id && (paper.markdownPath || paper.markdownOutputDir))),
    [papers],
  );

  const existingDocuments = useMemo(() => result?.manifest?.documents ?? [], [result]);

  const currentDocumentMap = useMemo(() => {
    // flatMap 显式保留二元组类型，同时过滤没有记录 ID 的论文。
    const entries = markdownReadyPapers.flatMap((paper): Array<[string, SavedPaper]> =>
      paper.id ? [[paper.id, paper]] : [],
    );
    return new Map<string, SavedPaper>(entries);
  }, [markdownReadyPapers]);

  const existingDocumentMap = useMemo(() => {
    return new Map(existingDocuments.map((document) => [document.recordId, document]));
  }, [existingDocuments]);

  const changeSummary = useMemo(() => {
    const added = markdownReadyPapers.filter((paper) => paper.id && !existingDocumentMap.has(paper.id));
    const removed = existingDocuments.filter((document) => !currentDocumentMap.has(document.recordId));
    return {
      added,
      removed,
      hasChanges: added.length > 0 || removed.length > 0,
    };
  }, [currentDocumentMap, existingDocumentMap, existingDocuments, markdownReadyPapers]);

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
      sampleEdges: edges.slice(0, 24),
    };
  }, [result]);

  const readableGraph = useMemo(() => {
    const nodes = result?.knowledgeGraph?.nodes ?? [];
    const edges = result?.knowledgeGraph?.edges ?? [];
    const nodeMap = new Map(nodes.map((node) => [node.id, node]));
    const edgesByRelation = edges.reduce<Record<string, GraphEdge[]>>((grouped, edge) => {
      const key = edge.relation || "unknown";
      grouped[key] = grouped[key] ?? [];
      grouped[key].push(edge);
      return grouped;
    }, {});

    const domainTopicMap = new Map<string, string[]>();
    for (const edge of edgesByRelation.covers_topic ?? []) {
      const topicName = nodeMap.get(edge.target)?.name?.trim() || "";
      if (!topicName) {
        continue;
      }
      const current = domainTopicMap.get(edge.source) ?? [];
      current.push(topicName);
      domainTopicMap.set(edge.source, uniqueStrings(current));
    }

    const documentTopicMap = new Map<string, string[]>();
    for (const edge of edgesByRelation.mentions_topic ?? []) {
      const topicName = nodeMap.get(edge.target)?.name?.trim() || "";
      if (!topicName) {
        continue;
      }
      const current = documentTopicMap.get(edge.source) ?? [];
      current.push(topicName);
      documentTopicMap.set(edge.source, uniqueStrings(current));
    }

    const documentSectionMap = new Map<string, string[]>();
    for (const edge of edgesByRelation.has_section ?? []) {
      const sectionName = nodeMap.get(edge.target)?.name?.trim() || "";
      if (!sectionName) {
        continue;
      }
      const current = documentSectionMap.get(edge.source) ?? [];
      current.push(sectionName);
      documentSectionMap.set(edge.source, uniqueStrings(current));
    }

    const readableDomains: ReadableGraphDomain[] = (edgesByRelation.has_domain ?? [])
      .map((edge) => {
        const domainNode = nodeMap.get(edge.target);
        if (!domainNode?.name?.trim()) {
          return null;
        }
        const subdomains = (edgesByRelation.has_subdomain ?? [])
          .filter((candidate) => candidate.source === edge.target)
          .map((candidate) => nodeMap.get(candidate.target)?.name?.trim() || "")
          .filter(Boolean);
        const topics = domainTopicMap.get(edge.target) ?? [];
        const documents = uniqueStrings(
          (edgesByRelation.contains_document ?? [])
            .map((candidate) => candidate.target)
            .filter(Boolean)
            .map((documentId) => {
              const documentNode = nodeMap.get(documentId);
              const documentTopics = documentTopicMap.get(documentId) ?? [];
              const hasSharedTopic = documentTopics.some((topic) => topics.includes(topic));
              return hasSharedTopic ? documentNode?.name?.trim() || "" : "";
            }),
        );

        return {
          id: edge.target,
          name: domainNode.name.trim(),
          subdomains: uniqueStrings(subdomains),
          topics: uniqueStrings(topics),
          documents,
        } satisfies ReadableGraphDomain;
      })
      .filter((item): item is ReadableGraphDomain => Boolean(item))
      .sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));

    const domainTopicLookup = new Map<string, string[]>();
    for (const domain of readableDomains) {
      domainTopicLookup.set(domain.id, domain.topics);
    }

    const readableDocuments: ReadableGraphDocument[] = (edgesByRelation.contains_document ?? [])
      .map((edge) => {
        const documentNode = nodeMap.get(edge.target);
        if (!documentNode?.name?.trim()) {
          return null;
        }
        const topics = documentTopicMap.get(edge.target) ?? [];
        const sections = documentSectionMap.get(edge.target) ?? [];
        const domains = readableDomains
          .filter((domain) => topics.some((topic) => (domainTopicLookup.get(domain.id) ?? []).includes(topic)))
          .map((domain) => domain.name);

        return {
          id: edge.target,
          title: documentNode.name.trim(),
          domains: uniqueStrings(domains),
          topics: uniqueStrings(topics),
          sections: uniqueStrings(sections).slice(0, 8),
        } satisfies ReadableGraphDocument;
      })
      .filter((item): item is ReadableGraphDocument => Boolean(item))
      .sort((left, right) => left.title.localeCompare(right.title, "zh-CN"));

    const relationSummary = [
      { label: "项目包含领域", count: (edgesByRelation.has_domain ?? []).length },
      { label: "领域下子方向", count: (edgesByRelation.has_subdomain ?? []).length },
      { label: "文献主题关联", count: (edgesByRelation.mentions_topic ?? []).length },
      { label: "文献章节关联", count: (edgesByRelation.has_section ?? []).length },
      { label: "全文实体提及", count: (edgesByRelation.mentions_entity ?? []).length },
      { label: "全文语义关系", count: (edgesByRelation.semantic_relation ?? []).length },
      { label: "文献引用关系", count: (edgesByRelation.cites ?? []).length },
    ].filter((item) => item.count > 0);

    return {
      domains: readableDomains,
      documents: readableDocuments,
      relationSummary,
    };
  }, [result]);

  const semanticOverview = useMemo(() => {
    const graph = result?.knowledgeGraph;
    const entities = graph?.entities ?? [];
    const relations = graph?.semanticRelations ?? [];
    const evidence = graph?.evidence ?? [];
    const citations = graph?.citations ?? [];
    const entityMap = new Map(entities.map((entity) => [entity.id, entity]));
    const evidenceMap = new Map(evidence.map((item) => [item.id, item]));
    const documentTitleMap = new Map(
      (graph?.nodes ?? [])
        .filter((node) => node.type === "document")
        .map((node) => [node.id.replace(/^doc:/, ""), node.name]),
    );

    const readableRelations = relations.map((relation) => ({
      ...relation,
      sourceName: entityMap.get(relation.source)?.name || relation.source,
      targetName: entityMap.get(relation.target)?.name || relation.target,
      evidence: (relation.evidenceIds ?? [])
        .map((id) => evidenceMap.get(id))
        .filter((item): item is SemanticEvidence => Boolean(item)),
    }));
    const entityTypes = entities.reduce<Record<string, number>>((summary, entity) => {
      const type = entity.type || "未分类实体";
      summary[type] = (summary[type] ?? 0) + 1;
      return summary;
    }, {});

    return {
      entities,
      entityTypes,
      relations: readableRelations,
      citations: citations.map((citation) => ({
        ...citation,
        documentTitle: documentTitleMap.get(citation.documentId) || citation.documentId,
      })),
      extraction: graph?.extraction,
    };
  }, [result]);

  useEffect(() => {
    let cancelled = false;

    /** 读取当前模型配置状态。 */
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

    /** 同时加载全局论文目录和当前项目成员论文。 */
    async function loadPapers() {
      setIsLoadingPapers(true);
      setError("");

      try {
        const allUrl = buildApiUrl("/api/papers");
        allUrl.searchParams.set("limit", "500");
        const [allResponse, projectResponse] = await Promise.all([
          fetch(allUrl),
          fetch(buildApiUrl(`/api/projects/${encodeURIComponent(activeProjectId)}/papers`)),
        ]);
        const [allPayload, projectPayload] = await Promise.all([
          allResponse.json().catch(() => ({})),
          projectResponse.json().catch(() => ({})),
        ]);
        if (!allResponse.ok) throw new Error(allPayload.detail || "加载论文列表失败");
        if (!projectResponse.ok) throw new Error(projectPayload.detail || "加载项目论文失败");

        if (!cancelled) {
          const projectPapers = (projectPayload.papers ?? []) as SavedPaper[];
          setAvailablePapers(allPayload.papers ?? []);
          setPapers(projectPapers);
          setMemberDraftIds(projectPapers.flatMap((paper) => paper.id ? [paper.id] : []));
        }
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
  }, [activeProjectId, embedded, isActiveView]);

  useEffect(() => {
    let cancelled = false;

    /** 读取已有领域树结果。 */
    async function loadExistingResult() {
      setIsLoadingExisting(true);
      setError("");

      try {
        const response = await fetch(
          buildApiUrl(`/api/projects/${encodeURIComponent(activeProjectId)}/domain-tree`),
        );
        const payload = await response.json().catch(() => ({}));

        if (response.status === 404) {
          if (!cancelled) {
            setResult(null);
            setStatus("当前工作区还没有生成领域树，点击下方按钮即可开始。");
          }
          return;
        }

        if (!response.ok) {
          throw new Error(payload.detail || "读取已有领域树失败");
        }

        if (!cancelled) {
          setResult(payload);
          setStatus("已加载当前工作区的领域树结果。");
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

    if (embedded && !isActiveView) {
      return;
    }

    void loadExistingResult();
    return () => {
      cancelled = true;
    };
  }, [activeProjectId, embedded, isActiveView]);

  useEffect(() => {
    let cancelled = false;

    /** 页面重新打开时恢复同一项目仍在运行的领域树任务。 */
    async function discoverActiveJob() {
      try {
        const response = await fetch(
          buildApiUrl(
            `/api/projects/${encodeURIComponent(activeProjectId)}/domain-tree/jobs/active`,
          ),
        );
        if (response.status === 404) {
          return;
        }
        const payload = (await response.json().catch(() => ({}))) as DomainTreeJob & { detail?: string };
        if (!response.ok) {
          throw new Error(payload.detail || "读取领域树任务状态失败");
        }
        if (!cancelled) {
          setActiveJob(payload);
          setActiveJobId(payload.jobId);
          setIsGenerating(true);
          setStatus(payload.message || "领域树任务正在后台运行");
          if (payload.partialResult) {
            setResult(payload.partialResult);
          }
        }
      } catch (jobError) {
        if (!cancelled) {
          setError(jobError instanceof Error ? jobError.message : "读取领域树任务状态失败");
        }
      }
    }

    if (embedded && !isActiveView) {
      return;
    }
    void discoverActiveJob();
    return () => {
      cancelled = true;
    };
  }, [activeProjectId, embedded, isActiveView]);

  useEffect(() => {
    if (!activeJobId || (embedded && !isActiveView)) {
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    /** 轮询后台任务；终态到达后停止并应用最终结果。 */
    async function pollJob() {
      try {
        const response = await fetch(
          buildApiUrl(
            `/api/projects/${encodeURIComponent(activeProjectId)}/domain-tree/jobs/${encodeURIComponent(activeJobId)}`,
          ),
        );
        const payload = (await response.json().catch(() => ({}))) as DomainTreeJob & { detail?: string };
        if (!response.ok) {
          throw new Error(payload.detail || "读取领域树任务进度失败");
        }
        if (cancelled) {
          return;
        }

        setActiveJob(payload);
        setStatus(payload.message || "领域树任务正在后台运行");
        if (payload.progress?.domainTreeReady && payload.partialResult) {
          setResult((current) =>
            current?.generatedAt === payload.partialResult?.generatedAt
              ? current
              : payload.partialResult ?? current,
          );
        }
        if (payload.status === "completed") {
          if (payload.result) {
            setResult(payload.result);
          }
          setMatchedChunks([]);
          setSelectedSecondaryKey("");
          setSelectedSecondaryLabel("");
          setStatus(
            payload.result?.degraded
              ? `${ACTION_LABELS[payload.action]}已降级完成：模型生成失败，本次结果来自启发式规则。`
              : `${ACTION_LABELS[payload.action]}完成，领域树和知识图谱已更新。`,
          );
          setIsGenerating(false);
          setIsCancelling(false);
          setActiveJobId("");
          return;
        }
        if (payload.status === "failed") {
          setError(payload.error || "领域树生成失败");
          setIsGenerating(false);
          setIsCancelling(false);
          setActiveJobId("");
          return;
        }
        if (payload.status === "cancelled") {
          setStatus("领域树生成已取消。");
          setIsGenerating(false);
          setIsCancelling(false);
          setActiveJobId("");
          return;
        }
        if (payload.status === "interrupted") {
          setError(payload.error || "服务重启或任务心跳过期，领域树生成已中断");
          setIsGenerating(false);
          setIsCancelling(false);
          setActiveJobId("");
          return;
        }
        timer = setTimeout(() => {
          void pollJob();
        }, 1000);
      } catch (pollError) {
        if (!cancelled) {
          setError(pollError instanceof Error ? pollError.message : "读取领域树任务进度失败");
          timer = setTimeout(() => {
            void pollJob();
          }, 2000);
        }
      }
    }

    void pollJob();
    return () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
      }
    };
  }, [activeJobId, activeProjectId, embedded, isActiveView]);

  const generationMode = useMemo<DomainTreeAction>(() => {
    if (manualGenerationMode) {
      return manualGenerationMode;
    }
    if (!result) {
      return "rebuild";
    }
    if (changeSummary.hasChanges) {
      return "revise";
    }
    return "keep";
  }, [changeSummary.hasChanges, manualGenerationMode, result]);

  /** 提交领域树生成或修订任务。 */
  async function handleGenerate() {
    if (isGenerating) {
      return;
    }

    if (!modelStatus?.configured) {
      setError("请先配置模型参数");
      setStatus("");
      return;
    }

    if (markdownReadyPapers.length === 0) {
      setError("请先准备至少一篇已完成 Markdown 解析的论文");
      setStatus("");
      return;
    }

    setIsGenerating(true);
    setError("");
    setStatus(
      generationMode === "revise"
        ? "正在根据新增或删除的文献修订领域树..."
        : generationMode === "rebuild"
          ? "正在基于全部文献重建领域树和知识图谱..."
          : "正在保留当前领域树结构并刷新展示结果...",
    );

    try {
      const response = await fetch(
        buildApiUrl(`/api/projects/${encodeURIComponent(activeProjectId)}/domain-tree/generate`),
        {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          action: generationMode,
          language: generationLanguage,
        }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "生成领域树失败");
      }

      const job = payload as DomainTreeJob;
      setActiveJob(job);
      setActiveJobId(job.jobId);
      setStatus(job.message || "领域树任务已进入后台队列");
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : "生成领域树失败");
      setIsGenerating(false);
    }
  }

  /** 请求取消后台任务；正在进行的单次模型请求结束后停止。 */
  async function handleCancelGeneration() {
    if (!activeJobId || isCancelling) {
      return;
    }
    setIsCancelling(true);
    setError("");
    try {
      const response = await fetch(
        buildApiUrl(
          `/api/projects/${encodeURIComponent(activeProjectId)}/domain-tree/jobs/${encodeURIComponent(activeJobId)}/cancel`,
        ),
        { method: "POST" },
      );
      const payload = (await response.json().catch(() => ({}))) as DomainTreeJob & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail || "取消领域树任务失败");
      }
      setActiveJob(payload);
      setStatus(payload.message || "正在取消领域树任务");
    } catch (cancelError) {
      setError(cancelError instanceof Error ? cancelError.message : "取消领域树任务失败");
      setIsCancelling(false);
    }
  }

  /** 创建空项目，论文成员由项目文献面板显式选择。 */
  async function handleCreateProject() {
    const name = newProjectName.trim();
    if (!name || isCreatingProject) return;
    setIsCreatingProject(true);
    setError("");
    try {
      await createProject(name);
      setNewProjectName("");
      setIsCreateProjectOpen(false);
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : "创建项目失败");
    } finally {
      setIsCreatingProject(false);
    }
  }

  /** 从指定来源项目加载可复用的论文成员。 */
  async function loadSourceProjectPapers(projectId: string) {
    if (!projectId) {
      setAvailablePapers([]);
      return;
    }
    setIsLoadingSourcePapers(true);
    setError("");
    try {
      const response = await fetch(
        buildApiUrl(`/api/projects/${encodeURIComponent(projectId)}/papers`),
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "加载来源项目论文失败");
      setAvailablePapers(payload.papers ?? []);
    } catch (loadError) {
      setAvailablePapers([]);
      setError(loadError instanceof Error ? loadError.message : "加载来源项目论文失败");
    } finally {
      setIsLoadingSourcePapers(false);
    }
  }

  /** 打开成员管理时默认展示默认项目，也允许切换到其他项目。 */
  async function handleToggleMemberEditor() {
    if (isEditingMembers) {
      setIsEditingMembers(false);
      return;
    }
    const sourceId = projects.some(
      (project) => project.id === sourceProjectId && project.id !== activeProjectId,
    )
      ? sourceProjectId
      : projects.find((project) => project.id !== activeProjectId)?.id || "";
    setSourceProjectId(sourceId);
    setIsEditingMembers(true);
    await loadSourceProjectPapers(sourceId);
  }

  /** 保存当前项目的完整论文成员集合。 */
  async function handleSaveProjectMembers() {
    if (activeProjectId === WORKSPACE_DOMAIN_TREE_PROJECT_ID) return;
    setIsSavingMembers(true);
    setError("");
    try {
      const response = await fetch(
        buildApiUrl(`/api/projects/${encodeURIComponent(activeProjectId)}/papers`),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paper_ids: memberDraftIds }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "保存项目论文失败");
      setPapers(payload.papers ?? []);
      setIsEditingMembers(false);
      setManualGenerationMode(null);
      setStatus("项目论文集合已更新，可修订或重建领域树与知识图谱。");
      await refreshProjects();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存项目论文失败");
    } finally {
      setIsSavingMembers(false);
    }
  }

  /** 补充加载领域树引用的论文详情。 */
  async function ensurePaperDetails(recordIds: string[]) {
    const missingIds = recordIds.filter((recordId) => !paperDetails[recordId]);
    if (missingIds.length === 0) {
      return paperDetails;
    }

    const responses = await Promise.all(
      missingIds.map(async (recordId) => {
        const response = await fetch(buildApiUrl(`/api/papers/${encodeURIComponent(recordId)}`));
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || `读取论文详情失败：${recordId}`);
        }
        return [recordId, payload.paper as PaperDetail] as const;
      }),
    );

    const nextDetails = { ...paperDetails };
    for (const [recordId, paper] of responses) {
      nextDetails[recordId] = paper;
    }
    setPaperDetails(nextDetails);
    return nextDetails;
  }

  /** 选择二级领域并检索关联证据。 */
  async function handleSelectSecondary(key: string, label: string) {
    setSelectedSecondaryKey(key);
    setSelectedSecondaryLabel(label);
    setChunkError("");
    setMatchedChunks([]);

    const recordIds = (result?.manifest?.documents ?? []).map((document) => document.recordId).filter(Boolean);
    if (recordIds.length === 0) {
      setChunkError("当前领域树结果里没有可用于匹配分块的文献清单。");
      return;
    }

    setIsLoadingChunks(true);
    try {
      const details = await ensurePaperDetails(recordIds);
      const matches: ChunkMatch[] = [];

      for (const recordId of recordIds) {
        const paper = details[recordId];
        if (!paper) {
          continue;
        }
        for (const chunk of paper.splitChunks ?? []) {
          if (EXCLUDED_CHUNK_CATEGORIES.has((chunk.semanticCategory || "").trim().toLowerCase())) {
            continue;
          }
          const score = scoreChunk(label, chunk);
          if (score <= 0) {
            continue;
          }
          matches.push({
            paperId: recordId,
            paperTitle: paper.title || recordId,
            score,
            chunk,
          });
        }
      }

      matches.sort((left, right) => {
        if (right.score !== left.score) {
          return right.score - left.score;
        }
        return (right.chunk.charCount ?? 0) - (left.chunk.charCount ?? 0);
      });

      setMatchedChunks(matches.slice(0, 18));
      if (matches.length === 0) {
        setChunkError("没有找到和该二级标签明显相关的原始分块，请先确认论文已经完成文本切分。");
      }
    } catch (loadError) {
      setChunkError(loadError instanceof Error ? loadError.message : "加载原始分块失败");
    } finally {
      setIsLoadingChunks(false);
    }
  }

  const latestAction = (result?.action as DomainTreeAction | undefined) ?? "rebuild";
  const treeCardTitle = latestAction === "revise" ? "修订标签树" : "领域树";
  const isModelConfigurationMissing = modelStatus?.configured === false;

  return (
    <main className="domain-tree-page">
      <section className="domain-tree-panel">
        <div className="domain-tree-toolbar">
        <section
          className="domain-tree-view-switcher"
          aria-label="项目知识空间视图切换"
          style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}
        >
          <button
            type="button"
            className={`domain-tree-view-button${viewMode === "project" ? " is-active" : ""}`}
            onClick={() => setViewMode("project")}
          >
            项目文献
          </button>
          <button
            type="button"
            className={`domain-tree-view-button${viewMode === "tree" ? " is-active" : ""}`}
            onClick={() => setViewMode("tree")}
          >
            领域树
          </button>
          <button
            type="button"
            className={`domain-tree-view-button${viewMode === "graph" ? " is-active" : ""}`}
            onClick={() => setViewMode("graph")}
          >
            知识图谱
          </button>
        </section>
          <div className="domain-tree-stats" aria-label="当前项目分析统计">
            <span>{markdownReadyPapers.length} 篇文献</span>
            <span>{graphStats.nodeCount} 个节点</span>
            <span>{graphStats.edgeCount} 条关系</span>
          </div>
        </div>

        {viewMode === "project" ? (
          <ProjectLiteraturePanel
            projects={projects}
            activeProjectId={activeProjectId}
            projectError={projectError}
            isLoadingProjects={isLoadingProjects}
            isGenerating={isGenerating}
            isCreateProjectOpen={isCreateProjectOpen}
            newProjectName={newProjectName}
            isCreatingProject={isCreatingProject}
            isEditingMembers={isEditingMembers}
            sourceProjectId={sourceProjectId}
            isLoadingSourcePapers={isLoadingSourcePapers}
            isSavingMembers={isSavingMembers}
            availablePapers={availablePapers}
            memberDraftIds={memberDraftIds}
            onSelectProject={selectProject}
            onToggleCreateProject={() => setIsCreateProjectOpen((current) => !current)}
            onNewProjectNameChange={setNewProjectName}
            onCreateProject={() => void handleCreateProject()}
            onCancelCreateProject={() => {
              setIsCreateProjectOpen(false);
              setNewProjectName("");
            }}
            onToggleMemberEditor={() => void handleToggleMemberEditor()}
            onSourceProjectChange={(projectId) => {
              setSourceProjectId(projectId);
              void loadSourceProjectPapers(projectId);
            }}
            onTogglePaper={(paperId, checked) => setMemberDraftIds((current) =>
              checked ? Array.from(new Set([...current, paperId])) : current.filter((value) => value !== paperId),
            )}
            onSelectAllSourcePapers={() => setMemberDraftIds((current) => Array.from(new Set([
              ...current,
              ...availablePapers.flatMap((paper) => paper.id ? [paper.id] : []),
            ])))}
            onClearSourcePapers={() => {
              const sourceIds = new Set(availablePapers.flatMap((paper) => paper.id ? [paper.id] : []));
              setMemberDraftIds((current) => current.filter((paperId) => !sourceIds.has(paperId)));
            }}
            onSaveMembers={() => void handleSaveProjectMembers()}
          />
        ) : null}

        {viewMode !== "project" ? (
          <>

        {isModelConfigurationMissing && !isLoadingModelStatus ? (
          <div className="domain-tree-empty domain-tree-config-empty">
            <strong>请先设置模型参数</strong>
            <span>在设置页面填写模型名称、Base URL 和 API Key 后，才可进行领域树构建。</span>
            {onOpenSettings ? (
              <button type="button" className="domain-tree-inline-button" onClick={onOpenSettings}>
                前往设置页面
              </button>
            ) : null}
          </div>
        ) : null}

        <section className="domain-tree-selected-paper domain-tree-literature-card">
          <div>
            <strong>当前文献集合</strong>
            <p>
              本次将基于 {markdownReadyPapers.length} 篇已完成 Markdown 解析的文献进行分析，
              {result?.generatedAt ? ` 最近一次生成时间为 ${new Date(result.generatedAt).toLocaleString()}。` : " 当前还没有历史生成记录。"}
            </p>
          </div>
          <span>{result ? `最近方式：${ACTION_LABELS[latestAction]}` : "首次生成"}</span>
          {result ? (
            <div className="domain-tree-change-panel domain-tree-change-inline">
            <div className="domain-tree-change-head">
              <div>
                <strong>文献变更检测</strong>
                <p>
                  {changeSummary.hasChanges
                    ? `检测到 ${changeSummary.added.length} 篇新增文献、${changeSummary.removed.length} 篇已移除文献，请选择如何处理当前领域树。`
                    : "当前文献集合与上一次生成结果一致，可保持不变或主动重建。"}
                </p>
              </div>
              <span>{changeSummary.hasChanges ? "需要选择处理模式" : "未检测到变更"}</span>
            </div>

            {changeSummary.hasChanges ? (
              <div className="domain-tree-change-tags">
                  {changeSummary.added.map((paper) => (
                    <span key={`added-${paper.id}`} className="domain-tree-change-tag is-added">
                      新增：{paper.title || paper.id}
                    </span>
                  ))}
                  {changeSummary.removed.map((document) => (
                    <span key={`removed-${document.recordId}`} className="domain-tree-change-tag is-removed">
                      删除：{document.title || document.recordId}
                    </span>
                  ))}
              </div>
            ) : (
              <div className="domain-tree-status">当前没有新增或删除文献，直接查看现有领域树即可。</div>
            )}
            </div>
          ) : null}
        </section>

        <section className="domain-tree-controls">
          <div className="domain-tree-select-wrap">
            <span>本次执行模式</span>
            <div
              className="domain-tree-mode-grid"
              style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}
            >
              {(["revise", "rebuild", "keep"] as const).map((mode) => (
                <label
                  key={mode}
                  className={`domain-tree-mode-card${generationMode === mode ? " is-active" : ""}`}
                >
                  <input
                    type="radio"
                    name="domain-tree-mode"
                    value={mode}
                    checked={generationMode === mode}
                    onChange={() => setManualGenerationMode(mode)}
                  />
                  <strong>{ACTION_LABELS[mode]}</strong>
                  <span>{ACTION_DESCRIPTIONS[mode]}</span>
                </label>
              ))}
            </div>
            <label className="domain-tree-language-control">
              <span>领域树语言</span>
              <select
                aria-label="领域树语言"
                value={generationLanguage}
                onChange={(event) => setGenerationLanguage(event.target.value as DomainTreeLanguage)}
                disabled={isGenerating}
              >
                {LANGUAGE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <small>
                {LANGUAGE_OPTIONS.find((option) => option.value === generationLanguage)?.description}
                {result?.language ? ` 最近一次实际使用：${result.language}。` : ""}
              </small>
            </label>
          </div>

          <div className="domain-tree-action-buttons">
            <button
              type="button"
              className="domain-tree-generate-button"
              disabled={isGenerating || !modelStatus?.configured || markdownReadyPapers.length === 0}
              onClick={() => {
                void handleGenerate();
              }}
            >
              {isGenerating ? "后台处理中..." : result ? "更新领域树" : "生成领域树"}
            </button>
            {isGenerating && activeJobId ? (
              <button
                type="button"
                className="domain-tree-secondary-button"
                disabled={isCancelling || activeJob?.status === "cancelling"}
                onClick={() => {
                  void handleCancelGeneration();
                }}
              >
                {isCancelling || activeJob?.status === "cancelling" ? "正在取消..." : "取消任务"}
              </button>
            ) : null}
          </div>
        </section>

        {status ? <div className="domain-tree-status">{status}</div> : null}
        {result?.degraded ? (
          <div className="domain-tree-degraded-warning" role="status">
            <strong>当前展示的是降级结果</strong>
            <span>{result.warnings?.[0] || "模型生成失败，本次领域树由启发式规则生成。"}</span>
          </div>
        ) : null}
        {isGenerating && activeJob ? (
          <div className="domain-tree-job-progress" aria-live="polite">
            <div className="domain-tree-job-progress-head">
              <strong>{activeJob.message || "领域树任务正在运行"}</strong>
              <span>
                {activeJob.progress?.completedChunks ?? 0}/{activeJob.progress?.totalChunks ?? "?"} 分块
              </span>
            </div>
            <progress
              max={Math.max(1, activeJob.progress?.totalChunks ?? 1)}
              value={activeJob.progress?.completedChunks ?? 0}
            />
            <div className="domain-tree-meta">
              <span>{activeJob.progress?.processedChunks ?? 0} 个成功分块</span>
              <span>{activeJob.progress?.failedChunks ?? 0} 个失败分块</span>
              <span>{activeJob.progress?.cacheHits ?? 0} 个缓存命中</span>
              <span>{activeJob.progress?.cacheMisses ?? 0} 个待抽取分块</span>
              <span>{activeJob.progress?.maxWorkers ?? 4} 路并发</span>
              {activeJob.progress?.retryAttempt ? (
                <span>正在进行第 {activeJob.progress.retryAttempt} 次尝试</span>
              ) : null}
            </div>
          </div>
        ) : null}
        {error && !isModelConfigurationMissing ? (
          <div className="domain-tree-error">
            <span>{error}</span>
          </div>
        ) : null}

        {isLoadingPapers || isLoadingExisting ? (
          <div className="domain-tree-empty">
            <strong>正在准备数据...</strong>
            <span>正在读取文献列表和已有领域树结果。</span>
          </div>
        ) : markdownReadyPapers.length === 0 ? (
          <div className="domain-tree-empty">
            <strong>暂无可生成领域树的文献</strong>
            <span>请先在“浏览数据集”中完成 PDF 转 Markdown，再回来生成领域树。</span>
          </div>
        ) : !result ? (
          <div className="domain-tree-empty">
            <strong>尚未生成领域树</strong>
            <span>当前工作区已具备可分析文献，点击“生成领域树”即可开始。</span>
          </div>
        ) : viewMode === "tree" ? (
          <DomainTreePanel>
          <div className="domain-tree-results">
            <section className="domain-tree-card domain-tree-tree-card">
              <div className="domain-tree-card-head">
                <div>
                  <p>{treeCardTitle}</p>
                  <h2>{result.projectId}</h2>
                </div>
                <div className="domain-tree-meta">
                  <span>{result.documentCount ?? 0} 篇文档</span>
                  <span>{ACTION_LABELS[latestAction]}</span>
                  <span>{result.degraded ? "启发式降级生成" : "模型生成"}</span>
                  <span>{result.generatedAt ? new Date(result.generatedAt).toLocaleString() : "刚刚生成"}</span>
                </div>
              </div>
              {renderTree(result.domainTree, {
                selectedKey: selectedSecondaryKey,
                onSelectSecondary: (key, label) => {
                  void handleSelectSecondary(key, label);
                },
              })}
            </section>

            <section className="domain-tree-side-column">
              <article className="domain-tree-card">
                <div className="domain-tree-card-head">
                  <div>
                    <p>原始分块</p>
                    <h2>{selectedSecondaryLabel ? cleanLabel(selectedSecondaryLabel) : "点击二级标签查看"}</h2>
                  </div>
                </div>

                {!selectedSecondaryLabel ? (
                  <div className="domain-tree-empty">
                    <strong>尚未选择二级标签</strong>
                    <span>点击左侧领域树中的二级标签后，这里会显示相关文献的原始分块。</span>
                  </div>
                ) : isLoadingChunks ? (
                  <div className="domain-tree-empty">
                    <strong>正在匹配原始分块...</strong>
                    <span>正在读取论文详情并按标签匹配相关分块。</span>
                  </div>
                ) : chunkError ? (
                  <div className="domain-tree-error">
                    <span>{chunkError}</span>
                  </div>
                ) : (
                  <div className="domain-tree-chunk-list">
                    {matchedChunks.map((match, index) => (
                      <article key={`${match.paperId}-${index}`} className="domain-tree-chunk-card">
                        <div className="domain-tree-chunk-head">
                          <strong>{match.paperTitle}</strong>
                          <span>匹配分数 {match.score}</span>
                        </div>
                        {match.chunk.headings && match.chunk.headings.length > 0 ? (
                          <div className="domain-tree-chunk-tags">
                            {match.chunk.headings.map((heading, headingIndex) => (
                              <span key={`${match.paperId}-${index}-heading-${headingIndex}`}>
                                {heading.heading || `标题 ${headingIndex + 1}`}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        {match.chunk.summary ? <p className="domain-tree-chunk-summary">{match.chunk.summary}</p> : null}
                        <pre className="domain-tree-chunk-content">{match.chunk.content || "暂无分块正文"}</pre>
                      </article>
                    ))}
                  </div>
                )}
              </article>

              <article className="domain-tree-card">
                <div className="domain-tree-card-head">
                  <div>
                    <p>原始目录</p>
                    <h2>Catalog 预览</h2>
                  </div>
                </div>
                <pre className="domain-tree-catalog-preview">{result.catalogText || "暂无目录内容"}</pre>
              </article>
            </section>
          </div>
          </DomainTreePanel>
        ) : (
          <KnowledgeGraphPanel>
          <div className="domain-tree-graph-page">
            <article className="domain-tree-card">
              <div className="domain-tree-card-head">
                <div>
                  <p>知识图谱</p>
                  <h2>可读版结构概览</h2>
                </div>
                <div className="domain-tree-meta">
                  <span>{graphStats.nodeCount} 个节点</span>
                  <span>{graphStats.edgeCount} 条关系</span>
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

              <div className="domain-tree-readable-grid">
                <section className="domain-tree-readable-column">
                  <article className="domain-tree-readable-card">
                    <div className="domain-tree-card-head">
                      <div>
                        <p>领域结构</p>
                        <h2>主题与子方向</h2>
                      </div>
                    </div>
                    <div className="domain-tree-readable-stack">
                      {readableGraph.domains.map((domain) => (
                        <article key={domain.id} className="domain-tree-readable-item">
                          <strong>{domain.name}</strong>
                          {domain.subdomains.length > 0 ? (
                            <div className="domain-tree-readable-tags">
                              {domain.subdomains.map((subdomain) => (
                                <span key={`${domain.id}-sub-${subdomain}`}>{subdomain}</span>
                              ))}
                            </div>
                          ) : (
                            <span className="domain-tree-readable-empty">暂无细分子方向</span>
                          )}
                        </article>
                      ))}
                    </div>
                  </article>

                  <article className="domain-tree-readable-card">
                    <div className="domain-tree-card-head">
                      <div>
                        <p>关系摘要</p>
                        <h2>图谱里实际连接了什么</h2>
                      </div>
                    </div>
                    <div className="domain-tree-readable-stack">
                      {readableGraph.relationSummary.map((item) => (
                        <article key={item.label} className="domain-tree-readable-item">
                          <strong>{item.label}</strong>
                          <span>{item.count} 条</span>
                        </article>
                      ))}
                    </div>
                  </article>
                </section>

                <section className="domain-tree-readable-column">
                  <article className="domain-tree-readable-card">
                    <div className="domain-tree-card-head">
                      <div>
                        <p>文献关联</p>
                        <h2>每篇文献在图谱中的定位</h2>
                      </div>
                    </div>
                    <div className="domain-tree-readable-stack">
                      {readableGraph.documents.map((document) => (
                        <article key={document.id} className="domain-tree-readable-item">
                          <strong>{document.title}</strong>
                          {document.domains.length > 0 ? (
                            <div className="domain-tree-readable-meta">
                              <label>所属领域</label>
                              <div className="domain-tree-readable-tags">
                                {document.domains.map((domain) => (
                                  <span key={`${document.id}-domain-${domain}`}>{domain}</span>
                                ))}
                              </div>
                            </div>
                          ) : null}
                          {document.topics.length > 0 ? (
                            <div className="domain-tree-readable-meta">
                              <label>命中主题</label>
                              <div className="domain-tree-readable-tags">
                                {document.topics.slice(0, 8).map((topic) => (
                                  <span key={`${document.id}-topic-${topic}`}>{topic}</span>
                                ))}
                              </div>
                            </div>
                          ) : null}
                          {document.sections.length > 0 ? (
                            <div className="domain-tree-readable-meta">
                              <label>关键章节</label>
                              <ul className="domain-tree-readable-list">
                                {document.sections.map((section) => (
                                  <li key={`${document.id}-section-${section}`}>{section}</li>
                                ))}
                              </ul>
                            </div>
                          ) : (
                            <span className="domain-tree-readable-empty">当前没有章节级关联信息</span>
                          )}
                        </article>
                      ))}
                    </div>
                  </article>
                </section>
              </div>

              <div className="domain-tree-semantic-section">
                <div className="domain-tree-card-head">
                  <div>
                    <p>全文语义层</p>
                    <h2>实体、关系、原文证据与引用</h2>
                  </div>
                  {semanticOverview.extraction ? (
                    <div className="domain-tree-meta">
                      <span>{semanticOverview.extraction.processedChunkCount ?? 0} 个成功分块</span>
                      <span>{semanticOverview.extraction.failedChunkCount ?? 0} 个失败分块</span>
                      <span>{semanticOverview.extraction.evidenceCount ?? 0} 条证据</span>
                    </div>
                  ) : null}
                </div>

                {semanticOverview.entities.length === 0 && semanticOverview.citations.length === 0 ? (
                  <div className="domain-tree-empty">
                    <strong>当前图谱尚未包含全文语义层</strong>
                    <span>重新构建领域树后，系统会从 Markdown 全文抽取实体关系、原文证据和引用。</span>
                  </div>
                ) : (
                  <div className="domain-tree-readable-grid">
                    <section className="domain-tree-readable-column">
                      <article className="domain-tree-readable-card">
                        <div className="domain-tree-card-head">
                          <div>
                            <p>实体清单</p>
                            <h2>{semanticOverview.entities.length} 个规范化实体</h2>
                          </div>
                        </div>
                        <div className="domain-tree-graph-summary">
                          {Object.entries(semanticOverview.entityTypes).map(([type, count]) => (
                            <div key={type} className="domain-tree-chip">
                              <strong>{count}</strong>
                              <span>{type}</span>
                            </div>
                          ))}
                        </div>
                        <div className="domain-tree-readable-stack">
                          {semanticOverview.entities.slice(0, 80).map((entity) => (
                            <article key={entity.id} className="domain-tree-readable-item">
                              <strong>{entity.name}</strong>
                              <span>{entity.type || "未分类实体"}</span>
                              {(entity.aliases ?? []).length > 0 ? (
                                <div className="domain-tree-readable-tags">
                                  {(entity.aliases ?? []).map((alias) => (
                                    <span key={`${entity.id}-alias-${alias}`}>{alias}</span>
                                  ))}
                                </div>
                              ) : null}
                              {(entity.attributes ?? []).length > 0 ? (
                                <ul className="domain-tree-readable-list">
                                  {(entity.attributes ?? []).map((attribute, index) => (
                                    <li key={`${entity.id}-attribute-${index}`}>
                                      {attribute.name}：{attribute.value}{attribute.unit ? ` ${attribute.unit}` : ""}
                                    </li>
                                  ))}
                                </ul>
                              ) : null}
                            </article>
                          ))}
                        </div>
                        {semanticOverview.entities.length > 80 ? (
                          <span className="domain-tree-readable-empty">为保证页面流畅，此处展示前 80 个实体，完整数据保存在图谱结果中。</span>
                        ) : null}
                      </article>

                      <article className="domain-tree-readable-card">
                        <div className="domain-tree-card-head">
                          <div>
                            <p>引用解析</p>
                            <h2>{semanticOverview.citations.length} 条参考文献</h2>
                          </div>
                        </div>
                        <div className="domain-tree-readable-stack">
                          {semanticOverview.citations.slice(0, 80).map((citation) => (
                            <article key={citation.id} className="domain-tree-readable-item">
                              <strong>{citation.marker} {citation.title || "未识别标题"}</strong>
                              <span>
                                来源：{citation.documentTitle}
                                {citation.year ? ` · ${citation.year}` : ""}
                                {citation.matchedDocumentId ? " · 已链接本地文献" : ""}
                              </span>
                              {citation.doi ? <span>DOI：{citation.doi}</span> : null}
                              {(citation.contexts ?? []).slice(0, 2).map((context, index) => (
                                <blockquote key={`${citation.id}-context-${index}`} className="domain-tree-evidence-quote">
                                  <span>{context.section || "正文"} · 第 {context.lineStart ?? "?"} 行</span>
                                  {context.quote}
                                </blockquote>
                              ))}
                            </article>
                          ))}
                        </div>
                      </article>
                    </section>

                    <section className="domain-tree-readable-column">
                      <article className="domain-tree-readable-card">
                        <div className="domain-tree-card-head">
                          <div>
                            <p>语义三元组</p>
                            <h2>{semanticOverview.relations.length} 条实体关系</h2>
                          </div>
                        </div>
                        <div className="domain-tree-readable-stack">
                          {semanticOverview.relations.slice(0, 100).map((relation) => (
                            <article key={relation.id} className="domain-tree-readable-item">
                              <strong>{relation.sourceName} —{relation.predicate}→ {relation.targetName}</strong>
                              <span>
                                {RELATION_TYPE_LABELS[relation.relationType] ?? relation.relationType}
                                {` · 置信度 ${Math.round((relation.confidence ?? 0) * 100)}%`}
                              </span>
                              {relation.evidence.slice(0, 3).map((item) => (
                                <blockquote key={item.id} className="domain-tree-evidence-quote">
                                  <span>{item.section || "正文"} · 第 {item.lineStart ?? "?"} 行</span>
                                  {item.quote}
                                </blockquote>
                              ))}
                            </article>
                          ))}
                        </div>
                        {semanticOverview.relations.length > 100 ? (
                          <span className="domain-tree-readable-empty">为保证页面流畅，此处展示前 100 条关系，完整数据保存在图谱结果中。</span>
                        ) : null}
                      </article>
                    </section>
                  </div>
                )}
              </div>
            </article>
          </div>
          </KnowledgeGraphPanel>
        )}
          </>
        ) : null}
      </section>
    </main>
  );
}
