/* 管理研究会话、流式代理响应、本地历史记录和工作区快捷入口。 */

"use client";

import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import AddRounded from "@mui/icons-material/AddRounded";
import ArticleOutlined from "@mui/icons-material/ArticleOutlined";
import AutoAwesomeRounded from "@mui/icons-material/AutoAwesomeRounded";
import BookmarkAddOutlined from "@mui/icons-material/BookmarkAddOutlined";
import CheckRounded from "@mui/icons-material/CheckRounded";
import CloseRounded from "@mui/icons-material/CloseRounded";
import ContentCopyRounded from "@mui/icons-material/ContentCopyRounded";
import DeleteOutlineRounded from "@mui/icons-material/DeleteOutlineRounded";
import DownloadRounded from "@mui/icons-material/DownloadRounded";
import EditRounded from "@mui/icons-material/EditRounded";
import FolderOpenRounded from "@mui/icons-material/FolderOpenRounded";
import HistoryRounded from "@mui/icons-material/HistoryRounded";
import MenuRounded from "@mui/icons-material/MenuRounded";
import MoreHorizRounded from "@mui/icons-material/MoreHorizRounded";
import SearchRounded from "@mui/icons-material/SearchRounded";
import SendRounded from "@mui/icons-material/SendRounded";
import ThumbUpAltOutlined from "@mui/icons-material/ThumbUpAltOutlined";
import { buildApiUrl } from "@/lib/api";
import { useBackgroundTasks } from "@/app/_components/BackgroundTaskProvider";
import { fetchJob } from "@/lib/background-jobs";
import MessageContent from "@/app/_components/MessageContent";
import { useProjects } from "@/app/_components/ProjectProvider";
import {
  createResearchMemory,
  deleteResearchMemory as deleteResearchMemoryApi,
  detectExplicitPreferredName,
  extractResearchMemory,
  getUserPreferences,
  listResearchMemories,
  saveUserPreferences,
  updateResearchMemory,
  type ResearchMemory as ResearchRecord,
  type ResearchMemoryDraft,
  type UserPreferences,
} from "@/lib/research-memory";

type Props = { onOpenBrowse: () => void; onOpenDomainTree: () => void };
type Source = {
  index: number;
  recordId?: string;
  title: string;
  year?: string;
  source?: string;
  section?: string;
  chunkIndex?: number;
  excerpt?: string;
  graphBacked?: boolean;
  retrievalChannels?: string[];
  graphEvidenceIds?: string[];
  graphRelationIds?: string[];
  graphNavigationClaims?: string[];
  graphQuotes?: string[];
};
type Message = {
  id: number;
  role: "user" | "agent";
  content: string;
  sources?: Source[];
  contextSources?: Source[];
  responseMode?: "direct" | "research";
  jobId?: string;
  maintenanceAction?: {
    kind: "cleanup-missing-pdfs";
    candidateIds: string[];
    status: "pending" | "running" | "completed" | "cancelled";
  };
};
type OrchestratorResult = {
  action: string;
  result: {
    answer?: string;
    sources?: Source[];
    retrievedSources?: Source[];
    status?: string;
    message?: string;
    requiredMaterials?: Array<{ description: string }>;
    retrievalDiagnostics?: {
      evidenceCount?: number;
      distinctPaperCount?: number;
      retrievalMode?: string;
      facetCoverage?: number;
    };
  };
};

type Conversation = { id: string; title: string; date: string; messages: Message[]; projectId?: string; projectIds?: string[] };
const CONVERSATIONS_KEY = "research-agent.conversations";
const LEGACY_RESEARCH_RECORDS_KEY = "research-agent.research-records";
// 对话仍保留本地缓存；旧版整条归档键仅用于一次性迁移到后端研究记忆。
const ACTIVE_CONVERSATION_KEY = "research-agent.active-conversation";
const RESEARCH_STAGE_ORDER = ["planning", "retrieving", "validating", "composing", "persisting", "completed"];
const RESEARCH_STAGE_ITEMS = [
  { stage: "planning", label: "理解问题与规划" },
  { stage: "retrieving", label: "检索知识库" },
  { stage: "validating", label: "交叉验证证据" },
  { stage: "composing", label: "生成综合结论" },
];

/** 识别明确要求清理无 PDF 文献记录的管理意图。 */
function isMissingPdfCleanupRequest(value: string) {
  const normalized = value.replace(/\s+/g, "").toLowerCase();
  const requestsDeletion = /(删除|清理|移除)/.test(normalized);
  const targetsDataset = /(数据集|数据中心|文献库|论文库|文献|论文)/.test(normalized);
  const targetsMissingPdf = /(?:没有|无|缺少|缺失|未下载|未绑定|不存在).*pdf|pdf.*(?:没有|无|缺少|缺失|未下载|未绑定|不存在)/i.test(normalized);
  return requestsDeletion && targetsDataset && targetsMissingPdf;
}

/** 排除失败占位消息及其对应问题，避免不完整轮次污染后续模型上下文。 */
function usableHistory(messages: Message[]) {
  const cleaned: Message[] = [];
  const errorPrefixes = ["请求失败：", "研究对话请求失败", "研究任务已完成，但没有返回"];
  for (const message of messages) {
    const isTransientError = message.role === "agent" && errorPrefixes.some((prefix) => message.content.startsWith(prefix));
    if (isTransientError) {
      if (cleaned.at(-1)?.role === "user") cleaned.pop();
      continue;
    }
    cleaned.push(message);
  }
  return cleaned;
}

/** 主项目排在首位，其余附加项目去重后组成当前对话的检索范围。 */
function listUniqueProjectIds(primaryProjectId: string, selectedProjectIds: string[]) {
  return [...new Set([primaryProjectId, ...selectedProjectIds].map((value) => value.trim()).filter(Boolean))];
}

/** 管理研究对话、流式响应和本地会话记录。 */
export default function ResearchChat({ onOpenBrowse, onOpenDomainTree }: Props) {
  const { jobs, submitJob, openCenter } = useBackgroundTasks();
  const { projects, activeProjectId, activeProject, isLoadingProjects, selectProject, refreshProjects } = useProjects();
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState("");
  const [conversationTitle, setConversationTitle] = useState("新建研究对话");
  const [researchRecords, setResearchRecords] = useState<ResearchRecord[]>([]);
  const [memoryDraft, setMemoryDraft] = useState<ResearchMemoryDraft | null>(null);
  const [editingMemoryId, setEditingMemoryId] = useState("");
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [preferences, setPreferences] = useState<UserPreferences>({
    preferredName: "",
    language: "zh-CN",
    answerStyle: "",
    updatedAt: "",
  });
  const [preferenceDraft, setPreferenceDraft] = useState({ preferredName: "", answerStyle: "" });
  const [isPreferenceOpen, setIsPreferenceOpen] = useState(false);
  const [workspaceView, setWorkspaceView] = useState<"chat" | "records">("chat");
  const [hasHydrated, setHasHydrated] = useState(false);
  const [hasLoadedServerConversations, setHasLoadedServerConversations] = useState(false);
  const [isLoadingMemories, setIsLoadingMemories] = useState(true);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [activeResearchJobId, setActiveResearchJobId] = useState("");
  const [thinkingText, setThinkingText] = useState("正在检索知识库并组织证据…");
  const [sidebar, setSidebar] = useState(true);
  const [projectScopeIds, setProjectScopeIds] = useState<string[]>([activeProjectId]);
  const [isProjectScopeOpen, setIsProjectScopeOpen] = useState(false);
  const [renamingConversationId, setRenamingConversationId] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
  const [renameError, setRenameError] = useState("");
  const [deletingConversationId, setDeletingConversationId] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [copied, setCopied] = useState<number | null>(null);
  const [selectedSource, setSelectedSource] = useState<Source | null>(null);
  const nextMessageId = useRef(1);
  const deletedConversationIds = useRef(new Set<string>());
  const activeAgentMessage = [...messages].reverse().find((message) => message.role === "agent");
  const activeSources = activeAgentMessage?.sources ?? [];
  const isDirectAnswer = activeAgentMessage?.responseMode === "direct";
  const researchJobId = activeResearchJobId || activeAgentMessage?.jobId || "";
  const researchJob = jobs.find((job) => job.jobId === researchJobId);
  const researchStatus = researchJob?.status ?? (thinking ? "running" : activeAgentMessage ? "completed" : "queued");
  const researchProgress = researchJob?.progress ?? (activeAgentMessage ? 100 : thinking ? 3 : 0);
  const currentStageIndex = researchStatus === "completed"
    ? RESEARCH_STAGE_ORDER.length - 1
    : Math.max(0, RESEARCH_STAGE_ORDER.indexOf(researchJob?.stage ?? "planning"));
  const researchStatusText = researchJob?.message
    || (researchStatus === "failed" ? researchJob?.error || "研究任务执行失败" : "等待开始研究任务");
  const researchResult = researchJob?.result as OrchestratorResult | null | undefined;
  const researchDiagnostics = researchResult?.result?.retrievalDiagnostics;
  const scopedProjects = projectScopeIds
    .map((projectId) => projects.find((project) => project.id === projectId))
    .filter((project) => project !== undefined);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        const storedConversations = JSON.parse(window.localStorage.getItem(CONVERSATIONS_KEY) || "[]") as Conversation[];
        const storedActiveId = window.localStorage.getItem(ACTIVE_CONVERSATION_KEY) || "";
        const activeConversation = storedConversations.find((conversation) => conversation.id === storedActiveId);
        setConversations(storedConversations);
        if (activeConversation) {
          setActiveConversationId(activeConversation.id);
          setConversationTitle(activeConversation.title);
          setMessages(activeConversation.messages);
          setActiveResearchJobId([...activeConversation.messages].reverse().find((message) => message.jobId)?.jobId ?? "");
          setProjectScopeIds(activeConversation.projectIds?.length
            ? activeConversation.projectIds
            : activeConversation.projectId ? [activeConversation.projectId] : []);
        }
        const highestMessageId = storedConversations.reduce(
          (highest, conversation) => Math.max(highest, ...conversation.messages.map((message) => message.id)),
          0,
        );
        nextMessageId.current = highestMessageId + 1;
      } catch {
        window.localStorage.removeItem(CONVERSATIONS_KEY);
        window.localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
      } finally {
        setHasHydrated(true);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    setProjectScopeIds((current) => current.includes(activeProjectId) ? current : [activeProjectId]);
  }, [activeProjectId]);

  useEffect(() => {
    if (!hasHydrated) return;
    window.localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(conversations));
    if (activeConversationId) {
      window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, activeConversationId);
    } else {
      window.localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
    }
  }, [activeConversationId, conversations, hasHydrated]);

  useEffect(() => {
    if (!hasHydrated) return;
    let cancelled = false;
    setIsLoadingMemories(true);
    void Promise.all([listResearchMemories(), getUserPreferences()])
      .then(async ([loadedMemories, loadedPreferences]) => {
        if (cancelled) return;
        let memories = loadedMemories;
        try {
          const legacyRecords = JSON.parse(window.localStorage.getItem(LEGACY_RESEARCH_RECORDS_KEY) || "[]") as Array<{
            conversationId?: string;
            messageId?: number;
            title?: string;
            question?: string;
            content?: string;
            sources?: Source[];
            projectId?: string;
            projectName?: string;
          }>;
          const existingKeys = new Set(memories.map((item) => `${item.sourceConversationId}:${item.sourceMessageId}`));
          const pendingLegacy = legacyRecords.filter((item) =>
            !existingKeys.has(`${item.conversationId ?? ""}:${String(item.messageId ?? "")}`),
          );
          if (pendingLegacy.length) {
            const migrated = await Promise.all(pendingLegacy.map((item) => createResearchMemory({
              projectId: item.projectId || activeProjectId,
              projectName: item.projectName || "",
              type: "conclusion",
              title: item.title || item.question || "历史研究记忆",
              summary: String(item.content || "").replace(/\s+/g, " ").trim().slice(0, 1200),
              tags: ["历史迁移"],
              confidence: item.sources?.length ? 0.6 : 0.4,
              evidence: item.sources ?? [],
              sourceConversationId: item.conversationId || "",
              sourceMessageId: String(item.messageId ?? ""),
              sourceQuestion: item.question || "",
            })));
            memories = [...migrated, ...memories];
          }
          if (legacyRecords.length) window.localStorage.removeItem(LEGACY_RESEARCH_RECORDS_KEY);
        } catch {
          // 旧数据迁移失败时保留 localStorage，避免不可恢复的数据丢失。
        }
        if (cancelled) return;
        setResearchRecords(memories);
        setPreferences(loadedPreferences);
        setPreferenceDraft({
          preferredName: loadedPreferences.preferredName,
          answerStyle: loadedPreferences.answerStyle,
        });
      })
      .catch((error) => {
        if (!cancelled) setMemoryError(error instanceof Error ? error.message : "加载研究记忆失败");
      })
      .finally(() => {
        if (!cancelled) setIsLoadingMemories(false);
      });
    return () => { cancelled = true; };
  }, [activeProjectId, hasHydrated]);

  useEffect(() => {
    if (!hasHydrated) return;
    let cancelled = false;
    void fetch(buildApiUrl("/api/conversations?sessionId=local&limit=100"), { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) return { conversations: [] };
        return response.json();
      })
      .then((payload: { conversations?: Array<{ id: string; title: string; messages: Array<Record<string, unknown>> }> }) => {
        if (cancelled) return;
        const serverConversations: Conversation[] = (payload.conversations ?? [])
          .filter((conversation) => !deletedConversationIds.current.has(conversation.id))
          .map((conversation) => ({
            id: conversation.id,
            title: conversation.title,
            date: "已同步",
            messages: conversation.messages.map((raw, index) => {
              const numericId = Number(raw.id);
              const rawSources = Array.isArray(raw.sources) ? raw.sources as Array<Record<string, unknown>> : [];
              const rawContext = Array.isArray(raw.contextSources) ? raw.contextSources as Array<Record<string, unknown>> : [];
              const normalizeSources = (sources: Array<Record<string, unknown>>) => sources.map((source, sourceIndex) => ({
                index: Number(source.index ?? sourceIndex + 1),
                recordId: String(source.recordId ?? source.record_id ?? ""),
                title: String(source.title ?? ""),
                year: String(source.year ?? ""),
                source: String(source.source ?? ""),
                section: String(source.section ?? ""),
                chunkIndex: Number(source.chunkIndex ?? source.chunk_index ?? 0),
                excerpt: String(source.excerpt ?? ""),
                graphBacked: Boolean(source.graphBacked ?? source.graph_backed),
                retrievalChannels: Array.isArray(source.retrievalChannels ?? source.retrieval_channels)
                  ? (source.retrievalChannels ?? source.retrieval_channels) as string[] : [],
                graphEvidenceIds: Array.isArray(source.graphEvidenceIds ?? source.graph_evidence_ids)
                  ? (source.graphEvidenceIds ?? source.graph_evidence_ids) as string[] : [],
                graphRelationIds: Array.isArray(source.graphRelationIds ?? source.graph_relation_ids)
                  ? (source.graphRelationIds ?? source.graph_relation_ids) as string[] : [],
                graphNavigationClaims: Array.isArray(source.graphNavigationClaims ?? source.graph_navigation_claims)
                  ? (source.graphNavigationClaims ?? source.graph_navigation_claims) as string[] : [],
                graphQuotes: Array.isArray(source.graphQuotes ?? source.graph_quotes)
                  ? (source.graphQuotes ?? source.graph_quotes) as string[] : [],
              }));
              return {
                id: Number.isFinite(numericId) ? numericId : Date.now() + index,
                role: raw.role === "assistant" ? "agent" : "user",
                content: String(raw.content ?? ""),
                sources: normalizeSources(rawSources),
                contextSources: normalizeSources(rawContext),
                responseMode: raw.responseMode === "direct" ? "direct" : "research",
                jobId: String(raw.jobId ?? ""),
              } satisfies Message;
            }),
          }));
        setConversations((current) => {
          const byId = new Map(current.map((conversation) => [conversation.id, conversation]));
          for (const incoming of serverConversations) {
            const existing = byId.get(incoming.id);
            const messagesById = new Map((existing?.messages ?? []).map((message) => [message.id, message]));
            for (const message of incoming.messages) messagesById.set(message.id, message);
            byId.set(incoming.id, { ...existing, ...incoming, messages: [...messagesById.values()].sort((a, b) => a.id - b.id) });
          }
          return [...byId.values()];
        });
        const active = serverConversations.find((conversation) => conversation.id === activeConversationId);
        if (active) {
          setMessages((current) => {
            const byId = new Map(current.map((message) => [message.id, message]));
            for (const message of active.messages) byId.set(message.id, message);
            return [...byId.values()].sort((a, b) => a.id - b.id);
          });
        }
        const highest = serverConversations.reduce(
          (value, conversation) => Math.max(value, ...conversation.messages.map((message) => message.id)),
          0,
        );
        nextMessageId.current = Math.max(nextMessageId.current, highest + 1);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setHasLoadedServerConversations(true);
      });
    return () => { cancelled = true; };
  }, [activeConversationId, hasHydrated, jobs]);

  /** 同步追加一条 Agent 消息到当前消息流和本地会话。 */
  function appendAgentMessage(conversationId: string, agentMessage: Message) {
    setMessages((items) => items.some((item) => item.id === agentMessage.id) ? items : [...items, agentMessage]);
    setConversations((items) => items.map((conversation) =>
      conversation.id === conversationId && !conversation.messages.some((message) => message.id === agentMessage.id)
        ? { ...conversation, messages: [...conversation.messages, agentMessage] }
        : conversation,
    ));
  }

  /** 同步更新当前消息流和会话中的管理操作消息。 */
  function updateAgentMessage(messageId: number, updater: (message: Message) => Message) {
    setMessages((items) => items.map((message) => message.id === messageId ? updater(message) : message));
    setConversations((items) => items.map((conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => message.id === messageId ? updater(message) : message),
    })));
  }

  /** 确认后只清理预览时列出的候选记录，后端会再次校验 PDF 状态。 */
  async function confirmMissingPdfCleanup(message: Message) {
    const action = message.maintenanceAction;
    if (!action || action.kind !== "cleanup-missing-pdfs" || action.status !== "pending") return;
    updateAgentMessage(message.id, (current) => ({
      ...current,
      maintenanceAction: current.maintenanceAction ? { ...current.maintenanceAction, status: "running" } : undefined,
    }));
    try {
      const response = await fetch(buildApiUrl("/api/papers/cleanup-missing-pdfs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: action.candidateIds }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "清理无 PDF 文献失败");
      const removedCount = Number(payload.removedCount ?? 0);
      const keptCount = Number(payload.keptCount ?? 0);
      const referenceCount = Number(payload.removedProjectReferenceCount ?? 0);
      updateAgentMessage(message.id, (current) => ({
        ...current,
        content: `清理完成：删除 ${removedCount} 条无有效本地 PDF 的文献记录，并移除 ${referenceCount} 条项目关联。${keptCount ? `另有 ${keptCount} 条记录因已绑定有效 PDF 而被保留。` : ""}`,
        maintenanceAction: current.maintenanceAction ? { ...current.maintenanceAction, status: "completed" } : undefined,
      }));
      await refreshProjects();
    } catch (cleanupError) {
      updateAgentMessage(message.id, (current) => ({
        ...current,
        content: `清理失败：${cleanupError instanceof Error ? cleanupError.message : "未知错误"}。候选记录尚未删除，你可以重试或取消。`,
        maintenanceAction: current.maintenanceAction ? { ...current.maintenanceAction, status: "pending" } : undefined,
      }));
    }
  }

  /** 取消当前清理确认，不调用任何删除接口。 */
  function cancelMissingPdfCleanup(message: Message) {
    updateAgentMessage(message.id, (current) => ({
      ...current,
      content: "已取消清理，没有删除任何文献记录。",
      maintenanceAction: current.maintenanceAction ? { ...current.maintenanceAction, status: "cancelled" } : undefined,
    }));
  }

  /** 提交持久化研究任务；后台完成后直接写入对应会话。 */
  async function send(value = input) {
    const prompt = value.trim();
    if (!prompt || thinking) return;
    const explicitPreferredName = detectExplicitPreferredName(prompt);
    if (explicitPreferredName) {
      try {
        const updatedPreferences = await saveUserPreferences({ preferredName: explicitPreferredName });
        setPreferences(updatedPreferences);
        setPreferenceDraft({
          preferredName: updatedPreferences.preferredName,
          answerStyle: updatedPreferences.answerStyle,
        });
      } catch (error) {
        setMemoryError(error instanceof Error ? error.message : "保存称呼偏好失败");
      }
    }
    const id = nextMessageId.current;
    nextMessageId.current += 2;
    const userMessage: Message = { id, role: "user", content: prompt };
    const conversationId = activeConversationId || `local-${id}`;
    const title = activeConversationId ? conversationTitle : prompt.length > 22 ? `${prompt.slice(0, 22)}…` : prompt;
    const selectedProjectIds = listUniqueProjectIds(activeProjectId, projectScopeIds);
    setMessages((items) => [...items, userMessage]);
    setConversations((items) => {
      const existing = items.find((conversation) => conversation.id === conversationId);
      if (existing) {
        return items.map((conversation) =>
          conversation.id === conversationId
            ? { ...conversation, projectId: activeProjectId, projectIds: selectedProjectIds, date: "刚刚", messages: [...conversation.messages, userMessage] }
            : conversation,
        );
      }
      return [{ id: conversationId, title, date: "刚刚", messages: [userMessage], projectId: activeProjectId, projectIds: selectedProjectIds }, ...items];
    });
    if (!activeConversationId) {
      setActiveConversationId(conversationId);
      setConversationTitle(title);
    }
    setInput("");
    setThinking(true);
    setSelectedSource(null);
    setActiveResearchJobId("");
    setThinkingText("正在判断如何处理你的问题…");
    try {
      if (isMissingPdfCleanupRequest(prompt)) {
        setThinkingText("正在扫描数据集中的本地 PDF 状态…");
        const response = await fetch(buildApiUrl("/api/papers/cleanup-missing-pdfs/preview"), { cache: "no-store" });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "扫描无 PDF 文献失败");
        const candidates = (Array.isArray(payload.candidates) ? payload.candidates : []) as Array<{ id?: string; title?: string }>;
        const candidateIds = candidates.map((candidate) => String(candidate.id || "").trim()).filter(Boolean);
        const previewTitles = candidates.slice(0, 8).map((candidate, index) => `${index + 1}. ${candidate.title || "未命名文献"}`).join("\n");
        const remainingCount = Math.max(0, candidateIds.length - 8);
        const content = candidateIds.length
          ? `扫描完成：找到 ${candidateIds.length} 条没有有效本地 PDF 的文献记录。\n\n${previewTitles}${remainingCount ? `\n…以及另外 ${remainingCount} 条` : ""}\n\n确认后只会删除这些元数据及其项目关联，不会删除任何已有的有效 PDF 文件。`
          : `扫描完成：当前没有需要清理的记录。数据集中 ${Number(payload.keptCount ?? 0)} 条文献都已绑定有效的本地 PDF。`;
        appendAgentMessage(conversationId, {
          id: id + 1,
          role: "agent",
          content,
          responseMode: "direct",
          maintenanceAction: candidateIds.length ? {
            kind: "cleanup-missing-pdfs",
            candidateIds,
            status: "pending",
          } : undefined,
        });
        return;
      }
      const job = await submitJob("research_chat", {
        question: prompt,
        project_id: activeProjectId,
        project_ids: selectedProjectIds,
        title,
        history: usableHistory(messages).slice(-8).map((message) => ({
          role: message.role === "agent" ? "assistant" : "user",
          content: message.content,
          sources: message.role === "agent"
            ? (message.contextSources ?? message.sources ?? []).map((source) => ({
                index: source.index,
                record_id: source.recordId ?? "",
                title: source.title,
                year: source.year ?? "",
                section: source.section ?? "",
                chunk_index: source.chunkIndex ?? 0,
                excerpt: source.excerpt ?? "",
              }))
            : [],
        })),
      }, {
        conversationId,
        messageId: String(id),
        responseMessageId: String(id + 1),
      });
      setActiveResearchJobId(job.jobId);
      let current = job;
      while (!["completed", "failed", "cancelled", "interrupted"].includes(current.status)) {
        setThinkingText(current.message || "研究任务正在后台执行…");
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
        current = await fetchJob(job.jobId);
      }
      if (current.status !== "completed" || !current.result) {
        throw new Error(current.error || current.message || "研究任务未完成");
      }
      const result = current.result as unknown as OrchestratorResult;
      const payload = result.result;
      const materialText = payload.requiredMaterials?.map((item, index) => `${index + 1}. ${item.description}`).join("\n");
      const needsUserHelp = payload.status === "needs_materials" || payload.status === "needs_user_action";
      const content = needsUserHelp
        ? `${payload.message || "当前流程需要你的协助。"}${materialText ? `\n\n建议补充：\n${materialText}` : ""}`
        : payload.answer || "研究任务已完成，但没有返回可展示的回答。";
      const agentMessage: Message = {
        id: id + 1,
        role: "agent",
        content,
        sources: payload.sources,
        contextSources: payload.retrievedSources ?? payload.sources,
        responseMode: result.action === "direct" ? "direct" : "research",
        jobId: current.jobId,
      };
      appendAgentMessage(conversationId, agentMessage);
    } catch (error) {
      const agentMessage: Message = { id: id + 1, role: "agent", content: error instanceof Error ? `请求失败：${error.message}` : "研究对话请求失败，请稍后重试。" };
      appendAgentMessage(conversationId, agentMessage);
    } finally {
      setThinking(false);
    }
  }

  /** 处理研究问题表单提交。 */
  function submit(event: FormEvent) { event.preventDefault(); send(); }
  /** 处理输入框回车发送和换行行为。 */
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } }

  /** 打开引用证据详情；由详情层继续定位原文或展示图谱导航依据。 */
  function openSource(source: Source) {
    setSelectedSource((current) => (
      current?.index === source.index && current.recordId === source.recordId ? null : source
    ));
  }

  function openSourceChunk(source: Source) {
    const recordId = source.recordId?.trim();
    if (!recordId) return;
    const query = new URLSearchParams();
    if (Number.isInteger(source.chunkIndex)) query.set("chunk", String(source.chunkIndex));
    const suffix = query.size ? `?${query.toString()}` : "";
    window.location.assign(`/dataset-brower/view/${encodeURIComponent(recordId)}${suffix}`);
  }

  /** 切换到指定历史会话。 */
  function openConversation(id: string) {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation || thinking) return;
    if (conversation.projectId && projects.some((project) => project.id === conversation.projectId)) {
      selectProject(conversation.projectId);
    }
    setProjectScopeIds(listUniqueProjectIds(
      conversation.projectId || activeProjectId,
      conversation.projectIds ?? [],
    ));
    setIsProjectScopeOpen(false);
    setWorkspaceView("chat");
    setActiveConversationId(conversation.id);
    setConversationTitle(conversation.title);
    setMessages(conversation.messages);
    setSelectedSource(null);
    setActiveResearchJobId([...conversation.messages].reverse().find((message) => message.jobId)?.jobId ?? "");
    setInput("");
  }

  /** 创建并激活一个空白研究会话。 */
  function startNewConversation() {
    if (thinking) return;
    setWorkspaceView("chat");
    setActiveConversationId("");
    setConversationTitle("新建研究对话");
    setMessages([]);
    setSelectedSource(null);
    setActiveResearchJobId("");
    setProjectScopeIds([activeProjectId]);
    setIsProjectScopeOpen(false);
    setInput("");
  }

  /** 在最近对话列表中开始编辑标题，避免依赖浏览器原生弹窗。 */
  function beginRenameConversation(conversation: Conversation) {
    if (thinking) return;
    setRenamingConversationId(conversation.id);
    setRenameDraft(conversation.title);
    setRenameError("");
  }

  /** 同时更新后端会话与前端缓存，防止下次同步恢复为旧标题。 */
  async function saveConversationRename(id: string) {
    const title = renameDraft.trim().slice(0, 80);
    if (!title) {
      setRenameError("标题不能为空");
      return;
    }
    try {
      const response = await fetch(buildApiUrl(`/api/conversations/${encodeURIComponent(id)}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      if (!response.ok && response.status !== 404) throw new Error(`重命名失败（${response.status}）`);
      setConversations((items) => items.map((conversation) => (
        conversation.id === id ? { ...conversation, title } : conversation
      )));
      if (activeConversationId === id) setConversationTitle(title);
      setRenamingConversationId("");
      setRenameDraft("");
      setRenameError("");
    } catch (error) {
      setRenameError(error instanceof Error ? error.message : "重命名失败，请稍后重试");
    }
  }

  function cancelConversationRename() {
    setRenamingConversationId("");
    setRenameDraft("");
    setRenameError("");
  }

  /** 为当前对话添加或移除检索项目；移除主项目时自动提升下一个项目。 */
  function toggleScopeProject(projectId: string) {
    if (thinking) return;
    const isSelected = projectScopeIds.includes(projectId);
    if (isSelected && projectScopeIds.length === 1) return;
    const next = isSelected
      ? projectScopeIds.filter((value) => value !== projectId)
      : [...projectScopeIds, projectId];
    const nextPrimaryProjectId = next.includes(activeProjectId) ? activeProjectId : next[0];
    if (nextPrimaryProjectId !== activeProjectId) selectProject(nextPrimaryProjectId);
    setProjectScopeIds(next);
    if (activeConversationId) {
      setConversations((items) => items.map((conversation) => conversation.id === activeConversationId
        ? { ...conversation, projectId: nextPrimaryProjectId, projectIds: next }
        : conversation));
    }
  }

  /** 从本地状态移除已持久化删除的会话，并选择新的活动会话。 */
  function removeConversationLocally(id: string) {
    const remaining = conversations.filter((conversation) => conversation.id !== id);
    setConversations(remaining);
    if (activeConversationId !== id) return;
    const nextConversation = remaining[0];
    if (nextConversation) {
      if (nextConversation.projectId && projects.some((project) => project.id === nextConversation.projectId)) {
        selectProject(nextConversation.projectId);
      }
      setProjectScopeIds(listUniqueProjectIds(
        nextConversation.projectId || activeProjectId,
        nextConversation.projectIds ?? [],
      ));
      setActiveConversationId(nextConversation.id);
      setConversationTitle(nextConversation.title);
      setMessages(nextConversation.messages);
      setActiveResearchJobId([...nextConversation.messages].reverse().find((message) => message.jobId)?.jobId ?? "");
    } else {
      setActiveConversationId("");
      setConversationTitle("新建研究对话");
      setMessages([]);
      setActiveResearchJobId("");
      setProjectScopeIds([activeProjectId]);
    }
    setIsProjectScopeOpen(false);
    setInput("");
  }

  /** 先删除后端持久化会话，再同步更新前端缓存，避免列表同步时恢复已删项。 */
  async function deleteConversation(id: string) {
    if (thinking || deletingConversationId) return;
    // 删除请求完成前可能仍有旧的列表请求返回；墓碑可阻止旧响应把该项重新合并回来。
    deletedConversationIds.current.add(id);
    setDeletingConversationId(id);
    setDeleteError("");
    try {
      const response = await fetch(buildApiUrl(`/api/conversations/${encodeURIComponent(id)}`), {
        method: "DELETE",
      });
      // 清理确认等纯本地对话从未写入后端，404 时仍应允许移除本地副本。
      if (!response.ok && response.status !== 404) throw new Error(`删除失败（${response.status}）`);
      removeConversationLocally(id);
    } catch (error) {
      deletedConversationIds.current.delete(id);
      setDeleteError(error instanceof Error ? error.message : "删除失败，请稍后重试");
    } finally {
      setDeletingConversationId("");
    }
  }

  /** 把回答保存为研究归档记录。 */
  async function saveResearchRecord(message: Message) {
    if (message.role !== "agent") return;
    const messageIndex = messages.findIndex((item) => item.id === message.id);
    const question = [...messages.slice(0, messageIndex)].reverse().find((item) => item.role === "user")?.content || conversationTitle;
    const existingRecord = researchRecords.find(
      (record) => record.sourceConversationId === activeConversationId && record.sourceMessageId === String(message.id),
    );
    if (existingRecord) {
      editResearchRecord(existingRecord);
      return;
    }
    setMemoryBusy(true);
    setMemoryError("");
    try {
      const candidate = await extractResearchMemory({
        question,
        answer: message.content,
        sources: message.sources ?? [],
        projectId: activeProjectId,
        projectName: activeProject?.name ?? "",
        sourceConversationId: activeConversationId,
        sourceMessageId: String(message.id),
      });
      setEditingMemoryId("");
      setMemoryDraft(candidate);
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "提炼研究记忆失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  /** 打开研究归档关联的原始会话。 */
  function openRecordConversation(record: ResearchRecord) {
    if (record.sourceConversationId && conversations.some((conversation) => conversation.id === record.sourceConversationId)) {
      openConversation(record.sourceConversationId);
    }
  }

  /** 删除指定研究归档记录。 */
  function editResearchRecord(record: ResearchRecord) {
    setEditingMemoryId(record.id);
    setMemoryDraft({
      projectId: record.projectId,
      projectName: record.projectName,
      type: record.type,
      title: record.title,
      summary: record.summary,
      tags: record.tags,
      confidence: record.confidence,
      evidence: record.evidence,
      sourceConversationId: record.sourceConversationId,
      sourceMessageId: record.sourceMessageId,
      sourceQuestion: record.sourceQuestion,
    });
    setMemoryError("");
  }

  async function confirmResearchMemory() {
    if (!memoryDraft || memoryBusy) return;
    setMemoryBusy(true);
    setMemoryError("");
    try {
      if (editingMemoryId) {
        const updated = await updateResearchMemory(editingMemoryId, memoryDraft);
        setResearchRecords((items) => items.map((item) => item.id === updated.id ? updated : item));
      } else {
        const created = await createResearchMemory(memoryDraft);
        setResearchRecords((items) => [created, ...items]);
      }
      setMemoryDraft(null);
      setEditingMemoryId("");
      setWorkspaceView("records");
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "保存研究记忆失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function deleteResearchRecord(id: string) {
    setMemoryError("");
    try {
      await deleteResearchMemoryApi(id);
      setResearchRecords((items) => items.filter((record) => record.id !== id));
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "删除研究记忆失败");
    }
  }

  async function persistPreferences(patch = preferenceDraft) {
    setMemoryBusy(true);
    setMemoryError("");
    try {
      const updated = await saveUserPreferences(patch);
      setPreferences(updated);
      setPreferenceDraft({ preferredName: updated.preferredName, answerStyle: updated.answerStyle });
      setIsPreferenceOpen(false);
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "保存全局偏好失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  return (
    <div className={`research-chat ${sidebar ? "" : "is-collapsed"}${workspaceView === "records" ? " is-records" : ""}`}>
      <aside className="research-sidebar">
        <div className="research-brand"><span className="research-agent-mark"><AutoAwesomeRounded /></span><div><small>知识驱动的研究伙伴</small></div><button onClick={() => setSidebar(false)}><MenuRounded /></button></div>
        <button className="research-new" onClick={startNewConversation}><AddRounded />新建研究对话</button>
        <label>工作区</label>
        <nav className="research-nav">
          <button className={workspaceView === "chat" ? "active" : ""} onClick={() => setWorkspaceView("chat")}><AutoAwesomeRounded />研究对话{conversations.length > 0 ? <em>{conversations.length}</em> : null}</button>
          <button className={workspaceView === "records" ? "active" : ""} onClick={() => setWorkspaceView("records")}><HistoryRounded />研究记忆{researchRecords.length > 0 ? <em>{researchRecords.length}</em> : null}</button>
        </nav>
        <label>最近对话</label>
        <div className="research-recents">
          {conversations.map((conversation) => (
            <div className={`research-recent-item ${activeConversationId === conversation.id ? "active" : ""}`} key={conversation.id}>
              {renamingConversationId === conversation.id ? <form className="research-recent-edit" onSubmit={(event) => { event.preventDefault(); void saveConversationRename(conversation.id); }}><input value={renameDraft} onChange={(event) => setRenameDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") cancelConversationRename(); }} maxLength={80} autoFocus aria-label="对话标题" title="按 Enter 保存" /><button type="button" onClick={cancelConversationRename} aria-label="取消重命名" title="取消重命名"><CloseRounded /></button>{renameError ? <small>{renameError}</small> : null}</form> : <><button className="research-recent-open" onClick={() => openConversation(conversation.id)}><span>{conversation.title}</span><small>{[projects.find((project) => project.id === conversation.projectId)?.name, conversation.date].filter(Boolean).join(" · ")}</small></button><div className="research-recent-actions"><button className="research-recent-rename" onClick={() => beginRenameConversation(conversation)} aria-label={`重命名对话：${conversation.title}`} title="重命名对话"><EditRounded /></button><button className="research-recent-delete" disabled={deletingConversationId === conversation.id} onClick={() => void deleteConversation(conversation.id)} aria-label={`删除对话：${conversation.title}`} title="删除对话"><DeleteOutlineRounded /></button></div></>}
            </div>
          ))}
          {deleteError ? <div className="research-recent-error" role="alert">{deleteError}</div> : null}
          {conversations.length === 0 ? (
            <div className="research-recent-empty">
              {hasHydrated && hasLoadedServerConversations ? "暂无最近对话" : "正在恢复最近对话…"}
            </div>
          ) : null}
        </div>
        <div className="research-side-bottom"><div><span>LX</span><p><strong>研究工作区</strong><small>个人专业版</small></p></div></div>
      </aside>

      <main className="research-main">
        <header className="research-topbar">
          {!sidebar && <button className="research-icon" onClick={() => setSidebar(true)}><MenuRounded /></button>}
          <div className="research-topbar-title"><h1>{workspaceView === "records" ? "成果档案" : conversationTitle}</h1><span><i />{workspaceView === "records" ? `${researchRecords.length} 条已保存成果` : "已自动保存"}</span></div>
          <div className="research-topbar-actions">
            <button className="research-export" onClick={() => setIsPreferenceOpen(true)}><EditRounded />{preferences.preferredName ? `称呼：${preferences.preferredName}` : "全局偏好"}</button>
            {workspaceView === "chat" ? <button className="research-export"><DownloadRounded />导出报告</button> : null}
          </div>
        </header>
        {workspaceView === "records" ? (
          <section className="research-records-page">
            <header><div><small>Research Memory</small><h2>研究记忆</h2><p>从对话中提炼可复用的结论、事实、决策、限制和待办；完整回答继续保留在原对话中。</p></div></header>
            {memoryError ? <p className="research-memory-error" role="alert">{memoryError}</p> : null}
            {isLoadingMemories ? (
              <div className="research-record-empty"><HistoryRounded /><h3>正在读取研究记忆…</h3></div>
            ) : researchRecords.length > 0 ? (
              <div className="research-record-grid">
                {researchRecords.map((record) => (
                  <article className="research-record-card" key={record.id}>
                    <header><div><small>{record.createdAt}</small><h3>{record.title}</h3></div><button onClick={() => deleteResearchRecord(record.id)} aria-label={`删除研究记录：${record.title}`} title="删除研究记录"><DeleteOutlineRounded /></button></header>
                    <strong>{record.sourceQuestion}</strong>
                    <p>{record.summary}</p>
                    {record.tags.length ? <div className="research-memory-tags">{record.tags.map((tag) => <span key={tag}>{tag}</span>)}</div> : null}
                    <footer>
                      <span>{[record.projectName, record.type, `${record.evidence.length} 个证据`, `置信度 ${Math.round(record.confidence * 100)}%`].filter(Boolean).join(" · ")}</span>
                      <div className="research-memory-actions">
                        <button onClick={() => editResearchRecord(record)}>编辑</button>
                        <button onClick={() => openRecordConversation(record)} disabled={!record.sourceConversationId || !conversations.some((conversation) => conversation.id === record.sourceConversationId)}>打开原对话</button>
                      </div>
                    </footer>
                  </article>
                ))}
              </div>
            ) : (
              <div className="research-record-empty"><HistoryRounded /><h3>暂无研究记忆</h3><p>在研究回答下方点击“提炼研究记忆”，确认摘要后会出现在这里。</p><button onClick={() => setWorkspaceView("chat")}>返回研究对话</button></div>
            )}
          </section>
        ) : <div className="research-body">
          <section className="research-thread">
            {!messages.length && (
              hasHydrated && hasLoadedServerConversations
                ? <div className="research-empty"><span className="research-agent-mark"><AutoAwesomeRounded /></span><h2>今天想研究什么？</h2><p>我会从{projectScopeIds.length > 1 ? `所选 ${projectScopeIds.length} 个项目` : `“${activeProject?.name || "当前知识空间"}”`}的文献中检索、分析并标注每一处引用。</p></div>
                : <div className="research-empty"><span className="research-agent-mark"><AutoAwesomeRounded /></span><h2>正在恢复研究对话…</h2></div>
            )}
            {messages.map((message) => <article className={`research-message ${message.role}`} key={message.id}>
              <div className="research-avatar">{message.role === "agent" ? <AutoAwesomeRounded /> : "LX"}</div>
              <div><header><strong>{message.role === "agent" ? "Research Agent" : "你"}</strong><span>{message.id <= 2 ? "10:24" : "刚刚"}</span></header><MessageContent content={message.content} />
                {message.role === "agent" && <>{message.sources?.length ? <div className="research-citations">{message.sources.map((source) => <button type="button" disabled={!source.recordId} onClick={() => openSource(source)} title={source.recordId ? `查看文献：${source.title}` : "该来源没有本地文献记录"} key={`${message.id}-${source.index}`}>{source.index} · {source.title}</button>)}</div> : null}{message.maintenanceAction?.status === "pending" || message.maintenanceAction?.status === "running" ? <div className="research-maintenance-action"><button type="button" className="is-danger" disabled={message.maintenanceAction.status === "running"} onClick={() => void confirmMissingPdfCleanup(message)}>{message.maintenanceAction.status === "running" ? "正在清理…" : `确认删除 ${message.maintenanceAction.candidateIds.length} 条`}</button><button type="button" disabled={message.maintenanceAction.status === "running"} onClick={() => cancelMissingPdfCleanup(message)}>取消</button></div> : null}<footer><button onClick={() => { navigator.clipboard?.writeText(message.content); setCopied(message.id); }}><ContentCopyRounded />{copied === message.id ? "已复制" : "复制"}</button><button><ThumbUpAltOutlined />有帮助</button><button disabled={memoryBusy} onClick={() => void saveResearchRecord(message)}><BookmarkAddOutlined />{researchRecords.some((record) => record.sourceConversationId === activeConversationId && record.sourceMessageId === String(message.id)) ? "编辑研究记忆" : memoryBusy ? "正在提炼…" : "提炼研究记忆"}</button></footer></>}
              </div>
            </article>)}
            {thinking && <div className="research-thinking"><i /><i /><i />{thinkingText}</div>}
          </section>
          <div className="research-compose-wrap">
            {isProjectScopeOpen ? (
              <section className="research-project-scope-menu" role="dialog" aria-label="选择当前对话的检索项目">
                <header><span className="research-project-scope-icon"><FolderOpenRounded /></span><div><strong>检索项目</strong><small>组合多个项目，后续追问将持续检索它们的文献并集。</small></div><button type="button" onClick={() => setIsProjectScopeOpen(false)} aria-label="关闭项目选择"><CloseRounded /></button></header>
                <div className="research-project-scope-summary"><span>当前范围</span><strong>{projectScopeIds.length} 个项目</strong></div>
                <div className="research-project-scope-list">{projects.map((project) => { const isSelected = projectScopeIds.includes(project.id); return <label className={isSelected ? "is-selected" : ""} key={project.id}><input type="checkbox" checked={isSelected} disabled={thinking || (isSelected && projectScopeIds.length === 1)} onChange={() => toggleScopeProject(project.id)} /><span><strong>{project.name}</strong><small>{project.paperCount} 篇文献{project.id === activeProjectId ? " · 当前主项目" : ""}</small></span>{isSelected ? <CheckRounded /> : null}</label>; })}</div>
                <footer><span>{isLoadingProjects ? "正在读取项目…" : "至少保留一个项目作为检索范围"}</span><button type="button" onClick={() => setIsProjectScopeOpen(false)}>完成</button></footer>
              </section>
            ) : null}
            <form className="research-compose" onSubmit={submit}><textarea rows={2} value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={keyDown} placeholder={messages.length ? "继续追问，或给 Research Agent 一个任务…" : "输入研究问题，或给 Research Agent 一个任务…"} /><div><button type="button" className="research-source-action" onClick={() => setIsProjectScopeOpen((current) => !current)} aria-expanded={isProjectScopeOpen} aria-label="添加检索项目" title="添加其他项目到当前对话的检索范围"><AddRounded />项目</button><button type="button" className="library-pill" onClick={onOpenDomainTree} aria-label={`管理项目文献：${activeProject?.name || "当前项目"}`} title="在项目知识空间管理项目文献"><FolderOpenRounded />{activeProject?.name || "当前项目"} <span>{projectScopeIds.length > 1 ? `${projectScopeIds.length} 个项目` : isLoadingProjects ? "读取中…" : `${activeProject?.paperCount ?? 0} 篇`}</span></button><button className="research-send" disabled={!input.trim() || thinking}><SendRounded /></button></div></form>
            <small className="research-note">{isDirectAnswer ? "本次为普通对话，未调用研究 Agent 或知识库。" : "研究内容由 AI 基于所选知识库生成，请核验关键结论与原始文献。"}</small>
          </div>
        </div>}
      </main>

      {workspaceView === "chat" ? <aside className="research-context">
        <header><div><small>{isDirectAnswer ? "本次对话" : "本次研究"}</small><strong>{isDirectAnswer ? "回答方式" : "上下文与来源"}</strong></div><MoreHorizRounded /></header>
        {isDirectAnswer ? (
          <section className="research-progress-card"><div><span><AutoAwesomeRounded /></span><p><strong>直接回答</strong><small>无需调用研究 Agent 或检索论文</small></p><CheckRounded /></div></section>
        ) : <>
          <section className={`research-progress-card is-${researchStatus}`}>
            <button type="button" className="research-progress-summary" onClick={openCenter} title="在后台任务中心查看研究任务详情">
              <span><SearchRounded /></span>
              <p><strong>深度研究</strong><small>{researchStatus === "completed" ? `已引用 ${activeSources.length} 个来源 · 点击查看任务详情` : researchStatusText}</small></p>
              {researchStatus === "completed" ? <CheckRounded /> : researchStatus === "failed" ? <CloseRounded /> : <em>{researchProgress}%</em>}
            </button>
            <progress value={researchProgress} max="100" />
            <ul>{RESEARCH_STAGE_ITEMS.map((item) => {
              const itemIndex = RESEARCH_STAGE_ORDER.indexOf(item.stage);
              const isComplete = researchStatus === "completed" || currentStageIndex > itemIndex;
              const isActive = researchStatus !== "completed" && researchStatus !== "failed" && currentStageIndex === itemIndex;
              return <li className={isComplete ? "is-complete" : isActive ? "is-active" : "is-pending"} key={item.stage}>{isComplete ? <CheckRounded /> : <i />}{item.label}</li>;
            })}</ul>
            {researchDiagnostics ? <div className="research-progress-metrics">
              <span><strong>{researchDiagnostics.evidenceCount ?? 0}</strong> 条证据</span>
              <span><strong>{researchDiagnostics.distinctPaperCount ?? 0}</strong> 篇文献</span>
              <span><strong>{Math.round((researchDiagnostics.facetCoverage ?? 0) * 100)}%</strong> 检索维度覆盖</span>
              {researchDiagnostics.retrievalMode ? <small>{researchDiagnostics.retrievalMode}</small> : null}
            </div> : null}
          </section>
          <div className="research-section-title"><strong>引用来源</strong><button onClick={onOpenBrowse}>查看全部</button></div>
          <div className="research-sources">
            {activeSources.length ? activeSources.map((source) => (
              <div className={`research-source-item${selectedSource?.index === source.index && selectedSource.recordId === source.recordId ? " is-expanded" : ""}`} key={`context-source-${source.index}`}>
                <button type="button" disabled={!source.recordId} onClick={() => openSource(source)} title={source.recordId ? `查看引用证据：${source.title}` : "该来源没有本地文献记录"}>
                  <span><ArticleOutlined /></span>
                  <p><strong>{source.title}</strong><small>{[source.source, source.year].filter(Boolean).join(" · ") || "本地知识库"}</small></p>
                  <em>{source.index}</em>
                </button>
                {selectedSource?.index === source.index && selectedSource.recordId === source.recordId ? <section className="research-source-inline-detail">
                  <div className="research-source-inline-meta">
                    <strong>{source.graphBacked ? "图谱增强证据" : "全文检索片段"}</strong>
                    {Number.isInteger(source.chunkIndex) ? <span>分块 {Number(source.chunkIndex) + 1}</span> : null}
                  </div>
                  {source.section ? <small className="research-source-inline-section">{source.section}</small> : null}
                  <blockquote>{source.excerpt || "该引用没有返回可预览的原文，请打开文献查看对应分块。"}</blockquote>
                  {source.graphBacked ? <div className="research-source-inline-graph">
                    <strong>知识图谱依据</strong>
                    {(source.graphNavigationClaims ?? []).length ? <ul>{source.graphNavigationClaims?.map((claim) => <li key={claim}>{claim}</li>)}</ul> : <p>该片段由知识图谱关系导航命中。</p>}
                    {(source.graphQuotes ?? []).slice(0, 2).map((quote, index) => <blockquote key={`${source.index}-graph-quote-${index}`}>{quote}</blockquote>)}
                    <small>{(source.graphRelationIds ?? []).length} 条关系 · {(source.graphEvidenceIds ?? []).length} 条图谱证据</small>
                  </div> : null}
                  <footer>
                    {source.graphBacked ? <button type="button" onClick={onOpenDomainTree}>知识图谱</button> : null}
                    <button type="button" className="is-primary" disabled={!source.recordId} onClick={() => openSourceChunk(source)}>查看原文分块</button>
                  </footer>
                </section> : null}
              </div>
            )) : <div className="research-source-empty">发送研究问题后，此处将显示回答引用的真实来源。</div>}
          </div>
          <div className="research-section-title"><strong>知识库范围</strong><button onClick={onOpenBrowse}>管理</button></div>
          <div className="research-library"><span><FolderOpenRounded /></span><p><strong>{scopedProjects.map((project) => project.name).join("、") || activeProject?.name || "当前知识空间"}</strong><small>{projectScopeIds.length} 个项目 · 文献并集作为当前对话范围</small></p><CheckRounded /></div>
          <button className="research-add-source" onClick={() => setIsProjectScopeOpen(true)}><AddRounded />添加检索项目</button>
        </>}
      </aside> : null}

      {memoryDraft ? <div className="research-memory-overlay" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget && !memoryBusy) setMemoryDraft(null);
      }}>
        <section className="research-memory-dialog" role="dialog" aria-modal="true" aria-labelledby="research-memory-dialog-title">
          <header>
            <div><small>Research Memory</small><h2 id="research-memory-dialog-title">{editingMemoryId ? "编辑研究记忆" : "确认本轮提炼"}</h2></div>
            <button type="button" disabled={memoryBusy} onClick={() => setMemoryDraft(null)}><CloseRounded /></button>
          </header>
          <p className="research-memory-hint">这里只保存可复用的结论摘要；完整回答仍保留在原对话中。</p>
          <label>类型
            <select value={memoryDraft.type} onChange={(event) => setMemoryDraft({ ...memoryDraft, type: event.target.value as ResearchMemoryDraft["type"] })}>
              <option value="conclusion">研究结论</option>
              <option value="fact">关键事实</option>
              <option value="decision">决策记录</option>
              <option value="limitation">争议与限制</option>
              <option value="hypothesis">待验证假设</option>
              <option value="task">后续任务</option>
            </select>
          </label>
          <label>标题
            <input value={memoryDraft.title} maxLength={200} onChange={(event) => setMemoryDraft({ ...memoryDraft, title: event.target.value })} />
          </label>
          <label>提炼摘要
            <textarea value={memoryDraft.summary} maxLength={1200} rows={7} onChange={(event) => setMemoryDraft({ ...memoryDraft, summary: event.target.value })} />
          </label>
          <label>标签（使用逗号分隔）
            <input value={memoryDraft.tags.join(", ")} onChange={(event) => setMemoryDraft({
              ...memoryDraft,
              tags: event.target.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean).slice(0, 8),
            })} />
          </label>
          <div className="research-memory-meta">保留 {memoryDraft.evidence.length} 条证据链接 · 置信度 {Math.round(memoryDraft.confidence * 100)}%</div>
          {memoryError ? <p className="research-memory-error" role="alert">{memoryError}</p> : null}
          <footer>
            <button type="button" disabled={memoryBusy} onClick={() => setMemoryDraft(null)}>不保存</button>
            <button type="button" className="is-primary" disabled={memoryBusy || !memoryDraft.title.trim() || !memoryDraft.summary.trim()} onClick={() => void confirmResearchMemory()}>{memoryBusy ? "正在保存…" : "确认保存"}</button>
          </footer>
        </section>
      </div> : null}

      {isPreferenceOpen ? <div className="research-memory-overlay" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget && !memoryBusy) setIsPreferenceOpen(false);
      }}>
        <section className="research-memory-dialog is-preferences" role="dialog" aria-modal="true" aria-labelledby="user-preference-dialog-title">
          <header>
            <div><small>Global Preferences</small><h2 id="user-preference-dialog-title">全局用户偏好</h2></div>
            <button type="button" disabled={memoryBusy} onClick={() => setIsPreferenceOpen(false)}><CloseRounded /></button>
          </header>
          <p className="research-memory-hint">这些设置跨对话生效，但不会混入项目研究结论。系统只保存你明确提供的偏好。</p>
          <label>希望如何称呼你
            <input value={preferenceDraft.preferredName} maxLength={80} placeholder="例如：林老师" onChange={(event) => setPreferenceDraft({ ...preferenceDraft, preferredName: event.target.value })} />
          </label>
          <label>回答风格
            <textarea value={preferenceDraft.answerStyle} maxLength={200} rows={4} placeholder="例如：先给结论，使用简洁中文" onChange={(event) => setPreferenceDraft({ ...preferenceDraft, answerStyle: event.target.value })} />
          </label>
          {memoryError ? <p className="research-memory-error" role="alert">{memoryError}</p> : null}
          <footer>
            <button type="button" disabled={memoryBusy} onClick={() => setIsPreferenceOpen(false)}>取消</button>
            <button type="button" className="is-primary" disabled={memoryBusy} onClick={() => void persistPreferences()}>{memoryBusy ? "正在保存…" : "保存全局偏好"}</button>
          </footer>
        </section>
      </div> : null}
    </div>
  );
}
