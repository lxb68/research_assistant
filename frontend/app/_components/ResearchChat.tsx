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
import DataObjectRounded from "@mui/icons-material/DataObjectRounded";
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
};
type Message = {
  id: number;
  role: "user" | "agent";
  content: string;
  sources?: Source[];
  contextSources?: Source[];
  responseMode?: "direct" | "research";
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
  };
};

type Conversation = { id: string; title: string; date: string; messages: Message[]; projectId?: string };
type ResearchRecord = {
  id: string;
  conversationId: string;
  messageId: number;
  title: string;
  question: string;
  content: string;
  sources: Source[];
  createdAt: string;
  projectId?: string;
  projectName?: string;
};

const CONVERSATIONS_KEY = "research-agent.conversations";
// 三组键分别保存会话列表、当前会话和归档研究记录。
const ACTIVE_CONVERSATION_KEY = "research-agent.active-conversation";
const RESEARCH_RECORDS_KEY = "research-agent.research-records";

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

/** 管理研究对话、流式响应和本地会话记录。 */
export default function ResearchChat({ onOpenBrowse, onOpenDomainTree }: Props) {
  const { jobs, submitJob } = useBackgroundTasks();
  const { projects, activeProjectId, activeProject, isLoadingProjects, selectProject, refreshProjects } = useProjects();
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState("");
  const [conversationTitle, setConversationTitle] = useState("新建研究对话");
  const [researchRecords, setResearchRecords] = useState<ResearchRecord[]>([]);
  const [workspaceView, setWorkspaceView] = useState<"chat" | "records">("chat");
  const [hasHydrated, setHasHydrated] = useState(false);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [thinkingText, setThinkingText] = useState("正在检索知识库并组织证据…");
  const [sidebar, setSidebar] = useState(true);
  const [renamingConversationId, setRenamingConversationId] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
  const [renameError, setRenameError] = useState("");
  const [copied, setCopied] = useState<number | null>(null);
  const nextMessageId = useRef(1);
  const activeAgentMessage = [...messages].reverse().find((message) => message.role === "agent");
  const activeSources = activeAgentMessage?.sources ?? [];
  const isDirectAnswer = activeAgentMessage?.responseMode === "direct";

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        const storedConversations = JSON.parse(window.localStorage.getItem(CONVERSATIONS_KEY) || "[]") as Conversation[];
        const storedRecords = JSON.parse(window.localStorage.getItem(RESEARCH_RECORDS_KEY) || "[]") as ResearchRecord[];
        const storedActiveId = window.localStorage.getItem(ACTIVE_CONVERSATION_KEY) || "";
        const activeConversation = storedConversations.find((conversation) => conversation.id === storedActiveId);
        setConversations(storedConversations);
        setResearchRecords(storedRecords);
        if (activeConversation) {
          setActiveConversationId(activeConversation.id);
          setConversationTitle(activeConversation.title);
          setMessages(activeConversation.messages);
        }
        const highestMessageId = storedConversations.reduce(
          (highest, conversation) => Math.max(highest, ...conversation.messages.map((message) => message.id)),
          0,
        );
        nextMessageId.current = highestMessageId + 1;
      } catch {
        window.localStorage.removeItem(CONVERSATIONS_KEY);
        window.localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
        window.localStorage.removeItem(RESEARCH_RECORDS_KEY);
      } finally {
        setHasHydrated(true);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

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
    window.localStorage.setItem(RESEARCH_RECORDS_KEY, JSON.stringify(researchRecords));
  }, [hasHydrated, researchRecords]);

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
        const serverConversations: Conversation[] = (payload.conversations ?? []).map((conversation) => ({
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
            }));
            return {
              id: Number.isFinite(numericId) ? numericId : Date.now() + index,
              role: raw.role === "assistant" ? "agent" : "user",
              content: String(raw.content ?? ""),
              sources: normalizeSources(rawSources),
              contextSources: normalizeSources(rawContext),
              responseMode: raw.responseMode === "direct" ? "direct" : "research",
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
      .catch(() => undefined);
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
    const id = nextMessageId.current;
    nextMessageId.current += 2;
    const userMessage: Message = { id, role: "user", content: prompt };
    const conversationId = activeConversationId || `local-${id}`;
    const title = activeConversationId ? conversationTitle : prompt.length > 22 ? `${prompt.slice(0, 22)}…` : prompt;
    setMessages((items) => [...items, userMessage]);
    setConversations((items) => {
      const existing = items.find((conversation) => conversation.id === conversationId);
      if (existing) {
        return items.map((conversation) =>
          conversation.id === conversationId
            ? { ...conversation, projectId: conversation.projectId || activeProjectId, date: "刚刚", messages: [...conversation.messages, userMessage] }
            : conversation,
        );
      }
      return [{ id: conversationId, title, date: "刚刚", messages: [userMessage], projectId: activeProjectId }, ...items];
    });
    if (!activeConversationId) {
      setActiveConversationId(conversationId);
      setConversationTitle(title);
    }
    setInput("");
    setThinking(true);
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

  /** 切换到指定历史会话。 */
  function openConversation(id: string) {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation || thinking) return;
    if (conversation.projectId && projects.some((project) => project.id === conversation.projectId)) {
      selectProject(conversation.projectId);
    }
    setWorkspaceView("chat");
    setActiveConversationId(conversation.id);
    setConversationTitle(conversation.title);
    setMessages(conversation.messages);
    setInput("");
  }

  /** 创建并激活一个空白研究会话。 */
  function startNewConversation() {
    if (thinking) return;
    setWorkspaceView("chat");
    setActiveConversationId("");
    setConversationTitle("新建研究对话");
    setMessages([]);
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

  /** 切换知识空间时开启空白会话，避免不同项目的历史上下文相互污染。 */
  function changeResearchProject(projectId: string) {
    if (thinking || projectId === activeProjectId) return;
    selectProject(projectId);
    startNewConversation();
  }

  /** 删除指定会话并选择新的活动会话。 */
  function deleteConversation(id: string) {
    if (thinking) return;
    const remaining = conversations.filter((conversation) => conversation.id !== id);
    setConversations(remaining);
    if (activeConversationId !== id) return;
    const nextConversation = remaining[0];
    if (nextConversation) {
      if (nextConversation.projectId && projects.some((project) => project.id === nextConversation.projectId)) {
        selectProject(nextConversation.projectId);
      }
      setActiveConversationId(nextConversation.id);
      setConversationTitle(nextConversation.title);
      setMessages(nextConversation.messages);
    } else {
      setActiveConversationId("");
      setConversationTitle("新建研究对话");
      setMessages([]);
    }
    setInput("");
  }

  /** 把回答保存为研究归档记录。 */
  function saveResearchRecord(message: Message) {
    if (message.role !== "agent") return;
    const messageIndex = messages.findIndex((item) => item.id === message.id);
    const question = [...messages.slice(0, messageIndex)].reverse().find((item) => item.role === "user")?.content || conversationTitle;
    const existingRecord = researchRecords.find(
      (record) => record.conversationId === activeConversationId && record.messageId === message.id,
    );
    if (existingRecord) {
      setWorkspaceView("records");
      return;
    }
    setResearchRecords((items) => [{
      id: `record-${Date.now()}-${message.id}`,
      conversationId: activeConversationId,
      messageId: message.id,
      title: conversationTitle,
      question,
      content: message.content,
      sources: message.sources ?? [],
      createdAt: new Date().toLocaleString("zh-CN"),
      projectId: activeProjectId,
      projectName: activeProject?.name,
    }, ...items]);
  }

  /** 打开研究归档关联的原始会话。 */
  function openRecordConversation(record: ResearchRecord) {
    if (record.conversationId && conversations.some((conversation) => conversation.id === record.conversationId)) {
      openConversation(record.conversationId);
    }
  }

  /** 删除指定研究归档记录。 */
  function deleteResearchRecord(id: string) {
    setResearchRecords((items) => items.filter((record) => record.id !== id));
  }

  return (
    <div className={`research-chat ${sidebar ? "" : "is-collapsed"}${workspaceView === "records" ? " is-records" : ""}`}>
      <aside className="research-sidebar">
        <div className="research-brand"><span className="research-agent-mark"><AutoAwesomeRounded /></span><div><small>知识驱动的研究伙伴</small></div><button onClick={() => setSidebar(false)}><MenuRounded /></button></div>
        <button className="research-new" onClick={startNewConversation}><AddRounded />新建研究对话</button>
        <label>工作区</label>
        <nav className="research-nav">
          <button className={workspaceView === "chat" ? "active" : ""} onClick={() => setWorkspaceView("chat")}><AutoAwesomeRounded />研究对话{conversations.length > 0 ? <em>{conversations.length}</em> : null}</button>
          <button className={workspaceView === "records" ? "active" : ""} onClick={() => setWorkspaceView("records")}><HistoryRounded />研究记录{researchRecords.length > 0 ? <em>{researchRecords.length}</em> : null}</button>
        </nav>
        <label>最近对话</label>
        <div className="research-recents">
          {conversations.map((conversation) => (
            <div className={`research-recent-item ${activeConversationId === conversation.id ? "active" : ""}`} key={conversation.id}>
              {renamingConversationId === conversation.id ? <form className="research-recent-edit" onSubmit={(event) => { event.preventDefault(); void saveConversationRename(conversation.id); }}><input value={renameDraft} onChange={(event) => setRenameDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") cancelConversationRename(); }} maxLength={80} autoFocus aria-label="对话标题" title="按 Enter 保存" /><button type="button" onClick={cancelConversationRename} aria-label="取消重命名" title="取消重命名"><CloseRounded /></button>{renameError ? <small>{renameError}</small> : null}</form> : <><button className="research-recent-open" onClick={() => openConversation(conversation.id)}><span>{conversation.title}</span><small>{[projects.find((project) => project.id === conversation.projectId)?.name, conversation.date].filter(Boolean).join(" · ")}</small></button><div className="research-recent-actions"><button className="research-recent-rename" onClick={() => beginRenameConversation(conversation)} aria-label={`重命名对话：${conversation.title}`} title="重命名对话"><EditRounded /></button><button className="research-recent-delete" onClick={() => deleteConversation(conversation.id)} aria-label={`删除对话：${conversation.title}`} title="删除对话"><DeleteOutlineRounded /></button></div></>}
            </div>
          ))}
          {conversations.length === 0 ? <div className="research-recent-empty">暂无最近对话</div> : null}
        </div>
        <div className="research-side-bottom"><div><span>LX</span><p><strong>研究工作区</strong><small>个人专业版</small></p></div></div>
      </aside>

      <main className="research-main">
        <header className="research-topbar">
          {!sidebar && <button className="research-icon" onClick={() => setSidebar(true)}><MenuRounded /></button>}
          <div className="research-topbar-title"><h1>{workspaceView === "records" ? "成果档案" : conversationTitle}</h1><span><i />{workspaceView === "records" ? `${researchRecords.length} 条已保存成果` : "已自动保存"}</span></div>
          {workspaceView === "chat" ? <><label className="research-project-picker"><DataObjectRounded /><span>知识空间</span><select value={activeProjectId} onChange={(event) => changeResearchProject(event.target.value)} disabled={thinking || isLoadingProjects}>{projects.length ? projects.map((project) => <option key={project.id} value={project.id}>{project.name}（{project.paperCount} 篇）</option>) : <option value={activeProjectId}>{isLoadingProjects ? "正在加载项目…" : "暂无可用项目"}</option>}</select></label><button className="research-export"><DownloadRounded />导出报告</button></> : null}
        </header>
        {workspaceView === "records" ? (
          <section className="research-records-page">
            <header><div><small>Research Archive</small><h2>研究记录</h2><p>沉淀值得长期保留的研究结论、原始问题与引用证据，形成可回溯的成果档案。</p></div></header>
            {researchRecords.length > 0 ? (
              <div className="research-record-grid">
                {researchRecords.map((record) => (
                  <article className="research-record-card" key={record.id}>
                    <header><div><small>{record.createdAt}</small><h3>{record.title}</h3></div><button onClick={() => deleteResearchRecord(record.id)} aria-label={`删除研究记录：${record.title}`} title="删除研究记录"><DeleteOutlineRounded /></button></header>
                    <strong>{record.question}</strong>
                    <p>{record.content}</p>
                    <footer><span>{[record.projectName, `${record.sources.length} 个引用来源`].filter(Boolean).join(" · ")}</span><button onClick={() => openRecordConversation(record)} disabled={!record.conversationId || !conversations.some((conversation) => conversation.id === record.conversationId)}>打开原对话</button></footer>
                  </article>
                ))}
              </div>
            ) : (
              <div className="research-record-empty"><HistoryRounded /><h3>暂无研究记录</h3><p>在研究回答下方点击“保存到研究记录”，成果会出现在这里。</p><button onClick={() => setWorkspaceView("chat")}>返回研究对话</button></div>
            )}
          </section>
        ) : <div className="research-body">
          <section className="research-thread">
            {!messages.length && <div className="research-empty"><span className="research-agent-mark"><AutoAwesomeRounded /></span><h2>今天想研究什么？</h2><p>我会从“{activeProject?.name || "当前知识空间"}”中检索、分析并标注每一处引用。</p></div>}
            {messages.map((message) => <article className={`research-message ${message.role}`} key={message.id}>
              <div className="research-avatar">{message.role === "agent" ? <AutoAwesomeRounded /> : "LX"}</div>
              <div><header><strong>{message.role === "agent" ? "Research Agent" : "你"}</strong><span>{message.id <= 2 ? "10:24" : "刚刚"}</span></header><MessageContent content={message.content} />
                {message.role === "agent" && <>{message.sources?.length ? <div className="research-citations">{message.sources.map((source) => <button key={`${message.id}-${source.index}`}>{source.index} · {source.title}</button>)}</div> : null}{message.maintenanceAction?.status === "pending" || message.maintenanceAction?.status === "running" ? <div className="research-maintenance-action"><button type="button" className="is-danger" disabled={message.maintenanceAction.status === "running"} onClick={() => void confirmMissingPdfCleanup(message)}>{message.maintenanceAction.status === "running" ? "正在清理…" : `确认删除 ${message.maintenanceAction.candidateIds.length} 条`}</button><button type="button" disabled={message.maintenanceAction.status === "running"} onClick={() => cancelMissingPdfCleanup(message)}>取消</button></div> : null}<footer><button onClick={() => { navigator.clipboard?.writeText(message.content); setCopied(message.id); }}><ContentCopyRounded />{copied === message.id ? "已复制" : "复制"}</button><button><ThumbUpAltOutlined />有帮助</button><button onClick={() => saveResearchRecord(message)}><BookmarkAddOutlined />{researchRecords.some((record) => record.conversationId === activeConversationId && record.messageId === message.id) ? "已保存" : "保存到研究记录"}</button></footer></>}
              </div>
            </article>)}
            {thinking && <div className="research-thinking"><i /><i /><i />{thinkingText}</div>}
          </section>
          <div className="research-compose-wrap">
            
            <form className="research-compose" onSubmit={submit}><textarea rows={2} value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={keyDown} placeholder={messages.length ? "继续追问，或给 Research Agent 一个任务…" : "输入研究问题，或给 Research Agent 一个任务…"} /><div><button type="button"><AddRounded /></button><button type="button" className="library-pill" onClick={onOpenDomainTree}><FolderOpenRounded />{activeProject?.name || "当前项目"} <span>{activeProject?.paperCount ?? 0} 篇</span></button><button className="research-send" disabled={!input.trim() || thinking}><SendRounded /></button></div></form>
            <small className="research-note">{isDirectAnswer ? "本次为普通对话，未调用研究 Agent 或知识库。" : "研究内容由 AI 基于所选知识库生成，请核验关键结论与原始文献。"}</small>
          </div>
        </div>}
      </main>

      {workspaceView === "chat" ? <aside className="research-context">
        <header><div><small>{isDirectAnswer ? "本次对话" : "本次研究"}</small><strong>{isDirectAnswer ? "回答方式" : "上下文与来源"}</strong></div><MoreHorizRounded /></header>
        {isDirectAnswer ? (
          <section className="research-progress-card"><div><span><AutoAwesomeRounded /></span><p><strong>直接回答</strong><small>无需调用研究 Agent 或检索论文</small></p><CheckRounded /></div></section>
        ) : <>
          <section className="research-progress-card"><div><span><SearchRounded /></span><p><strong>深度研究</strong><small>已分析 {activeSources.length} 个相关来源</small></p><CheckRounded /></div><progress value="100" max="100" /><ul><li><CheckRounded />理解问题与规划</li><li><CheckRounded />检索知识库</li><li><CheckRounded />交叉验证证据</li><li><CheckRounded />生成综合结论</li></ul></section>
          <div className="research-section-title"><strong>引用来源</strong><button onClick={onOpenBrowse}>查看全部</button></div>
          <div className="research-sources">
            {activeSources.length ? activeSources.map((source) => (
              <button key={`context-source-${source.index}`}>
                <span><ArticleOutlined /></span>
                <p><strong>{source.title}</strong><small>{[source.source, source.year].filter(Boolean).join(" · ") || "本地知识库"}</small></p>
                <em>{source.index}</em>
              </button>
            )) : <div className="research-source-empty">发送研究问题后，此处将显示回答引用的真实来源。</div>}
          </div>
          <div className="research-section-title"><strong>知识库范围</strong><button onClick={onOpenBrowse}>管理</button></div>
          <div className="research-library"><span><FolderOpenRounded /></span><p><strong>{activeProject?.name || "当前知识空间"}</strong><small>{activeProject?.paperCount ?? 0} 篇文献 · 当前对话范围</small></p><CheckRounded /></div>
          <button className="research-add-source" onClick={onOpenBrowse}><AddRounded />添加知识来源</button>
        </>}
      </aside> : null}
    </div>
  );
}
