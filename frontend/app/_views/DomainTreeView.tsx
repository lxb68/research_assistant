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
import {
  KnowledgeCurationDialog,
  KnowledgeCurationEditor,
  KnowledgeCurationValues,
} from "@/app/_views/project-knowledge/KnowledgeCurationDialog";

type DomainTreeNode = {
  id: string;
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
  curation?: {
    revision: number;
    updatedAt?: string;
    hasManualChanges?: boolean;
    orphanedPatchCount?: number;
  };
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

type KnowledgeBrowserRelation = {
  id: string;
  sourceId: string;
  sourceName: string;
  sourceType: string;
  targetId: string;
  targetName: string;
  targetType: string;
  predicate: string;
  relationType: string;
  confidence?: number;
  evidence: SemanticEvidence[];
  documentIds: string[];
  editable: boolean;
};

type CurationUndo = {
  kind: "tree" | "entity" | "relation";
  id: string;
  label: string;
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

/* 提交按钮直接说明预期结果，避免只写“更新”而无法对应当前执行模式。 */
const ACTION_BUTTON_LABELS: Record<DomainTreeAction, string> = {
  revise: "应用修改并更新领域树",
  rebuild: "重新生成领域树",
  keep: "保留结构并刷新结果",
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
    onEdit: (node: DomainTreeNode) => void;
    onDelete: (node: DomainTreeNode) => void;
    disabled?: boolean;
  },
  parentKey = "root",
): React.ReactNode {
  // 递归渲染任意深度的领域树，同时保持稳定节点路径。
  return (
    <div className="domain-tree-node-list">
      {nodes.map((node, index) => {
        const key = node.id || `${parentKey}-${index}-${node.label}`;
        return (
          <article key={key} className="domain-tree-node-card">
            <div className="domain-tree-node-heading">
              <div className="domain-tree-node-label">{node.label}</div>
              <div className="domain-tree-node-actions">
                <button type="button" disabled={options.disabled} onClick={() => options.onEdit(node)}>编辑</button>
                <button className="is-danger" type="button" disabled={options.disabled} onClick={() => options.onDelete(node)}>删除</button>
              </div>
            </div>
            {node.child && node.child.length > 0 ? (
              <div className="domain-tree-secondary-list">
                {node.child.map((child, childIndex) => {
                  const childKey = child.id || `${key}-child-${childIndex}-${child.label}`;
                  return (
                    <div key={childKey} className="domain-tree-secondary-row">
                      <button
                        type="button"
                        className={`domain-tree-secondary-button${
                          options.selectedKey === childKey ? " is-active" : ""
                        }`}
                        onClick={() => options.onSelectSecondary(childKey, child.label)}
                      >
                        {child.label}
                      </button>
                      <div className="domain-tree-node-actions">
                        <button type="button" disabled={options.disabled} onClick={() => options.onEdit(child)}>编辑</button>
                        <button className="is-danger" type="button" disabled={options.disabled} onClick={() => options.onDelete(child)}>删除</button>
                      </div>
                    </div>
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
  const [graphQuery, setGraphQuery] = useState("");
  const [graphEntityType, setGraphEntityType] = useState("all");
  const [graphRelationType, setGraphRelationType] = useState("all");
  const [graphDocumentId, setGraphDocumentId] = useState("all");
  const [graphDomain, setGraphDomain] = useState("all");
  const [selectedGraphRelationId, setSelectedGraphRelationId] = useState("");
  const [visibleGraphRelationCount, setVisibleGraphRelationCount] = useState(20);
  const [isSavingCuration, setIsSavingCuration] = useState(false);
  const [curationUndo, setCurationUndo] = useState<CurationUndo | null>(null);
  const [curationEditor, setCurationEditor] = useState<KnowledgeCurationEditor | null>(null);

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

  const knowledgeBrowser = useMemo(() => {
    const graph = result?.knowledgeGraph;
    const nodes = graph?.nodes ?? [];
    const entities = graph?.entities ?? [];
    const evidence = graph?.evidence ?? [];
    const nodeMap = new Map(nodes.map((node) => [node.id, node]));
    const entityMap = new Map(entities.map((entity) => [entity.id, entity]));
    const evidenceMap = new Map(evidence.map((item) => [item.id, item]));

    const semanticRelations: KnowledgeBrowserRelation[] = (graph?.semanticRelations ?? []).map((relation) => {
      const relationEvidence = (relation.evidenceIds ?? [])
        .map((evidenceId) => evidenceMap.get(evidenceId))
        .filter((item): item is SemanticEvidence => Boolean(item));
      const source = entityMap.get(relation.source);
      const target = entityMap.get(relation.target);
      return {
        id: relation.id,
        sourceId: relation.source,
        sourceName: source?.name || relation.source,
        sourceType: source?.type || "未分类实体",
        targetId: relation.target,
        targetName: target?.name || relation.target,
        targetType: target?.type || "未分类实体",
        predicate: relation.predicate || "关联",
        relationType: relation.relationType || "general",
        confidence: relation.confidence,
        evidence: relationEvidence,
        documentIds: uniqueStrings([
          ...(relation.documentIds ?? []),
          ...relationEvidence.map((item) => item.documentId),
        ]).map((documentId) => documentId.replace(/^doc:/, "")),
        editable: true,
      };
    });

    // 兼容尚未重建语义层的历史图谱，至少让结构边可被检索和阅读。
    const structuralRelations: KnowledgeBrowserRelation[] = (graph?.edges ?? []).map((edge, index) => {
      const source = nodeMap.get(edge.source);
      const target = nodeMap.get(edge.target);
      const relationEvidence = (edge.evidenceIds ?? [])
        .map((evidenceId) => evidenceMap.get(evidenceId))
        .filter((item): item is SemanticEvidence => Boolean(item));
      const endpointDocumentIds = [source, target]
        .filter((node): node is GraphNode => Boolean(node?.type === "document"))
        .map((node) => node.id.replace(/^doc:/, ""));
      return {
        id: `structure-${edge.source}-${edge.target}-${index}`,
        sourceId: edge.source,
        sourceName: source?.name || edge.source,
        sourceType: source?.type || "未知节点",
        targetId: edge.target,
        targetName: target?.name || edge.target,
        targetType: target?.type || "未知节点",
        predicate: edge.predicate || edge.relation || "关联",
        relationType: edge.relationType || edge.relation || "general",
        confidence: edge.confidence,
        evidence: relationEvidence,
        documentIds: uniqueStrings([
          ...endpointDocumentIds,
          ...relationEvidence.map((item) => item.documentId.replace(/^doc:/, "")),
        ]),
        editable: false,
      };
    });

    const relations = semanticRelations.length > 0 ? semanticRelations : structuralRelations;
    const entityTypes = uniqueStrings(relations.flatMap((relation) => [relation.sourceType, relation.targetType]))
      .sort((left, right) => left.localeCompare(right, "zh-CN"));
    const relationTypes = uniqueStrings(relations.map((relation) => relation.relationType))
      .sort((left, right) => left.localeCompare(right, "zh-CN"));
    const documentTitleMap = new Map<string, string>();
    for (const document of result?.manifest?.documents ?? []) {
      documentTitleMap.set(document.recordId.replace(/^doc:/, ""), document.title || document.recordId);
    }
    for (const document of readableGraph.documents) {
      documentTitleMap.set(document.id.replace(/^doc:/, ""), document.title);
    }
    for (const relation of relations) {
      for (const documentId of relation.documentIds) {
        if (!documentTitleMap.has(documentId)) {
          documentTitleMap.set(documentId, documentId);
        }
      }
    }

    return {
      relations,
      entityMap,
      entityTypes,
      relationTypes,
      documentOptions: Array.from(documentTitleMap, ([id, title]) => ({ id, title }))
        .sort((left, right) => left.title.localeCompare(right.title, "zh-CN")),
      domainOptions: readableGraph.domains.map((domain) => domain.name),
    };
  }, [readableGraph, result]);

  const filteredGraphRelations = useMemo(() => {
    const normalizedQuery = graphQuery.trim().toLowerCase();
    const domainDocumentIds = graphDomain === "all"
      ? null
      : new Set(
          readableGraph.documents
            .filter((document) => document.domains.includes(graphDomain))
            .map((document) => document.id.replace(/^doc:/, "")),
        );

    return knowledgeBrowser.relations.filter((relation) => {
      if (graphEntityType !== "all" && relation.sourceType !== graphEntityType && relation.targetType !== graphEntityType) {
        return false;
      }
      if (graphRelationType !== "all" && relation.relationType !== graphRelationType) {
        return false;
      }
      if (graphDocumentId !== "all" && !relation.documentIds.includes(graphDocumentId)) {
        return false;
      }
      if (domainDocumentIds && !relation.documentIds.some((documentId) => domainDocumentIds.has(documentId))) {
        return false;
      }
      if (!normalizedQuery) {
        return true;
      }
      return [relation.sourceName, relation.targetName, relation.predicate, relation.sourceType, relation.targetType]
        .some((value) => value.toLowerCase().includes(normalizedQuery));
    });
  }, [graphDocumentId, graphDomain, graphEntityType, graphQuery, graphRelationType, knowledgeBrowser.relations, readableGraph.documents]);

  const selectedGraphRelation = filteredGraphRelations.find((relation) => relation.id === selectedGraphRelationId)
    ?? filteredGraphRelations[0]
    ?? null;

  const selectedGraphContext = useMemo(() => {
    if (!selectedGraphRelation) {
      return null;
    }
    const documentIdSet = new Set(selectedGraphRelation.documentIds);
    return {
      source: knowledgeBrowser.entityMap.get(selectedGraphRelation.sourceId),
      target: knowledgeBrowser.entityMap.get(selectedGraphRelation.targetId),
      documents: knowledgeBrowser.documentOptions.filter((document) => documentIdSet.has(document.id)),
      citations: semanticOverview.citations
        .filter((citation) => documentIdSet.has(citation.documentId.replace(/^doc:/, "")))
        .slice(0, 6),
    };
  }, [knowledgeBrowser.documentOptions, knowledgeBrowser.entityMap, selectedGraphRelation, semanticOverview.citations]);

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

  /** 提交人工修订并使用后端返回的有效结果刷新页面。 */
  async function requestCuration(
    path: string,
    method: "PATCH" | "DELETE" | "POST",
    body: Record<string, unknown>,
  ) {
    const response = await fetch(buildApiUrl(path), {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 409) {
        const latestResponse = await fetch(
          buildApiUrl(`/api/projects/${encodeURIComponent(activeProjectId)}/domain-tree`),
        );
        const latestPayload = await latestResponse.json().catch(() => ({}));
        if (latestResponse.ok) setResult(latestPayload);
      }
      throw new Error(payload.detail || "保存人工修订失败");
    }
    return payload;
  }

  function currentRevision() {
    return result?.curation?.revision ?? 0;
  }

  function handleEditTreeNode(node: DomainTreeNode) {
    setError("");
    setCurationEditor({ action: "edit", kind: "tree", id: node.id, label: node.label });
  }

  async function handleDeleteTreeNode(node: DomainTreeNode) {
    setIsSavingCuration(true);
    setError("");
    try {
      const path = `/api/projects/${encodeURIComponent(activeProjectId)}/domain-tree/nodes/${encodeURIComponent(node.id)}`;
      const preview = await requestCuration(`${path}?dry_run=true`, "DELETE", { revision: currentRevision() });
      const descendantCount = Number(preview.impact?.descendantCount || 0);
      setCurationEditor({
        action: "delete",
        kind: "tree",
        id: node.id,
        label: node.label,
        impactText: descendantCount > 0
          ? `该节点包含 ${descendantCount} 个子节点，确认后将一并从有效领域树和结构图中隐藏。`
          : "该节点会从有效领域树和对应结构图中隐藏。",
      });
    } catch (mutationError) {
      setError(mutationError instanceof Error ? mutationError.message : "删除领域节点失败");
    } finally {
      setIsSavingCuration(false);
    }
  }

  function handleEditEntity(entity: SemanticEntity) {
    setError("");
    setCurationEditor({
      action: "edit",
      kind: "entity",
      id: entity.id,
      name: entity.name,
      entityType: entity.type,
      aliases: entity.aliases ?? [],
    });
  }

  async function handleDeleteEntity(entity: SemanticEntity) {
    setIsSavingCuration(true);
    setError("");
    try {
      const path = `/api/projects/${encodeURIComponent(activeProjectId)}/knowledge-graph/entities/${encodeURIComponent(entity.id)}`;
      const preview = await requestCuration(`${path}?dry_run=true`, "DELETE", { revision: currentRevision() });
      const relationCount = Number(preview.impact?.relationCount || 0);
      setCurationEditor({
        action: "delete",
        kind: "entity",
        id: entity.id,
        label: entity.name,
        impactText: relationCount > 0
          ? `该实体连接 ${relationCount} 条语义关系，确认后这些关系也会一并隐藏。原文证据仍会保留。`
          : "该实体当前没有关联关系。原文证据仍会保留。",
      });
    } catch (mutationError) {
      setError(mutationError instanceof Error ? mutationError.message : "删除实体失败");
    } finally {
      setIsSavingCuration(false);
    }
  }

  function handleEditRelation(relation: KnowledgeBrowserRelation) {
    if (!relation.editable) return;
    setError("");
    setCurationEditor({
      action: "edit",
      kind: "relation",
      id: relation.id,
      predicate: relation.predicate,
      relationType: relation.relationType,
      confidence: relation.confidence ?? 0.5,
      source: relation.sourceId,
      target: relation.targetId,
    });
  }

  function handleDeleteRelation(relation: KnowledgeBrowserRelation) {
    if (!relation.editable) return;
    setError("");
    setCurationEditor({
      action: "delete",
      kind: "relation",
      id: relation.id,
      label: `${relation.sourceName} —${relation.predicate}→ ${relation.targetName}`,
      impactText: "该关系会从知识浏览和混合图谱检索中隐藏，实体与原文证据不受影响。",
    });
  }

  async function handleSubmitCurationEditor(values: KnowledgeCurationValues) {
    if (!curationEditor) return;
    setIsSavingCuration(true);
    setError("");
    try {
      const base = `/api/projects/${encodeURIComponent(activeProjectId)}`;
      let payload: DomainTreeResult;
      let successMessage = "人工修订已保存。";
      if (curationEditor.action === "delete") {
        const pathByKind = {
          tree: "domain-tree/nodes",
          entity: "knowledge-graph/entities",
          relation: "knowledge-graph/relations",
        };
        payload = await requestCuration(
          `${base}/${pathByKind[curationEditor.kind]}/${encodeURIComponent(curationEditor.id)}`,
          "DELETE",
          { revision: currentRevision() },
        );
        setCurationUndo({
          kind: curationEditor.kind,
          id: curationEditor.id,
          label: curationEditor.label,
        });
        successMessage = `已删除“${curationEditor.label}”，可使用撤销恢复。`;
        if (curationEditor.kind === "tree") {
          setSelectedSecondaryKey("");
          setSelectedSecondaryLabel("");
        } else {
          setSelectedGraphRelationId("");
        }
      } else if (curationEditor.kind === "tree") {
        payload = await requestCuration(
          `${base}/domain-tree/nodes/${encodeURIComponent(curationEditor.id)}`,
          "PATCH",
          { revision: currentRevision(), label: values.label },
        );
        setCurationUndo(null);
        successMessage = `已将领域节点修改为“${values.label}”。`;
      } else if (curationEditor.kind === "entity") {
        payload = await requestCuration(
          `${base}/knowledge-graph/entities/${encodeURIComponent(curationEditor.id)}`,
          "PATCH",
          {
            revision: currentRevision(),
            name: values.name,
            type: values.entityType,
            aliases: values.aliases,
          },
        );
        setCurationUndo(null);
        successMessage = `已更新实体“${values.name}”。`;
      } else {
        payload = await requestCuration(
          `${base}/knowledge-graph/relations/${encodeURIComponent(curationEditor.id)}`,
          "PATCH",
          {
            revision: currentRevision(),
            predicate: values.predicate,
            relationType: values.relationType,
            confidence: values.confidence,
            source: values.source,
            target: values.target,
          },
        );
        setCurationUndo(null);
        successMessage = `已更新关系“${values.predicate}”。`;
      }
      setResult(payload);
      setStatus(successMessage);
      setCurationEditor(null);
    } catch (mutationError) {
      setError(mutationError instanceof Error ? mutationError.message : "保存人工修订失败");
    } finally {
      setIsSavingCuration(false);
    }
  }

  async function handleUndoCuration() {
    if (!curationUndo) return;
    const pathByKind = {
      tree: "domain-tree/nodes",
      entity: "knowledge-graph/entities",
      relation: "knowledge-graph/relations",
    };
    setIsSavingCuration(true);
    setError("");
    try {
      const payload = await requestCuration(
        `/api/projects/${encodeURIComponent(activeProjectId)}/${pathByKind[curationUndo.kind]}/${encodeURIComponent(curationUndo.id)}/restore`,
        "POST",
        { revision: currentRevision() },
      );
      setResult(payload);
      setStatus(`已恢复“${curationUndo.label}”。`);
      setCurationUndo(null);
    } catch (mutationError) {
      setError(mutationError instanceof Error ? mutationError.message : "恢复失败");
    } finally {
      setIsSavingCuration(false);
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
            {result?.curation?.hasManualChanges ? (
              <span className="knowledge-curation-badge">人工修订 v{result.curation.revision}</span>
            ) : null}
          </div>
        </div>

        {viewMode === "project" ? (
          <>
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
              {isGenerating ? "后台处理中..." : result ? ACTION_BUTTON_LABELS[generationMode] : "生成领域树"}
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

        {status ? (
          <div className="domain-tree-status">
            <span>{status}</span>
            {curationUndo ? (
              <button
                type="button"
                className="domain-tree-inline-button"
                disabled={isSavingCuration}
                onClick={() => void handleUndoCuration()}
              >
                撤销删除
              </button>
            ) : null}
          </div>
        ) : null}
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
          </>
        ) : null}

        {viewMode !== "project" ? (
          <>
        {status ? (
          <div className="domain-tree-status">
            <span>{status}</span>
            {curationUndo ? (
              <button
                type="button"
                className="domain-tree-inline-button"
                disabled={isSavingCuration}
                onClick={() => void handleUndoCuration()}
              >
                撤销删除
              </button>
            ) : null}
          </div>
        ) : null}
        {error ? <div className="domain-tree-error"><span>{error}</span></div> : null}
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
                onEdit: (node) => void handleEditTreeNode(node),
                onDelete: (node) => void handleDeleteTreeNode(node),
                disabled: isSavingCuration || isGenerating,
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
              <article className="domain-tree-card knowledge-browser">
                <header className="knowledge-browser-header">
                  <div>
                    <p>知识图谱</p>
                    <h2>关系浏览器</h2>
                    <span>筛选关系后，在右侧核对实体属性、关联文献和原文证据。</span>
                  </div>
                  <div className="knowledge-browser-metrics" aria-label="知识图谱概览">
                    <div><span>节点</span><strong>{graphStats.nodeCount}</strong></div>
                    <div><span>可读关系</span><strong>{knowledgeBrowser.relations.length}</strong></div>
                    <div><span>文献</span><strong>{knowledgeBrowser.documentOptions.length}</strong></div>
                    <div><span>证据</span><strong>{semanticOverview.extraction?.evidenceCount ?? 0}</strong></div>
                  </div>
                </header>

                <div className="knowledge-browser-layout">
                  <aside className="knowledge-browser-filters" aria-label="关系筛选">
                    <div className="knowledge-browser-section-head">
                      <div><span>浏览范围</span><strong>筛选</strong></div>
                      <button
                        type="button"
                        onClick={() => {
                          setGraphQuery("");
                          setGraphEntityType("all");
                          setGraphRelationType("all");
                          setGraphDocumentId("all");
                          setGraphDomain("all");
                          setVisibleGraphRelationCount(20);
                        }}
                      >
                        重置
                      </button>
                    </div>
                    <label>
                      <span>搜索实体或关系</span>
                      <input
                        value={graphQuery}
                        onChange={(event) => {
                          setGraphQuery(event.target.value);
                          setVisibleGraphRelationCount(20);
                        }}
                        placeholder="输入名称或关系词"
                      />
                    </label>
                    <label>
                      <span>实体类型</span>
                      <select
                        value={graphEntityType}
                        onChange={(event) => {
                          setGraphEntityType(event.target.value);
                          setVisibleGraphRelationCount(20);
                        }}
                      >
                        <option value="all">全部实体类型</option>
                        {knowledgeBrowser.entityTypes.map((type) => <option key={type} value={type}>{type}</option>)}
                      </select>
                    </label>
                    <label>
                      <span>关系类型</span>
                      <select
                        value={graphRelationType}
                        onChange={(event) => {
                          setGraphRelationType(event.target.value);
                          setVisibleGraphRelationCount(20);
                        }}
                      >
                        <option value="all">全部关系类型</option>
                        {knowledgeBrowser.relationTypes.map((type) => (
                          <option key={type} value={type}>{RELATION_TYPE_LABELS[type] ?? type}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>研究领域</span>
                      <select
                        value={graphDomain}
                        onChange={(event) => {
                          setGraphDomain(event.target.value);
                          setVisibleGraphRelationCount(20);
                        }}
                      >
                        <option value="all">全部领域</option>
                        {knowledgeBrowser.domainOptions.map((domain) => <option key={domain} value={domain}>{domain}</option>)}
                      </select>
                    </label>
                    <label>
                      <span>关联文献</span>
                      <select
                        value={graphDocumentId}
                        onChange={(event) => {
                          setGraphDocumentId(event.target.value);
                          setVisibleGraphRelationCount(20);
                        }}
                      >
                        <option value="all">全部文献</option>
                        {knowledgeBrowser.documentOptions.map((document) => (
                          <option key={document.id} value={document.id}>{document.title}</option>
                        ))}
                      </select>
                    </label>
                    <div className="knowledge-browser-type-summary">
                      <span>节点构成</span>
                      {Object.entries(graphStats.typeSummary).map(([type, count]) => (
                        <div key={type}><span>{type}</span><strong>{count}</strong></div>
                      ))}
                    </div>
                  </aside>

                  <section className="knowledge-browser-relations" aria-label="关系列表">
                    <div className="knowledge-browser-section-head">
                      <div><span>匹配结果</span><strong>关系列表</strong></div>
                      <span>{filteredGraphRelations.length} 条</span>
                    </div>
                    {filteredGraphRelations.length === 0 ? (
                      <div className="knowledge-browser-empty">
                        <strong>没有符合条件的关系</strong>
                        <span>尝试减少筛选条件或更换搜索词。</span>
                      </div>
                    ) : (
                      <div className="knowledge-browser-relation-list">
                        {filteredGraphRelations.slice(0, visibleGraphRelationCount).map((relation) => (
                          <button
                            key={relation.id}
                            type="button"
                            className={`knowledge-browser-relation-row${selectedGraphRelation?.id === relation.id ? " is-active" : ""}`}
                            onClick={() => setSelectedGraphRelationId(relation.id)}
                          >
                            <span className="knowledge-browser-entity">
                              <strong>{relation.sourceName}</strong>
                              <small>{relation.sourceType}</small>
                            </span>
                            <span className="knowledge-browser-predicate">
                              <small>{RELATION_TYPE_LABELS[relation.relationType] ?? relation.relationType}</small>
                              <strong>— {relation.predicate} →</strong>
                            </span>
                            <span className="knowledge-browser-entity">
                              <strong>{relation.targetName}</strong>
                              <small>{relation.targetType}</small>
                            </span>
                            <span className="knowledge-browser-row-meta">
                              {relation.confidence === undefined ? "结构关系" : `置信度 ${Math.round(relation.confidence * 100)}%`}
                              {relation.evidence.length > 0 ? ` · ${relation.evidence.length} 条证据` : ""}
                            </span>
                          </button>
                        ))}
                        {visibleGraphRelationCount < filteredGraphRelations.length ? (
                          <button
                            type="button"
                            className="knowledge-browser-more"
                            onClick={() => setVisibleGraphRelationCount((count) => count + 20)}
                          >
                            再显示 {Math.min(20, filteredGraphRelations.length - visibleGraphRelationCount)} 条
                          </button>
                        ) : null}
                      </div>
                    )}
                  </section>

                  <aside className="knowledge-browser-detail" aria-label="关系详情">
                    <div className="knowledge-browser-section-head">
                      <div><span>当前选择</span><strong>详情</strong></div>
                    </div>
                    {!selectedGraphRelation || !selectedGraphContext ? (
                      <div className="knowledge-browser-empty">
                        <strong>尚未选择关系</strong>
                        <span>从中间列表选择一条关系后查看详细依据。</span>
                      </div>
                    ) : (
                      <div className="knowledge-browser-detail-stack">
                        <section className="knowledge-browser-detail-summary">
                          <span>{RELATION_TYPE_LABELS[selectedGraphRelation.relationType] ?? selectedGraphRelation.relationType}</span>
                          <strong>{selectedGraphRelation.sourceName} —{selectedGraphRelation.predicate}→ {selectedGraphRelation.targetName}</strong>
                          {selectedGraphRelation.confidence !== undefined ? (
                            <small>置信度 {Math.round(selectedGraphRelation.confidence * 100)}%</small>
                          ) : null}
                          {selectedGraphRelation.editable ? (
                            <div className="knowledge-curation-actions">
                              <button
                                type="button"
                                disabled={isSavingCuration || isGenerating}
                                onClick={() => void handleEditRelation(selectedGraphRelation)}
                              >
                                修改关系
                              </button>
                              <button
                                type="button"
                                className="is-danger"
                                disabled={isSavingCuration || isGenerating}
                                onClick={() => void handleDeleteRelation(selectedGraphRelation)}
                              >
                                删除关系
                              </button>
                            </div>
                          ) : (
                            <small>结构关系由领域树和文献自动生成，仅支持查看。</small>
                          )}
                        </section>

                        <section>
                          <h3>实体属性</h3>
                          <div className="knowledge-browser-endpoints">
                            {[
                              { role: "起点", id: selectedGraphRelation.sourceId, entity: selectedGraphContext.source, fallbackName: selectedGraphRelation.sourceName, fallbackType: selectedGraphRelation.sourceType },
                              { role: "终点", id: selectedGraphRelation.targetId, entity: selectedGraphContext.target, fallbackName: selectedGraphRelation.targetName, fallbackType: selectedGraphRelation.targetType },
                            ].map((item) => (
                              <article key={`${item.role}-${item.id}`}>
                                <span>{item.role} · {item.entity?.type || item.fallbackType}</span>
                                <strong>{item.entity?.name || item.fallbackName}</strong>
                                {(item.entity?.aliases ?? []).length > 0 ? <small>别名：{item.entity?.aliases?.join("、")}</small> : null}
                                {(item.entity?.attributes ?? []).map((attribute, index) => (
                                  <small key={`${item.id}-attribute-${index}`}>
                                    {attribute.name}：{attribute.value}{attribute.unit ? ` ${attribute.unit}` : ""}
                                  </small>
                                ))}
                                {item.entity ? (
                                  <div className="knowledge-curation-actions">
                                    <button
                                      type="button"
                                      disabled={isSavingCuration || isGenerating}
                                      onClick={() => void handleEditEntity(item.entity!)}
                                    >
                                      修改实体
                                    </button>
                                    <button
                                      type="button"
                                      className="is-danger"
                                      disabled={isSavingCuration || isGenerating}
                                      onClick={() => void handleDeleteEntity(item.entity!)}
                                    >
                                      删除实体
                                    </button>
                                  </div>
                                ) : null}
                              </article>
                            ))}
                          </div>
                        </section>

                        <section>
                          <h3>关联文献</h3>
                          {selectedGraphContext.documents.length > 0 ? (
                            <ul className="knowledge-browser-document-list">
                              {selectedGraphContext.documents.map((document) => <li key={document.id}>{document.title}</li>)}
                            </ul>
                          ) : <span className="domain-tree-readable-empty">当前关系未记录文献来源。</span>}
                        </section>

                        <section>
                          <h3>原文证据</h3>
                          {selectedGraphRelation.evidence.length > 0 ? selectedGraphRelation.evidence.map((item) => (
                            <blockquote key={item.id} className="domain-tree-evidence-quote">
                              <span>{item.section || "正文"} · 第 {item.lineStart ?? "?"} 行</span>
                              {item.quote}
                            </blockquote>
                          )) : <span className="domain-tree-readable-empty">当前关系没有可展示的原文证据。</span>}
                        </section>

                        {selectedGraphContext.citations.length > 0 ? (
                          <section>
                            <h3>相关引用</h3>
                            <div className="knowledge-browser-citations">
                              {selectedGraphContext.citations.map((citation) => (
                                <article key={citation.id}>
                                  <strong>{citation.marker} {citation.title || "未识别标题"}</strong>
                                  <span>{citation.documentTitle}{citation.year ? ` · ${citation.year}` : ""}</span>
                                </article>
                              ))}
                            </div>
                          </section>
                        ) : null}
                      </div>
                    )}
                  </aside>
                </div>

                <div className="knowledge-browser-supporting">
                  <details>
                    <summary>实体管理 <span>{result.knowledgeGraph?.entities?.length ?? 0} 个实体</span></summary>
                    <div className="knowledge-browser-supporting-grid">
                      {(result.knowledgeGraph?.entities ?? [])
                        .filter((entity) => {
                          const query = graphQuery.trim().toLocaleLowerCase();
                          return !query || `${entity.name} ${entity.type} ${(entity.aliases ?? []).join(" ")}`
                            .toLocaleLowerCase()
                            .includes(query);
                        })
                        .slice(0, 200)
                        .map((entity) => (
                        <article key={entity.id}>
                          <strong>{entity.name}</strong>
                          <span>{entity.type}</span>
                          <div className="knowledge-curation-actions">
                            <button
                              type="button"
                              disabled={isSavingCuration || isGenerating}
                              onClick={() => void handleEditEntity(entity)}
                            >
                              修改
                            </button>
                            <button
                              type="button"
                              className="is-danger"
                              disabled={isSavingCuration || isGenerating}
                              onClick={() => void handleDeleteEntity(entity)}
                            >
                              删除
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                  </details>
                  <details>
                    <summary>领域结构 <span>{readableGraph.domains.length} 个领域</span></summary>
                    <div className="knowledge-browser-supporting-grid">
                      {readableGraph.domains.map((domain) => (
                        <article key={domain.id}>
                          <strong>{domain.name}</strong>
                          <span>{domain.subdomains.length > 0 ? domain.subdomains.join("、") : "暂无细分子方向"}</span>
                        </article>
                      ))}
                    </div>
                  </details>
                  <details>
                    <summary>文献定位 <span>{readableGraph.documents.length} 篇文献</span></summary>
                    <div className="knowledge-browser-supporting-grid">
                      {readableGraph.documents.map((document) => (
                        <article key={document.id}>
                          <strong>{document.title}</strong>
                          <span>{document.domains.length > 0 ? document.domains.join("、") : "暂未匹配领域"}</span>
                        </article>
                      ))}
                    </div>
                  </details>
                </div>
              </article>
            </div>
          </KnowledgeGraphPanel>
        )}
          </>
        ) : null}
      </section>
      {curationEditor ? (
        <KnowledgeCurationDialog
          key={`${curationEditor.action}-${curationEditor.kind}-${curationEditor.id}`}
          editor={curationEditor}
          entities={(result?.knowledgeGraph?.entities ?? []).map((entity) => ({
            id: entity.id,
            name: entity.name,
            type: entity.type,
          }))}
          busy={isSavingCuration}
          error={error}
          onClose={() => {
            setCurationEditor(null);
            setError("");
          }}
          onSubmit={handleSubmitCurationEditor}
        />
      ) : null}
    </main>
  );
}
