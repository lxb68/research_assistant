/* 管理研究会话、流式代理响应、本地历史记录和工作区快捷入口。 */

"use client";

import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import AddRounded from "@mui/icons-material/AddRounded";
import ArticleOutlined from "@mui/icons-material/ArticleOutlined";
import AutoAwesomeRounded from "@mui/icons-material/AutoAwesomeRounded";
import BoltRounded from "@mui/icons-material/BoltRounded";
import BookmarkAddOutlined from "@mui/icons-material/BookmarkAddOutlined";
import CheckRounded from "@mui/icons-material/CheckRounded";
import ContentCopyRounded from "@mui/icons-material/ContentCopyRounded";
import DataObjectRounded from "@mui/icons-material/DataObjectRounded";
import DeleteOutlineRounded from "@mui/icons-material/DeleteOutlineRounded";
import DownloadRounded from "@mui/icons-material/DownloadRounded";
import FolderOpenRounded from "@mui/icons-material/FolderOpenRounded";
import HistoryRounded from "@mui/icons-material/HistoryRounded";
import MenuRounded from "@mui/icons-material/MenuRounded";
import MoreHorizRounded from "@mui/icons-material/MoreHorizRounded";
import SearchRounded from "@mui/icons-material/SearchRounded";
import SendRounded from "@mui/icons-material/SendRounded";
import SettingsOutlined from "@mui/icons-material/SettingsOutlined";
import ThumbUpAltOutlined from "@mui/icons-material/ThumbUpAltOutlined";
import { buildApiUrl } from "@/lib/api";
import { useBackgroundTasks } from "@/app/_components/BackgroundTaskProvider";
import { fetchJob } from "@/lib/background-jobs";
import MessageContent from "@/app/_components/MessageContent";

type Props = { onOpenDownload: () => void; onOpenBrowse: () => void; onOpenDomainTree: () => void; onOpenSettings: () => void };
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

type Conversation = { id: string; title: string; date: string; messages: Message[] };
type ResearchRecord = {
  id: string;
  conversationId: string;
  messageId: number;
  title: string;
  question: string;
  content: string;
  sources: Source[];
  createdAt: string;
};

const CONVERSATIONS_KEY = "research-agent.conversations";
// 三组键分别保存会话列表、当前会话和归档研究记录。
const ACTIVE_CONVERSATION_KEY = "research-agent.active-conversation";
const RESEARCH_RECORDS_KEY = "research-agent.research-records";

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
export default function ResearchChat({ onOpenDownload, onOpenBrowse, onOpenDomainTree, onOpenSettings }: Props) {
  const { jobs, submitJob } = useBackgroundTasks();
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
            ? { ...conversation, date: "刚刚", messages: [...conversation.messages, userMessage] }
            : conversation,
        );
      }
      return [{ id: conversationId, title, date: "刚刚", messages: [userMessage] }, ...items];
    });
    if (!activeConversationId) {
      setActiveConversationId(conversationId);
      setConversationTitle(title);
    }
    setInput("");
    setThinking(true);
    setThinkingText("正在判断如何处理你的问题…");
    try {
      const job = await submitJob("research_chat", {
        question: prompt,
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
      setMessages((items) => items.some((item) => item.id === agentMessage.id) ? items : [...items, agentMessage]);
      setConversations((items) => items.map((conversation) =>
        conversation.id === conversationId && !conversation.messages.some((message) => message.id === agentMessage.id)
          ? { ...conversation, messages: [...conversation.messages, agentMessage] }
          : conversation,
      ));
    } catch (error) {
      const agentMessage: Message = { id: id + 1, role: "agent", content: error instanceof Error ? `请求失败：${error.message}` : "研究对话请求失败，请稍后重试。" };
      setMessages((items) => [...items, agentMessage]);
      setConversations((items) => items.map((conversation) =>
        conversation.id === conversationId
          ? { ...conversation, messages: [...conversation.messages, agentMessage] }
          : conversation,
      ));
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

  /** 删除指定会话并选择新的活动会话。 */
  function deleteConversation(id: string) {
    if (thinking) return;
    const remaining = conversations.filter((conversation) => conversation.id !== id);
    setConversations(remaining);
    if (activeConversationId !== id) return;
    const nextConversation = remaining[0];
    if (nextConversation) {
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
        <div className="research-brand"><span><AutoAwesomeRounded /></span><div><strong>Research Agent</strong><small>知识驱动的研究伙伴</small></div><button onClick={() => setSidebar(false)}><MenuRounded /></button></div>
        <button className="research-new" onClick={startNewConversation}><AddRounded />新建研究对话</button>
        <label>工作区</label>
        <nav className="research-nav">
          <button className={workspaceView === "chat" ? "active" : ""} onClick={() => setWorkspaceView("chat")}><AutoAwesomeRounded />研究对话{conversations.length > 0 ? <em>{conversations.length}</em> : null}</button>
          <button onClick={onOpenDownload}><DownloadRounded />下载数据集</button>
          <button onClick={onOpenBrowse}><FolderOpenRounded />浏览数据集</button>
          <button onClick={onOpenDomainTree}><DataObjectRounded />领域图谱</button>
          <button className={workspaceView === "records" ? "active" : ""} onClick={() => setWorkspaceView("records")}><HistoryRounded />研究记录{researchRecords.length > 0 ? <em>{researchRecords.length}</em> : null}</button>
        </nav>
        <label>最近对话</label>
        <div className="research-recents">
          {conversations.map((conversation) => (
            <div className={`research-recent-item ${activeConversationId === conversation.id ? "active" : ""}`} key={conversation.id}>
              <button className="research-recent-open" onClick={() => openConversation(conversation.id)}>
                <span>{conversation.title}</span><small>{conversation.date}</small>
              </button>
              <button className="research-recent-delete" onClick={() => deleteConversation(conversation.id)} aria-label={`删除对话：${conversation.title}`} title="删除对话">
                <DeleteOutlineRounded />
              </button>
            </div>
          ))}
          {conversations.length === 0 ? <div className="research-recent-empty">暂无最近对话</div> : null}
        </div>
        <div className="research-side-bottom"><button onClick={onOpenSettings}><SettingsOutlined />设置</button><div><span>LX</span><p><strong>研究工作区</strong><small>个人专业版</small></p><MoreHorizRounded /></div></div>
      </aside>

      <main className="research-main">
        <header className="research-topbar">
          {!sidebar && <button className="research-icon" onClick={() => setSidebar(true)}><MenuRounded /></button>}
          <div><h1>{workspaceView === "records" ? "成果档案" : conversationTitle}</h1><span><i />{workspaceView === "records" ? `${researchRecords.length} 条已保存成果` : "已自动保存"}</span></div>
          {workspaceView === "chat" ? <><button className="research-export"><DownloadRounded />导出报告</button><button className="research-icon"><MoreHorizRounded /></button></> : null}
        </header>
        {workspaceView === "records" ? (
          <section className="research-records-page">
            <header><div><small>Research Archive</small><h2>研究记录</h2><p>沉淀值得长期保留的研究结论、原始问题与引用证据，形成可回溯的成果档案。</p></div><button onClick={() => setWorkspaceView("chat")}><AutoAwesomeRounded />返回研究对话</button></header>
            {researchRecords.length > 0 ? (
              <div className="research-record-grid">
                {researchRecords.map((record) => (
                  <article className="research-record-card" key={record.id}>
                    <header><div><small>{record.createdAt}</small><h3>{record.title}</h3></div><button onClick={() => deleteResearchRecord(record.id)} aria-label={`删除研究记录：${record.title}`} title="删除研究记录"><DeleteOutlineRounded /></button></header>
                    <strong>{record.question}</strong>
                    <p>{record.content}</p>
                    <footer><span>{record.sources.length} 个引用来源</span><button onClick={() => openRecordConversation(record)} disabled={!record.conversationId || !conversations.some((conversation) => conversation.id === record.conversationId)}>打开原对话</button></footer>
                  </article>
                ))}
              </div>
            ) : (
              <div className="research-record-empty"><HistoryRounded /><h3>暂无研究记录</h3><p>在研究回答下方点击“保存到研究记录”，成果会出现在这里。</p><button onClick={() => setWorkspaceView("chat")}>返回研究对话</button></div>
            )}
          </section>
        ) : <div className="research-body">
          <section className="research-thread">
            {!messages.length && <div className="research-empty"><span><AutoAwesomeRounded /></span><h2>今天想研究什么？</h2><p>我会从你的知识库中检索、分析并标注每一处引用。</p></div>}
            {messages.map((message) => <article className={`research-message ${message.role}`} key={message.id}>
              <div className="research-avatar">{message.role === "agent" ? <AutoAwesomeRounded /> : "LX"}</div>
              <div><header><strong>{message.role === "agent" ? "Research Agent" : "你"}</strong><span>{message.id <= 2 ? "10:24" : "刚刚"}</span></header><MessageContent content={message.content} />
                {message.role === "agent" && <>{message.sources?.length ? <div className="research-citations">{message.sources.map((source) => <button key={`${message.id}-${source.index}`}>{source.index} · {source.title}</button>)}</div> : null}<footer><button onClick={() => { navigator.clipboard?.writeText(message.content); setCopied(message.id); }}><ContentCopyRounded />{copied === message.id ? "已复制" : "复制"}</button><button><ThumbUpAltOutlined />有帮助</button><button onClick={() => saveResearchRecord(message)}><BookmarkAddOutlined />{researchRecords.some((record) => record.conversationId === activeConversationId && record.messageId === message.id) ? "已保存" : "保存到研究记录"}</button></footer></>}
              </div>
            </article>)}
            {thinking && <div className="research-thinking"><i /><i /><i />{thinkingText}</div>}
          </section>
          <div className="research-compose-wrap">
            <div className="research-prompts">{["生成一份文献综述大纲", "对比 RAG 与微调路线", "找出近两年的研究趋势"].map((text) => <button onClick={() => send(text)} key={text}><BoltRounded />{text}</button>)}</div>
            <form className="research-compose" onSubmit={submit}><textarea rows={2} value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={keyDown} placeholder="继续追问，或给 Research Agent 一个任务…" /><div><button type="button"><AddRounded /></button><button type="button" className="library-pill"><FolderOpenRounded />医疗大模型研究 <span>42 篇</span></button><button className="research-send" disabled={!input.trim() || thinking}><SendRounded /></button></div></form>
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
          <div className="research-library"><span><FolderOpenRounded /></span><p><strong>医疗大模型研究</strong><small>42 篇文献 · 更新于今天</small></p><CheckRounded /></div>
          <button className="research-add-source" onClick={onOpenBrowse}><AddRounded />添加知识来源</button>
        </>}
      </aside> : null}
    </div>
  );
}
