"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import AutoStoriesRounded from "@mui/icons-material/AutoStoriesRounded";
import FolderRounded from "@mui/icons-material/FolderRounded";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import SyncRounded from "@mui/icons-material/SyncRounded";
import { buildApiUrl } from "@/lib/api";
import type { SavedPaper } from "@/lib/papers";

type ZoteroCollectionTreeNode = {
  sourceId: string;
  key: string;
  parentKey: string;
  name: string;
  path: string;
  depth: number;
  isVirtual: boolean;
  directPaperCount: number;
  paperCount: number;
  children: ZoteroCollectionTreeNode[];
};

type ZoteroCollectionTree = {
  sourceId: string;
  roots: ZoteroCollectionTreeNode[];
};

async function responsePayload(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : fallback);
  return payload;
}

function CollectionTreeNode({
  node,
  selectedId,
  onSelect,
}: {
  node: ZoteroCollectionTreeNode;
  selectedId: string;
  onSelect: (node: ZoteroCollectionTreeNode) => void;
}) {
  const nodeId = `${node.sourceId}:${node.key}`;
  return (
    <li>
      <button
        className={selectedId === nodeId ? "is-selected" : ""}
        type="button"
        onClick={() => onSelect(node)}
        title={node.path}
      >
        <FolderRounded />
        <span>{node.name}</span>
        <small>{node.paperCount}</small>
      </button>
      {node.children.length ? (
        <ul>
          {node.children.map((child) => (
            <CollectionTreeNode
              key={child.key}
              node={child}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function ZoteroCollectionBrowser({
  projectId,
  refreshToken,
}: {
  projectId: string;
  refreshToken: number;
}) {
  const [trees, setTrees] = useState<ZoteroCollectionTree[]>([]);
  const [selectedNode, setSelectedNode] = useState<ZoteroCollectionTreeNode | null>(null);
  const [papers, setPapers] = useState<SavedPaper[]>([]);
  const [isLoadingTrees, setIsLoadingTrees] = useState(false);
  const [isLoadingPapers, setIsLoadingPapers] = useState(false);
  const [error, setError] = useState("");
  const [loadedProjectId, setLoadedProjectId] = useState("");
  const treeRequestRef = useRef<AbortController | null>(null);
  const paperRequestRef = useRef<AbortController | null>(null);

  const loadTrees = useCallback(async () => {
    treeRequestRef.current?.abort();
    paperRequestRef.current?.abort();
    paperRequestRef.current = null;
    const controller = new AbortController();
    treeRequestRef.current = controller;
    setIsLoadingTrees(true);
    setError("");
    try {
      const response = await fetch(buildApiUrl(
        `/api/projects/${encodeURIComponent(projectId)}/zotero-collections/tree`,
      ), { cache: "no-store", signal: controller.signal });
      const payload = await responsePayload(response, "读取 Zotero 分类结构失败");
      if (treeRequestRef.current !== controller) return;
      setTrees((payload.trees ?? []) as ZoteroCollectionTree[]);
      setLoadedProjectId(projectId);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      if (treeRequestRef.current !== controller) return;
      setTrees([]);
      setLoadedProjectId(projectId);
      setError(reason instanceof Error ? reason.message : "读取 Zotero 分类结构失败");
    } finally {
      if (treeRequestRef.current === controller) {
        treeRequestRef.current = null;
        setIsLoadingTrees(false);
      }
    }
  }, [projectId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSelectedNode(null);
      setPapers([]);
      void loadTrees();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadTrees, refreshToken]);

  useEffect(() => () => {
    treeRequestRef.current?.abort();
    paperRequestRef.current?.abort();
  }, []);

  async function selectNode(node: ZoteroCollectionTreeNode) {
    paperRequestRef.current?.abort();
    const controller = new AbortController();
    paperRequestRef.current = controller;
    setSelectedNode(node);
    setPapers([]);
    setIsLoadingPapers(true);
    setError("");
    try {
      const response = await fetch(buildApiUrl(
        `/api/projects/${encodeURIComponent(projectId)}/zotero-collections/`
        + `${encodeURIComponent(node.sourceId)}/${encodeURIComponent(node.key)}/papers`
        + "?includeDescendants=true",
      ), { cache: "no-store", signal: controller.signal });
      const payload = await responsePayload(response, "读取分类文献失败");
      if (paperRequestRef.current !== controller) return;
      setPapers((payload.papers ?? []) as SavedPaper[]);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      if (paperRequestRef.current !== controller) return;
      setPapers([]);
      setError(reason instanceof Error ? reason.message : "读取分类文献失败");
    } finally {
      if (paperRequestRef.current === controller) {
        paperRequestRef.current = null;
        setIsLoadingPapers(false);
      }
    }
  }

  const isCurrentProjectLoaded = loadedProjectId === projectId;
  const isTreePending = !isCurrentProjectLoaded || isLoadingTrees;
  const currentSelectedNode = isCurrentProjectLoaded ? selectedNode : null;
  const hasTrees = isCurrentProjectLoaded && trees.some((tree) => tree.roots.length);
  const selectedId = currentSelectedNode ? `${currentSelectedNode.sourceId}:${currentSelectedNode.key}` : "";
  return (
    <section className="project-zotero-collections" aria-label="当前项目的 Zotero 分类">
      <header>
        <div>
          <span className="project-zotero-eyebrow">ZOTERO</span>
          <h2>项目文件夹</h2>
          <p>浏览当前项目保留的 Zotero 多级目录；点击父文件夹可查看全部下级文献。</p>
        </div>
        <button type="button" onClick={() => void loadTrees()} disabled={isLoadingTrees} aria-label="刷新分类结构">
          <RefreshRounded className={isLoadingTrees ? "zotero-spin" : ""} />
          刷新
        </button>
      </header>
      {isCurrentProjectLoaded && error ? <div className="project-zotero-error">{error}</div> : null}
      {!isTreePending && !error && !hasTrees ? (
        <div className="project-zotero-empty">
          <FolderRounded />
          <div>
            <strong>当前项目还没有 Zotero 文件夹</strong>
            <span>可在下方添加 Zotero 数据源，并选择整个文库或指定多级分类。</span>
          </div>
          <a href="#zotero-data-sources">添加 Zotero 文件夹</a>
        </div>
      ) : (
        <div className="zotero-library-browser-grid">
        <nav aria-label="Zotero 分类树">
          {isTreePending ? (
            <div className="zotero-collection-papers-empty"><SyncRounded className="zotero-spin" />正在读取分类…</div>
          ) : trees.map((tree) => (
            <ul className="zotero-saved-tree" key={tree.sourceId}>
              {tree.roots.map((root) => (
                <CollectionTreeNode
                  key={root.key}
                  node={root}
                  selectedId={selectedId}
                  onSelect={(node) => void selectNode(node)}
                />
              ))}
            </ul>
          ))}
        </nav>
        <div className="zotero-collection-papers">
          {currentSelectedNode ? (
            <>
              <div className="zotero-collection-papers-heading">
                <div>
                  <strong>{currentSelectedNode.name}</strong>
                  <small>{currentSelectedNode.path}</small>
                </div>
                <span>{isLoadingPapers ? "正在读取…" : `${papers.length} 篇`}</span>
              </div>
              {isLoadingPapers ? (
                <div className="zotero-collection-papers-empty"><SyncRounded className="zotero-spin" />正在读取文献…</div>
              ) : papers.length ? (
                <ul>
                  {papers.map((paper) => (
                    <li key={paper.id}>
                      <AutoStoriesRounded />
                      <div>
                        <strong>{paper.title || "未命名文献"}</strong>
                        <small>
                          {[paper.authors?.slice(0, 3).join("、"), paper.year, paper.venue]
                            .filter(Boolean)
                            .join(" · ") || "暂无题录信息"}
                        </small>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="zotero-collection-papers-empty">该分类及其子分类中暂无已导入文献</div>
              )}
            </>
          ) : (
            <div className="zotero-collection-papers-empty">
              <FolderRounded />
              选择左侧分类查看当前项目文献
            </div>
          )}
        </div>
        </div>
      )}
    </section>
  );
}
