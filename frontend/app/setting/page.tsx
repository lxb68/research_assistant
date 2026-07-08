"use client";

import { useState } from "react";

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
  minimumLength: number;
  maximumSplitLength: number;
};

const STORAGE_KEY = "research-agent.settings";

const defaultSettings: SettingsState = {
  projectName: "我的论文项目",
  projectDesc: "用于解析学术论文的自动化流程",
  selectedModel: "gpt-4",
  taskType: "pdf-to-md",
  promptTemplate: "将以下 PDF 内容转换为 Markdown 格式，保留公式、表格和章节结构。",
  splitStrategy: "document-structure",
  minimumLength: 1500,
  maximumSplitLength: 2000,
};

const strategies = [
  {
    value: "document-structure" as const,
    label: "按文档结构切分",
    desc: "优先按标题、章节、段落等自然边界切分文本。",
  },
  {
    value: "custom-delimiter" as const,
    label: "按自定义分隔符切分",
    desc: "适合文本中已经存在明确分段标记的场景。",
  },
  {
    value: "fixed-char" as const,
    label: "按固定字符数切分",
    desc: "按字符长度范围直接切分，便于快速控制分块大小。",
  },
  {
    value: "fixed-token" as const,
    label: "按固定 Token 数切分",
    desc: "更贴近模型上下文限制，适合问答与向量化处理。",
  },
  {
    value: "code-intelligent" as const,
    label: "按代码语义智能切分",
    desc: "适合混合代码与技术文档的处理流程。",
  },
];

const TABS = ["基本信息", "模型配置", "任务配置", "提示词配置"] as const;
type TabIndex = 0 | 1 | 2 | 3;

function getStrategyDescription(strategy: SplitStrategy) {
  return strategies.find((item) => item.value === strategy)?.desc ?? "";
}

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
    setSettings((current) => ({ ...current, [key]: value }));
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
      <section className="dataset-browser-panel settings-panel">
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

        <div className="settings-divider" />

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
                选择用于 PDF 解析与内容生成的视觉语言模型。不同模型在公式识别、表格还原等方面表现会有差异。
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

              <label className="settings-row">
                <span className="settings-row-label">分割策略（Split Strategy）</span>
                <select
                  className="settings-input"
                  value={settings.splitStrategy}
                  onChange={(e) => set("splitStrategy", e.target.value as SplitStrategy)}
                >
                  {strategies.map((strategy) => (
                    <option key={strategy.value} value={strategy.value}>
                      {strategy.label}
                    </option>
                  ))}
                </select>
                <span className="settings-hint">
                  文本分割基于设置的长度范围进行操作，将输入文本按照规则分割成合适的段落，以便后续处理。
                </span>
                <span className="settings-hint">{getStrategyDescription(settings.splitStrategy)}</span>
              </label>

              <div className="settings-row">
                <span className="settings-row-label">最小长度（Minimum Length）</span>
                <div className="settings-slider-block">
                  <div className="settings-slider-meta">
                    <strong>{settings.minimumLength}</strong>
                    <span>默认值 1500</span>
                  </div>
                  <input
                    type="range"
                    min={500}
                    max={5000}
                    step={100}
                    className="settings-slider"
                    value={settings.minimumLength}
                    onChange={(e) => set("minimumLength", Number(e.target.value))}
                  />
                </div>
                <span className="settings-hint">
                  设定分割后每个文本片段的最小字符长度。若某段文本长度小于该值，会与相邻文本段合并，直至满足最小长度要求。
                </span>
              </div>

              <div className="settings-row">
                <span className="settings-row-label">最大分割长度（Maximum Split Length）</span>
                <div className="settings-slider-block">
                  <div className="settings-slider-meta">
                    <strong>{settings.maximumSplitLength}</strong>
                    <span>默认值 2000</span>
                  </div>
                  <input
                    type="range"
                    min={1000}
                    max={8000}
                    step={100}
                    className="settings-slider"
                    value={settings.maximumSplitLength}
                    onChange={(e) => set("maximumSplitLength", Number(e.target.value))}
                  />
                </div>
                <span className="settings-hint">
                  限制分割后每个文本片段的最大字符长度。超过该长度的文本会被分割成多个片段。
                </span>
              </div>
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

        <div className="settings-divider" />

        <div className="settings-footer">
          {saved && <span className="settings-toast">已保存</span>}
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
