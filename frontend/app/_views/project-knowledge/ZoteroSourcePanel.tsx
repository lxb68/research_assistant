"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import AddRounded from "@mui/icons-material/AddRounded";
import AutoStoriesRounded from "@mui/icons-material/AutoStoriesRounded";
import CheckCircleRounded from "@mui/icons-material/CheckCircleRounded";
import CloseRounded from "@mui/icons-material/CloseRounded";
import DeleteOutlineRounded from "@mui/icons-material/DeleteOutlineRounded";
import ErrorOutlineRounded from "@mui/icons-material/ErrorOutlineRounded";
import FolderRounded from "@mui/icons-material/FolderRounded";
import LinkRounded from "@mui/icons-material/LinkRounded";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import SearchRounded from "@mui/icons-material/SearchRounded";
import SyncRounded from "@mui/icons-material/SyncRounded";
import { useBackgroundTasks } from "@/app/_components/BackgroundTaskProvider";
import { useProjects } from "@/app/_components/ProjectProvider";
import { buildApiUrl } from "@/lib/api";
import { waitForJob } from "@/lib/background-jobs";

type ZoteroCollection = {
  key: string;
  name: string;
  parentCollection: string;
  version: number;
};

type CollectionRow = ZoteroCollection & {
  depth: number;
  path: string;
};

type ZoteroSource = {
  id: string;
  projectId: string;
  apiBaseUrl: string;
  libraryType: string;
  libraryId: string;
  collectionKeys: string[];
  includeSubcollections: boolean;
  status: string;
  lastError: string;
  lastSyncedAt: string;
};

type ScopeMode = "library" | "collections";

const STATUS_LABELS: Record<string, string> = {
  idle: "等待首次同步",
  syncing: "正在同步",
  ready: "同步正常",
  failed: "同步失败",
};

async function responsePayload(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : fallback);
  return payload;
}

function buildCollectionRows(collections: ZoteroCollection[]): CollectionRow[] {
  const byParent = new Map<string, ZoteroCollection[]>();
  const knownKeys = new Set(collections.map((collection) => collection.key));
  for (const collection of collections) {
    const parent = knownKeys.has(collection.parentCollection) ? collection.parentCollection : "";
    byParent.set(parent, [...(byParent.get(parent) ?? []), collection]);
  }
  for (const values of byParent.values()) {
    values.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
  }

  const result: CollectionRow[] = [];
  const visit = (parent: string, depth: number, parentPath: string) => {
    for (const collection of byParent.get(parent) ?? []) {
      const path = parentPath ? `${parentPath} / ${collection.name}` : collection.name;
      result.push({ ...collection, depth, path });
      visit(collection.key, depth + 1, path);
    }
  };
  visit("", 0, "");
  return result;
}

function formatSyncTime(value: string) {
  if (!value) return "尚未同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function statusClass(status: string) {
  if (status === "ready") return "is-ready";
  if (status === "failed") return "is-failed";
  if (status === "syncing") return "is-syncing";
  return "is-idle";
}

function StatusIcon({ status }: { status: string }) {
  if (status === "syncing") return <SyncRounded className="zotero-spin" />;
  if (status === "failed") return <ErrorOutlineRounded />;
  return <CheckCircleRounded />;
}

export function ZoteroSourcePanel({ projectId, disabled = false }: { projectId: string; disabled?: boolean }) {
  const { submitJob, openCenter } = useBackgroundTasks();
  const { refreshProjects, selectProject } = useProjects();
  const activeProjectIdRef = useRef(projectId);
  const [apiBaseUrl, setApiBaseUrl] = useState("http://127.0.0.1:23119/api");
  const [collections, setCollections] = useState<ZoteroCollection[]>([]);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [sources, setSources] = useState<ZoteroSource[]>([]);
  const [scopeMode, setScopeMode] = useState<ScopeMode>("library");
  const [collectionQuery, setCollectionQuery] = useState("");
  const [includeSubcollections, setIncludeSubcollections] = useState(true);
  const [isSetupOpen, setIsSetupOpen] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [syncingSourceIds, setSyncingSourceIds] = useState<string[]>([]);
  const [confirmRemoveId, setConfirmRemoveId] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const connectionPayload = useMemo(() => ({
    api_base_url: apiBaseUrl.trim(),
    library_type: "users",
    library_id: "0",
  }), [apiBaseUrl]);

  const collectionRows = useMemo(() => buildCollectionRows(collections), [collections]);
  const visibleCollections = useMemo(() => {
    const query = collectionQuery.trim().toLocaleLowerCase("zh-CN");
    return query
      ? collectionRows.filter((collection) => collection.path.toLocaleLowerCase("zh-CN").includes(query))
      : collectionRows;
  }, [collectionQuery, collectionRows]);

  useEffect(() => {
    activeProjectIdRef.current = projectId;
  }, [projectId]);

  const loadSources = useCallback(async (targetProjectId = projectId) => {
    const response = await fetch(buildApiUrl(`/api/zotero/sources?projectId=${encodeURIComponent(targetProjectId)}`), {
      cache: "no-store",
    });
    const payload = await responsePayload(response, "读取 Zotero 数据源失败");
    const values = (payload.sources ?? []) as ZoteroSource[];
    if (activeProjectIdRef.current === targetProjectId) {
      setSources(values);
      setIsSetupOpen((current) => current || values.length === 0);
    }
    return values;
  }, [projectId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setCollections([]);
      setSelectedKeys([]);
      setScopeMode("library");
      setCollectionQuery("");
      setIsConnected(false);
      setMessage("");
      setError("");
      setConfirmRemoveId("");
      void loadSources().catch((reason) => {
        setError(reason instanceof Error ? reason.message : "读取 Zotero 数据源失败");
      });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadSources]);

  const hasServerSyncingSource = sources.some((source) => source.status === "syncing");
  useEffect(() => {
    if (!hasServerSyncingSource) return;
    const interval = window.setInterval(() => {
      void loadSources(projectId).catch((reason) => {
        setError(reason instanceof Error ? reason.message : "刷新 Zotero 同步状态失败");
      });
    }, 1500);
    return () => window.clearInterval(interval);
  }, [hasServerSyncingSource, loadSources, projectId]);

  async function connect() {
    setBusy("connect");
    setError("");
    setMessage("");
    try {
      const connection = await fetch(buildApiUrl("/api/zotero/connection"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(connectionPayload),
      });
      await responsePayload(connection, "连接 Zotero 失败");

      const response = await fetch(buildApiUrl("/api/zotero/collections"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(connectionPayload),
      });
      const payload = await responsePayload(response, "读取 Zotero 分类失败");
      const values = (payload.collections ?? []) as ZoteroCollection[];
      setCollections(values);
      setSelectedKeys([]);
      setScopeMode("library");
      setCollectionQuery("");
      setIsConnected(true);
      setMessage(`连接成功，已读取 ${values.length} 个 Zotero 分类。`);
    } catch (reason) {
      setIsConnected(false);
      setError(reason instanceof Error ? reason.message : "连接 Zotero 失败");
    } finally {
      setBusy("");
    }
  }

  async function trackSync(source: ZoteroSource, jobId: string) {
    const sourceId = source.id;
    setSyncingSourceIds((current) => [...new Set([...current, sourceId])]);
    try {
      const job = await waitForJob(jobId);
      const resultProjectId = typeof job.result?.projectId === "string"
        ? job.result.projectId
        : source.projectId;
      await refreshProjects();
      if (resultProjectId !== source.projectId) {
        selectProject(resultProjectId);
      } else {
        await loadSources(source.projectId);
      }
      if (job.status === "completed") {
        const indexed = Number(job.result?.indexed ?? 0);
        const unchanged = Number(job.result?.unchanged ?? 0);
        const unavailable = Number(job.result?.unavailable ?? 0);
        const unavailableText = unavailable ? `，${unavailable} 篇附件未下载` : "";
        setMessage(`同步完成：新增或重建 ${indexed} 篇，跳过未变化 ${unchanged} 篇${unavailableText}。`);
      } else {
        setError(job.error || "Zotero 同步未完成，请在后台任务中心查看详情。");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取 Zotero 同步状态失败");
    } finally {
      setSyncingSourceIds((current) => current.filter((id) => id !== sourceId));
    }
  }

  async function submitSync(source: ZoteroSource, announce = true) {
    const job = await submitJob("zotero_sync", { sourceId: source.id }, {
      dedupeKey: `zotero-sync:${source.id}`,
    });
    if (announce) setMessage("同步任务已提交，页面会在完成后自动刷新。");
    void trackSync(source, job.jobId);
  }

  async function createSource() {
    if (scopeMode === "collections" && selectedKeys.length === 0) return;
    setBusy("create");
    setError("");
    try {
      const response = await fetch(buildApiUrl(`/api/projects/${encodeURIComponent(projectId)}/zotero-sources`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...connectionPayload,
          collection_keys: scopeMode === "library" ? [] : selectedKeys,
          include_subcollections: includeSubcollections,
          include_standalone_attachments: false,
          create_collection_projects: true,
        }),
      });
      const payload = await responsePayload(response, "创建 Zotero 数据源失败");
      const createdSources = ((payload.sources ?? [payload.source]) as ZoteroSource[]).filter(Boolean);
      if (!createdSources.length) throw new Error("Zotero 数据源创建成功，但没有返回目标项目");
      await refreshProjects();
      selectProject(createdSources[0].projectId);
      setIsSetupOpen(false);
      for (const source of createdSources) {
        await submitSync(source, false);
      }
      setMessage(
        createdSources.length === 1
          ? "已创建或复用 Zotero 同名项目，并开始同步；文献不会进入默认研究项目。"
          : `已为 ${createdSources.length} 个 Zotero 分类创建或复用同名项目，并开始同步。`,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建 Zotero 数据源失败");
    } finally {
      setBusy("");
    }
  }

  async function syncSource(source: ZoteroSource) {
    setBusy(source.id);
    setError("");
    try {
      await submitSync(source);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交 Zotero 同步失败");
    } finally {
      setBusy("");
    }
  }

  async function removeSource(source: ZoteroSource) {
    setBusy(source.id);
    setError("");
    try {
      const response = await fetch(buildApiUrl(`/api/zotero/sources/${encodeURIComponent(source.id)}`), {
        method: "DELETE",
      });
      await responsePayload(response, "移除 Zotero 数据源失败");
      await loadSources();
      setConfirmRemoveId("");
      setMessage("已移除数据源配置；已导入论文和 Zotero 原文件均未删除。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "移除 Zotero 数据源失败");
    } finally {
      setBusy("");
    }
  }

  function toggleCollection(key: string, checked: boolean) {
    setSelectedKeys((current) => checked
      ? [...new Set([...current, key])]
      : current.filter((value) => value !== key));
  }

  const activeSyncCount = syncingSourceIds.length + sources.filter(
    (source) => source.status === "syncing" && !syncingSourceIds.includes(source.id),
  ).length;

  return (
    <section className="zotero-source-panel" aria-label="Zotero 数据源">
      <header className="zotero-panel-header">
        <div className="zotero-brand-mark" aria-hidden="true"><span>Z</span></div>
        <div className="zotero-panel-title">
          <div>
            <h2>Zotero 文献库</h2>
            <span className="zotero-local-badge"><LinkRounded />本机连接</span>
          </div>
          <p>同步题录和 PDF 全文到当前项目，原文件继续由 Zotero 管理。</p>
        </div>
        <div className="zotero-panel-summary">
          <span><strong>{sources.length}</strong> 个数据源</span>
          <span><strong>{activeSyncCount}</strong> 个同步中</span>
        </div>
        <div className="zotero-header-actions">
          <button className="zotero-icon-button" type="button" onClick={() => void loadSources()} disabled={disabled || Boolean(busy)} title="刷新数据源状态" aria-label="刷新数据源状态">
            <RefreshRounded />
          </button>
          <button className="zotero-primary-button" type="button" onClick={() => setIsSetupOpen((current) => !current)} disabled={disabled || Boolean(busy)}>
            {isSetupOpen ? <><CloseRounded />收起</> : <><AddRounded />添加数据源</>}
          </button>
        </div>
      </header>

      {sources.length ? (
        <div className="zotero-source-list">
          {sources.map((source) => {
            const isSyncing = syncingSourceIds.includes(source.id) || source.status === "syncing";
            const effectiveStatus = isSyncing ? "syncing" : source.status;
            const removePending = confirmRemoveId === source.id;
            return (
              <article className={statusClass(effectiveStatus)} key={source.id}>
                <div className="zotero-source-icon" aria-hidden="true"><AutoStoriesRounded /></div>
                <div className="zotero-source-copy">
                  <div>
                    <strong>{source.collectionKeys.length ? `${source.collectionKeys.length} 个指定分类` : "整个个人文库"}</strong>
                    <span className={`zotero-status-chip ${statusClass(effectiveStatus)}`}>
                      <StatusIcon status={effectiveStatus} />
                      {STATUS_LABELS[effectiveStatus] ?? effectiveStatus}
                    </span>
                  </div>
                  <p>{source.includeSubcollections && source.collectionKeys.length ? "包含子分类 · " : ""}最近同步：{formatSyncTime(source.lastSyncedAt)}</p>
                  {source.lastError ? <small>{source.lastError}</small> : null}
                </div>
                <div className="zotero-source-actions">
                  {removePending ? (
                    <div className="zotero-remove-confirm" role="alert">
                      <span>仅移除配置，不删除论文</span>
                      <button type="button" onClick={() => void removeSource(source)} disabled={Boolean(busy)}>确认移除</button>
                      <button type="button" onClick={() => setConfirmRemoveId("")}>取消</button>
                    </div>
                  ) : (
                    <>
                      <button className="zotero-sync-button" type="button" onClick={() => void syncSource(source)} disabled={disabled || Boolean(busy) || isSyncing}>
                        <SyncRounded className={isSyncing ? "zotero-spin" : ""} />{isSyncing ? "同步中…" : "立即同步"}
                      </button>
                      <button className="zotero-icon-button is-danger" type="button" onClick={() => setConfirmRemoveId(source.id)} disabled={disabled || Boolean(busy) || isSyncing} title="移除数据源配置" aria-label="移除数据源配置">
                        <DeleteOutlineRounded />
                      </button>
                    </>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      ) : !isSetupOpen ? (
        <div className="zotero-empty-state">
          <AutoStoriesRounded />
          <strong>还没有连接 Zotero</strong>
          <p>添加数据源后，分类中的论文会自动进入当前项目，并可用于切片、检索和问答。</p>
          <button type="button" onClick={() => setIsSetupOpen(true)}><AddRounded />开始连接</button>
        </div>
      ) : null}

      {isSetupOpen ? (
        <section className="zotero-setup" aria-label="添加 Zotero 数据源">
          <div className="zotero-setup-heading">
            <div>
              <h3>连接本机 Zotero</h3>
              <p>请先在 Zotero 高级设置中开启“允许其他应用通信”。</p>
            </div>
            <button className="zotero-icon-button" type="button" onClick={() => setIsSetupOpen(false)} aria-label="关闭配置"><CloseRounded /></button>
          </div>

          <div className="zotero-step-title">
            <span>第 1 步</span>
            <div><strong>检测 Local API</strong><small>默认地址适用于当前电脑上的 Zotero 7。</small></div>
          </div>
          <div className="zotero-connection-row">
            <label>
              Local API 地址
              <span><LinkRounded /><input value={apiBaseUrl} onChange={(event) => { setApiBaseUrl(event.target.value); setIsConnected(false); }} disabled={disabled || Boolean(busy)} /></span>
            </label>
            <button className="zotero-secondary-button" type="button" onClick={() => void connect()} disabled={disabled || Boolean(busy) || !apiBaseUrl.trim()}>
              {busy === "connect" ? <><SyncRounded className="zotero-spin" />正在连接…</> : <><LinkRounded />{isConnected ? "重新读取" : "测试连接"}</>}
            </button>
          </div>

          {isConnected ? (
            <div className="zotero-scope-step">
              <div className="zotero-step-title">
                <span>第 2 步</span>
                <div><strong>选择同步范围</strong><small>同步整个个人文库，或只同步指定分类。</small></div>
              </div>
              <div className="zotero-scope-tabs" role="radiogroup" aria-label="同步范围">
                <label>
                  <input type="radio" name="zotero-scope" checked={scopeMode === "library"} onChange={() => setScopeMode("library")} />
                  <span><strong>整个个人文库</strong><small>自动包含未来新增的全部题录</small></span>
                </label>
                <label>
                  <input type="radio" name="zotero-scope" checked={scopeMode === "collections"} onChange={() => setScopeMode("collections")} />
                  <span><strong>指定分类</strong><small>精确控制当前项目的文献边界</small></span>
                </label>
              </div>

              {scopeMode === "collections" ? (
                <div className="zotero-collection-picker">
                  <div className="zotero-collection-toolbar">
                    <label><SearchRounded /><input value={collectionQuery} onChange={(event) => setCollectionQuery(event.target.value)} placeholder="搜索分类或完整路径" /></label>
                    <button type="button" onClick={() => setSelectedKeys(collectionRows.map((item) => item.key))}>全选（{collectionRows.length}）</button>
                    <button type="button" onClick={() => setSelectedKeys([])} disabled={!selectedKeys.length}>清空（{selectedKeys.length}）</button>
                  </div>
                  <div className="zotero-collection-list">
                    {visibleCollections.map((collection) => (
                      <label className="zotero-collection-item" key={collection.key} style={{ paddingLeft: 8 + collection.depth * 18 }} title={collection.path}>
                        <input type="checkbox" checked={selectedKeys.includes(collection.key)} onChange={(event) => toggleCollection(collection.key, event.target.checked)} />
                        <FolderRounded />
                        <span>{collection.name}</span>
                      </label>
                    ))}
                    {!visibleCollections.length ? <div className="zotero-collection-empty">没有匹配的分类</div> : null}
                  </div>
                  <label className="zotero-option">
                    <input type="checkbox" checked={includeSubcollections} onChange={(event) => setIncludeSubcollections(event.target.checked)} />
                    <span><strong>包含子分类</strong><small>以后加入子分类的论文也会自动同步</small></span>
                  </label>
                </div>
              ) : null}

              <footer className="zotero-setup-footer">
                <p>{scopeMode === "library" ? "将创建“Zotero 个人文库”项目" : `将按 ${selectedKeys.length} 个分类创建同名项目`}；文献不会进入默认研究项目。</p>
                <button className="zotero-primary-button" type="button" onClick={() => void createSource()} disabled={disabled || Boolean(busy) || (scopeMode === "collections" && selectedKeys.length === 0)}>
                  {busy === "create" ? <><SyncRounded className="zotero-spin" />正在创建…</> : <><AddRounded />添加并开始同步</>}
                </button>
              </footer>
            </div>
          ) : null}
        </section>
      ) : null}

      {message ? <div className="zotero-feedback is-success"><CheckCircleRounded />{message}<button type="button" onClick={() => setMessage("")} aria-label="关闭提示"><CloseRounded /></button></div> : null}
      {error ? <div className="zotero-feedback is-error"><ErrorOutlineRounded />{error}<button type="button" onClick={() => setError("")} aria-label="关闭错误"><CloseRounded /></button></div> : null}
      {syncingSourceIds.length ? <button className="zotero-task-link" type="button" onClick={openCenter}>查看后台同步详情</button> : null}
    </section>
  );
}
