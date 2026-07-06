"use client";

import { useState } from "react";

/* ================================================================
   类型与常量
   ================================================================ */

type SplitStrategy =
  | "document-structure"
  | "custom-delimiter"
  | "fixed-char"
  | "fixed-token"
  | "code-intelligent";

type SettingsState = {
  projectName: string;
  projectDesc: string;
  selectedModel: string;
  taskType: string;
  promptTemplate: string;
  splitStrategy: SplitStrategy;
  questionLength: number;
};

const STORAGE_KEY = "research-agent.settings";

const defaultSettings: SettingsState = {
  projectName: "我的论文项目",
  projectDesc: "用于解析学术论文的自动化流程",
  selectedModel: "gpt-4",
  taskType: "pdf-to-md",
  promptTemplate: "将以下 PDF 内容转换为 Markdown 格式，保留公式、表格和章节结构。",
  splitStrategy: "document-structure",
  questionLength: 240,
};

const strategies = [
  { value: "document-structure" as const, label: "按文档结构切分", desc: "优先依据标题、章节、表格与段落边界切分。" },
  { value: "custom-delimiter" as const, label: "按自定义分隔符切分", desc: "适合已经有明确分段标记的文本材料。" },
  { value: "fixed-char" as const, label: "按固定字符数切分", desc: "实现简单，适合快速试验与基准对比。" },
  { value: "fixed-token" as const, label: "按固定 Token 数切分", desc: "更接近模型上下文限制，适合问答与嵌入场景。" },
  { value: "code-intelligent" as const, label: "按代码语义智能切分", desc: "适合混合代码与技术文档的处理流程。" },
];

const TABS = ["基本信息", "模型配置", "任务配置", "提示词配置"] as const;
type TabIndex = 0 | 1 | 2 | 3;

/* ================================================================
   组件
   ================================================================ */

export default function SettingsWorkspace() {
  const [settings, setSettings] = useState<SettingsState>(() => {
    if (typeof window === "undefined") return defaultSettings;
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return defaultSettings;
      const parsed = JSON.parse(raw) as Partial<SettingsState>;
      return { ...defaultSettings, ...parsed };
    } catch {
      return defaultSettings;
    }
  });

  const [activeTab, setActiveTab] = useState<TabIndex>(0);
  const [saved, setSaved] = useState(false);

  function set<K extends keyof SettingsState>(key: K, value: SettingsState[K]) {
    setSettings((s) => ({ ...s, [key]: value }));
    setSaved(false);
  }

  function save() {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    setSaved(true);
  }

  function reset() {
    setSettings(defaultSettings);
    window.localStorage.removeItem(STORAGE_KEY);
    setSaved(true);
  }

  return (
    <main className="dataset-browser-page">
      <section className="dataset-browser-panel">

        {/* Tab 栏 */}
        <nav className="settings-tabs">
          {TABS.map((label, i) => (
            <button
              key={label}
              type="button"
              className={`settings-tab-btn${activeTab === i ? " is-active" : ""}`}
              onClick={() => setActiveTab(i as TabIndex)}
            >
              {label}
            </button>
          ))}
        </nav>

        {/* 分隔线 */}
        <div className="settings-divider" />

        {/* 内容 */}
        <div className="settings-body">
          {activeTab === 0 && (
            <>
              <label className="settings-row">
                <span className="settings-row-label">项目名称</span>
                <input
                  type="text"
                  className="settings-input"
                  value={settings.projectName}
                  onChange={(e) => set("projectName", e.target.value)}
                />
              </label>
              <label className="settings-row">
                <span className="settings-row-label">项目描述</span>
                <textarea
                  className="settings-input"
                  rows={4}
                  value={settings.projectDesc}
                  onChange={(e) => set("projectDesc", e.target.value)}
                />
              </label>
            </>
          )}

          {activeTab === 1 && (
            <label className="settings-row">
              <span className="settings-row-label">视觉模型</span>
              <select
                className="settings-input"
                value={settings.selectedModel}
                onChange={(e) => set("selectedModel", e.target.value)}
              >
                <option value="gpt-4">GPT-4 Vision</option>
                <option value="gpt-4o">GPT-4o</option>
                <option value="claude-3">Claude 3 Opus</option>
                <option value="gemini-pro">Gemini Pro Vision</option>
              </select>
              <span className="settings-hint">
                选择用于 PDF 解析与内容生成的视觉语言模型。不同模型在公式识别、表格还原等方面表现有差异。
              </span>
            </label>
          )}

          {activeTab === 2 && (
            <>
              <label className="settings-row">
                <span className="settings-row-label">任务类型</span>
                <select
                  className="settings-input"
                  value={settings.taskType}
                  onChange={(e) => set("taskType", e.target.value)}
                >
                  <option value="pdf-to-md">PDF 转 Markdown</option>
                  <option value="pdf-to-json">PDF 转 JSON</option>
                  <option value="image-to-md">图片转 Markdown</option>
                </select>
              </label>

              <div className="settings-row">
                <span className="settings-row-label">文本切分策略</span>
                <div className="settings-radios">
                  {strategies.map((s) => (
                    <label
                      key={s.value}
                      className={`settings-radio${settings.splitStrategy === s.value ? " is-checked" : ""}`}
                    >
                      <input
                        type="radio"
                        name="splitStrategy"
                        value={s.value}
                        checked={settings.splitStrategy === s.value}
                        onChange={() => set("splitStrategy", s.value)}
                      />
                      <span>
                        <strong>{s.label}</strong>
                        <small>{s.desc}</small>
                      </span>
                    </label>
                  ))}
                </div>
              </div>

              <label className="settings-row">
                <span className="settings-row-label">题目最大长度</span>
                <span className="settings-inline">
                  <input
                    type="number"
                    className="settings-input settings-input-num"
                    value={settings.questionLength}
                    min={1}
                    max={1000}
                    onChange={(e) => set("questionLength", Number(e.target.value) || 1)}
                  />
                  <span className="settings-hint">1–1000，控制自动生成问题的长度上限。</span>
                </span>
              </label>
            </>
          )}

          {activeTab === 3 && (
            <label className="settings-row">
              <span className="settings-row-label">Prompt 模板</span>
              <textarea
                className="settings-input settings-input-mono"
                rows={10}
                value={settings.promptTemplate}
                onChange={(e) => set("promptTemplate", e.target.value)}
              />
              <span className="settings-hint">
                定义将 PDF 内容发送给模型时的系统提示。可使用占位符引用文档变量。
              </span>
            </label>
          )}
        </div>

        {/* 分隔线 */}
        <div className="settings-divider" />

        {/* 底部操作 */}
        <div className="settings-footer">
          {saved && <span className="settings-toast">✓ 已保存</span>}
          <button type="button" className="settings-btn settings-btn-ghost" onClick={reset}>
            恢复默认
          </button>
          <button type="button" className="settings-btn settings-btn-primary" onClick={save}>
            保存
          </button>
        </div>
      </section>
    </main>
  );
}
