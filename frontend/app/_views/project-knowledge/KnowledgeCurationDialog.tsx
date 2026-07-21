"use client";

import { FormEvent, useEffect, useState } from "react";
import styles from "./KnowledgeCurationDialog.module.css";

export type CurationEntityOption = {
  id: string;
  name: string;
  type: string;
};

type EditTreeDialog = {
  action: "edit";
  kind: "tree";
  id: string;
  label: string;
};

type EditEntityDialog = {
  action: "edit";
  kind: "entity";
  id: string;
  name: string;
  entityType: string;
  aliases: string[];
};

type EditRelationDialog = {
  action: "edit";
  kind: "relation";
  id: string;
  predicate: string;
  relationType: string;
  confidence: number;
  source: string;
  target: string;
};

type DeleteDialog = {
  action: "delete";
  kind: "tree" | "entity" | "relation";
  id: string;
  label: string;
  impactText: string;
};

export type KnowledgeCurationEditor = EditTreeDialog | EditEntityDialog | EditRelationDialog | DeleteDialog;

export type KnowledgeCurationValues = {
  label?: string;
  name?: string;
  entityType?: string;
  aliases?: string[];
  predicate?: string;
  relationType?: string;
  confidence?: number;
  source?: string;
  target?: string;
};

type KnowledgeCurationDialogProps = {
  editor: KnowledgeCurationEditor;
  entities: CurationEntityOption[];
  busy: boolean;
  error?: string;
  onClose: () => void;
  onSubmit: (values: KnowledgeCurationValues) => Promise<void>;
};

const KIND_LABELS = {
  tree: "领域节点",
  entity: "知识实体",
  relation: "语义关系",
};

export function KnowledgeCurationDialog({
  editor,
  entities,
  busy,
  error,
  onClose,
  onSubmit,
}: KnowledgeCurationDialogProps) {
  const [label, setLabel] = useState(editor.kind === "tree" && editor.action === "edit" ? editor.label : "");
  const [name, setName] = useState(editor.kind === "entity" && editor.action === "edit" ? editor.name : "");
  const [entityType, setEntityType] = useState(
    editor.kind === "entity" && editor.action === "edit" ? editor.entityType : "",
  );
  const [aliases, setAliases] = useState(
    editor.kind === "entity" && editor.action === "edit" ? editor.aliases.join("、") : "",
  );
  const [predicate, setPredicate] = useState(
    editor.kind === "relation" && editor.action === "edit" ? editor.predicate : "",
  );
  const [relationType, setRelationType] = useState(
    editor.kind === "relation" && editor.action === "edit" ? editor.relationType : "general",
  );
  const [confidence, setConfidence] = useState(
    editor.kind === "relation" && editor.action === "edit" ? editor.confidence : 0.5,
  );
  const [source, setSource] = useState(
    editor.kind === "relation" && editor.action === "edit" ? editor.source : "",
  );
  const [target, setTarget] = useState(
    editor.kind === "relation" && editor.action === "edit" ? editor.target : "",
  );

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [busy, onClose]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (editor.action === "delete") {
      await onSubmit({});
      return;
    }
    if (editor.kind === "tree") {
      await onSubmit({ label: label.trim() });
      return;
    }
    if (editor.kind === "entity") {
      await onSubmit({
        name: name.trim(),
        entityType: entityType.trim(),
        aliases: Array.from(new Set(
          aliases.split(/[、,，\n]/).map((item) => item.trim()).filter(Boolean),
        )),
      });
      return;
    }
    await onSubmit({
      predicate: predicate.trim(),
      relationType: relationType.trim(),
      confidence,
      source,
      target,
    });
  }

  const title = editor.action === "delete"
    ? `删除${KIND_LABELS[editor.kind]}`
    : `修改${KIND_LABELS[editor.kind]}`;

  return (
    <div
      className={styles.backdrop}
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className={`${styles.dialog}${editor.action === "delete" ? ` ${styles.destructive}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="knowledge-editor-title"
      >
        <header className={styles.header}>
          <div className={styles.icon} aria-hidden="true">
            {editor.action === "delete" ? "!" : editor.kind === "tree" ? "T" : editor.kind === "entity" ? "E" : "R"}
          </div>
          <div>
            <span>人工修订 · {KIND_LABELS[editor.kind]}</span>
            <h2 id="knowledge-editor-title">{title}</h2>
            <p>
              {editor.action === "delete"
                ? "删除采用可恢复修订，不会改写模型生成的原始分析文件。"
                : "保存后，领域树展示和知识检索会同时使用这次修订。"}
            </p>
          </div>
          <button
            type="button"
            className={styles.close}
            aria-label="关闭编辑器"
            disabled={busy}
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <form className={styles.form} onSubmit={(event) => void handleSubmit(event)}>
          {error ? <div className={styles.error} role="alert">{error}</div> : null}
          {editor.action === "delete" ? (
            <div className={styles.deleteSummary}>
              <span>即将删除</span>
              <strong>{editor.label}</strong>
              <p>{editor.impactText}</p>
            </div>
          ) : editor.kind === "tree" ? (
            <label className={styles.field}>
              <span>节点名称</span>
              <input
                autoFocus
                required
                maxLength={500}
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                placeholder="请输入清晰、具体的领域名称"
              />
              <small>修改会同步到知识图谱中的对应领域节点。</small>
            </label>
          ) : editor.kind === "entity" ? (
            <>
              <div className={styles.fieldGrid}>
                <label className={styles.field}>
                  <span>实体名称</span>
                  <input
                    autoFocus
                    required
                    maxLength={1000}
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                  />
                </label>
                <label className={styles.field}>
                  <span>实体类型</span>
                  <input
                    required
                    maxLength={200}
                    value={entityType}
                    onChange={(event) => setEntityType(event.target.value)}
                    placeholder="例如：算法、数据集、指标"
                  />
                </label>
              </div>
              <label className={styles.field}>
                <span>别名</span>
                <textarea
                  rows={3}
                  value={aliases}
                  onChange={(event) => setAliases(event.target.value)}
                  placeholder="多个别名可使用逗号、顿号或换行分隔"
                />
                <small>别名会参与图谱检索，但不会改变原文证据。</small>
              </label>
            </>
          ) : (
            <>
              <label className={styles.field}>
                <span>关系表达</span>
                <input
                  autoFocus
                  required
                  maxLength={500}
                  value={predicate}
                  onChange={(event) => setPredicate(event.target.value)}
                  placeholder="例如：应用于、优于、导致"
                />
              </label>
              <div className={styles.fieldGrid}>
                <label className={styles.field}>
                  <span>起点实体</span>
                  <select required value={source} onChange={(event) => setSource(event.target.value)}>
                    {entities.map((entity) => (
                      <option key={entity.id} value={entity.id}>{entity.name} · {entity.type}</option>
                    ))}
                  </select>
                </label>
                <label className={styles.field}>
                  <span>终点实体</span>
                  <select required value={target} onChange={(event) => setTarget(event.target.value)}>
                    {entities.map((entity) => (
                      <option key={entity.id} value={entity.id}>{entity.name} · {entity.type}</option>
                    ))}
                  </select>
                </label>
              </div>
              <div className={styles.fieldGrid}>
                <label className={styles.field}>
                  <span>关系类型</span>
                  <select value={relationType} onChange={(event) => setRelationType(event.target.value)}>
                    <option value="general">一般关系</option>
                    <option value="causal">因果关系</option>
                    <option value="comparison">比较关系</option>
                    <option value="experimental">实验关系</option>
                    <option value="property">属性关系</option>
                    {!["general", "causal", "comparison", "experimental", "property"].includes(relationType) ? (
                      <option value={relationType}>{relationType}</option>
                    ) : null}
                  </select>
                </label>
                <label className={styles.field}>
                  <span>置信度 <strong>{Math.round(confidence * 100)}%</strong></span>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.01"
                    value={confidence}
                    onChange={(event) => setConfidence(Number(event.target.value))}
                  />
                </label>
              </div>
            </>
          )}

          <footer className={styles.footer}>
            <button type="button" className={styles.cancel} disabled={busy} onClick={onClose}>
              取消
            </button>
            <button type="submit" className={styles.submit} disabled={busy}>
              {busy ? "正在保存…" : editor.action === "delete" ? "确认删除" : "保存修改"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
