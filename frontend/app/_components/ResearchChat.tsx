"use client";

import { FormEvent, KeyboardEvent, useRef, useState } from "react";
import AddRounded from "@mui/icons-material/AddRounded";
import ArticleOutlined from "@mui/icons-material/ArticleOutlined";
import AutoAwesomeRounded from "@mui/icons-material/AutoAwesomeRounded";
import BoltRounded from "@mui/icons-material/BoltRounded";
import CheckRounded from "@mui/icons-material/CheckRounded";
import ContentCopyRounded from "@mui/icons-material/ContentCopyRounded";
import DataObjectRounded from "@mui/icons-material/DataObjectRounded";
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
import { readNdjsonStream } from "@/lib/stream";

type Props = { onOpenDownload: () => void; onOpenBrowse: () => void; onOpenDomainTree: () => void; onOpenSettings: () => void };
type Source = { index: number; title: string; year?: string; source?: string };
type Message = { id: number; role: "user" | "agent"; content: string; sources?: Source[] };
type StreamEvent = { type: "log"; message: string } | { type: "result"; result: OrchestratorResult } | { type: "error"; message: string } | { type: "done" };
type OrchestratorResult = { action: string; result: { answer?: string; sources?: Source[]; status?: string; message?: string; requiredMaterials?: Array<{ description: string }> } };

const seed: Message[] = [
  { id: 1, role: "user", content: "帮我梳理一下大语言模型在医疗问答领域的主要研究方向，并指出目前最值得关注的空白。" },
  { id: 2, role: "agent", content: "基于当前知识库中的 42 篇论文，我将研究脉络归纳为四条主线：临床知识评测、医疗对话安全、检索增强生成，以及面向真实工作流的辅助决策。现阶段最值得关注的空白，是如何在保持可追溯性的同时完成跨机构、跨人群的稳定泛化。" },
];

export default function ResearchChat({ onOpenDownload, onOpenBrowse, onOpenDomainTree, onOpenSettings }: Props) {
  const [messages, setMessages] = useState(seed);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [thinkingText, setThinkingText] = useState("正在检索知识库并组织证据…");
  const [sidebar, setSidebar] = useState(true);
  const [copied, setCopied] = useState<number | null>(null);
  const nextMessageId = useRef(3);

  async function send(value = input) {
    const prompt = value.trim();
    if (!prompt || thinking) return;
    const id = nextMessageId.current;
    nextMessageId.current += 2;
    setMessages((items) => [...items, { id, role: "user", content: prompt }]);
    setInput("");
    setThinking(true);
    setThinkingText("正在判断知识库证据是否充分…");
    try {
      const response = await fetch(buildApiUrl("/api/research/chat/stream"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: prompt,
          history: messages.slice(-8).map((message) => ({
            role: message.role === "agent" ? "assistant" : "user",
            content: message.content,
          })),
        }),
      });
      if (!response.ok || !response.body) throw new Error("研究对话服务暂时不可用");
      await readNdjsonStream<StreamEvent>(response.body, (event) => {
        if (event.type === "log") setThinkingText(event.message);
        if (event.type === "error") throw new Error(event.message);
        if (event.type === "result") {
          const payload = event.result.result;
          const materialText = payload.requiredMaterials?.map((item, index) => `${index + 1}. ${item.description}`).join("\n");
          const needsUserHelp = payload.status === "needs_materials" || payload.status === "needs_user_action";
          const content = needsUserHelp
            ? `${payload.message || "当前流程需要你的协助。"}${materialText ? `\n\n建议补充：\n${materialText}` : ""}`
            : payload.answer || "研究任务已完成，但没有返回可展示的回答。";
          setMessages((items) => [...items, { id: id + 1, role: "agent", content, sources: payload.sources }]);
        }
      });
    } catch (error) {
      setMessages((items) => [...items, { id: id + 1, role: "agent", content: error instanceof Error ? `请求失败：${error.message}` : "研究对话请求失败，请稍后重试。" }]);
    } finally {
      setThinking(false);
    }
  }

  function submit(event: FormEvent) { event.preventDefault(); send(); }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } }

  return (
    <div className={`research-chat ${sidebar ? "" : "is-collapsed"}`}>
      <aside className="research-sidebar">
        <div className="research-brand"><span><AutoAwesomeRounded /></span><div><strong>Research Agent</strong><small>知识驱动的研究伙伴</small></div><button onClick={() => setSidebar(false)}><MenuRounded /></button></div>
        <button className="research-new" onClick={() => setMessages([])}><AddRounded />新建研究对话</button>
        <label>工作区</label>
        <nav className="research-nav">
          <button className="active"><AutoAwesomeRounded />研究对话<em>4</em></button>
          <button onClick={onOpenDownload}><DownloadRounded />下载数据集</button>
          <button onClick={onOpenBrowse}><FolderOpenRounded />浏览数据集</button>
          <button onClick={onOpenDomainTree}><DataObjectRounded />领域图谱</button>
          <button><HistoryRounded />研究记录</button>
        </nav>
        <label>最近对话</label>
        <div className="research-recents"><button className="active"><span>医疗大模型研究综述</span><small>刚刚</small></button><button><span>RAG 评测框架调研</span><small>昨天</small></button><button><span>多智能体协作论文整理</span><small>7月8日</small></button></div>
        <div className="research-side-bottom"><button onClick={onOpenSettings}><SettingsOutlined />设置</button><div><span>LX</span><p><strong>研究工作区</strong><small>个人专业版</small></p><MoreHorizRounded /></div></div>
      </aside>

      <main className="research-main">
        <header className="research-topbar">
          {!sidebar && <button className="research-icon" onClick={() => setSidebar(true)}><MenuRounded /></button>}
          <div><h1>医疗大模型研究综述</h1><span><i />已自动保存</span></div>
          <button className="research-export"><DownloadRounded />导出报告</button><button className="research-icon"><MoreHorizRounded /></button>
        </header>
        <div className="research-body">
          <section className="research-thread">
            {!messages.length && <div className="research-empty"><span><AutoAwesomeRounded /></span><h2>今天想研究什么？</h2><p>我会从你的知识库中检索、分析并标注每一处引用。</p></div>}
            {messages.map((message) => <article className={`research-message ${message.role}`} key={message.id}>
              <div className="research-avatar">{message.role === "agent" ? <AutoAwesomeRounded /> : "LX"}</div>
              <div><header><strong>{message.role === "agent" ? "Research Agent" : "你"}</strong><span>{message.id <= 2 ? "10:24" : "刚刚"}</span></header><p>{message.content}</p>
                {message.role === "agent" && <>{message.sources?.length ? <div className="research-citations">{message.sources.map((source) => <button key={`${message.id}-${source.index}`}>{source.index} · {source.title}</button>)}</div> : null}<footer><button onClick={() => { navigator.clipboard?.writeText(message.content); setCopied(message.id); }}><ContentCopyRounded />{copied === message.id ? "已复制" : "复制"}</button><button><ThumbUpAltOutlined />有帮助</button></footer></>}
              </div>
            </article>)}
            {thinking && <div className="research-thinking"><i /><i /><i />{thinkingText}</div>}
          </section>
          <div className="research-compose-wrap">
            <div className="research-prompts">{["生成一份文献综述大纲", "对比 RAG 与微调路线", "找出近两年的研究趋势"].map((text) => <button onClick={() => send(text)} key={text}><BoltRounded />{text}</button>)}</div>
            <form className="research-compose" onSubmit={submit}><textarea rows={2} value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={keyDown} placeholder="继续追问，或给 Research Agent 一个任务…" /><div><button type="button"><AddRounded /></button><button type="button" className="library-pill"><FolderOpenRounded />医疗大模型研究 <span>42 篇</span></button><button className="research-send" disabled={!input.trim() || thinking}><SendRounded /></button></div></form>
            <small className="research-note">内容由 AI 基于所选知识库生成，请核验关键结论与原始文献。</small>
          </div>
        </div>
      </main>

      <aside className="research-context">
        <header><div><small>本次研究</small><strong>上下文与来源</strong></div><MoreHorizRounded /></header>
        <section className="research-progress-card"><div><span><SearchRounded /></span><p><strong>深度研究</strong><small>已分析 6 个相关来源</small></p><CheckRounded /></div><progress value="100" max="100" /><ul><li><CheckRounded />理解问题与规划</li><li><CheckRounded />检索知识库</li><li><CheckRounded />交叉验证证据</li><li><CheckRounded />生成综合结论</li></ul></section>
        <div className="research-section-title"><strong>引用来源</strong><button onClick={onOpenBrowse}>查看全部</button></div>
        <div className="research-sources"><button><span><ArticleOutlined /></span><p><strong>Large language models encode clinical knowledge</strong><small>Nature · 2023</small></p><em>1</em></button><button><span><ArticleOutlined /></span><p><strong>Retrieval-Augmented Generation for Medicine</strong><small>JAMIA · 2024</small></p><em>2</em></button><button><span><ArticleOutlined /></span><p><strong>Evaluating LLMs in Clinical QA</strong><small>arXiv · 2025</small></p><em>3</em></button></div>
        <div className="research-section-title"><strong>知识库范围</strong><button onClick={onOpenBrowse}>管理</button></div>
        <div className="research-library"><span><FolderOpenRounded /></span><p><strong>医疗大模型研究</strong><small>42 篇文献 · 更新于今天</small></p><CheckRounded /></div>
        <button className="research-add-source" onClick={onOpenBrowse}><AddRounded />添加知识来源</button>
      </aside>
    </div>
  );
}
