"use client";

import { useEffect, useState } from "react";

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

type ModelConfigResponse = {
  configured?: boolean;
  model?: string;
  baseUrl?: string;
  hasApiKey?: boolean;
  maskedApiKey?: string;
  systemConstraint?: string;
};

const STORAGE_KEY = "research-agent.settings";
const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4000";

const defaultSettings: SettingsState = {
  projectName: "我的论文项目",
  projectDesc: "用于解析学术论文的自动化流程",
  selectedModel: "gpt-4o-mini",
  taskType: "pdf-to-md",
  promptTemplate: "将输入内容整理为结构化 Markdown，保留标题、公式、表格和关键结论。",
  splitStrategy: "document-structure",
  minimumLength: 1500,
  maximumSplitLength: 2000,
};

const strategies = [
  {
    value: "document-structure" as const,
    label: "按文档结构切分",
    desc: "优先按标题、章节和自然段落切分文本。",
  },
  {
    value: "custom-delimiter" as const,
    label: "按自定义分隔符切分",
    desc: "适合文本中已存在明确分段标记的场景。",
  },
  {
    value: "fixed-char" as const,
    label: "按固定字符数切分",
    desc: "按字符长度直接分割，便于快速控制块大小。",
  },
  {
    value: "fixed-token" as const,
    label: "按固定 Token 数切分",
    desc: "更贴近模型上下文限制，适合问答与向量化处理。",
  },
  {
    value: "code-intelligent" as const,
    label: "按代码语义切分",
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
  const [modelName, setModelName] = useState(defaultSettings.selectedModel);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [maskedApiKey, setMaskedApiKey] = useState("");
  const [modelConfigured, setModelConfigured] = useState(false);
  const [isLoadingModelConfig, setIsLoadingModelConfig] = useState(true);
  const [isSavingModelConfig, setIsSavingModelConfig] = useState(false);
  const [modelMessage, setModelMessage] = useState("");
  const [modelError, setModelError] = useState("");
  const [systemConstraint, setSystemConstraint] = useState("");

  function set<K extends keyof SettingsState>(key: K, value: SettingsState[K]) {
    setSettings((current) => ({ ...current, [key]: value }));
    if (key === "selectedModel") {
      setModelName(String(value));
    }
    setSaved(false);
  }

  useEffect(() => {
    let cancelled = false;

    async function loadModelConfig() {
      setIsLoadingModelConfig(true);
      setModelError("");

      try {
        const response = await fetch(new URL("/api/settings/model-config", apiBaseUrl));
        const payload = (await response.json().catch(() => ({}))) as ModelConfigResponse & { detail?: string };
        if (!response.ok) {
          throw new Error(payload.detail || "加载模型配置失败");
        }

        if (cancelled) {
          return;
        }

        setModelConfigured(Boolean(payload.configured));
        setMaskedApiKey(payload.maskedApiKey || "");
        setBaseUrl(payload.baseUrl || "");
        setModelName(payload.model || defaultSettings.selectedModel);
        setSystemConstraint(payload.systemConstraint || "");
        setSettings((current) => ({
          ...current,
          selectedModel: payload.model || current.selectedModel || defaultSettings.selectedModel,
        }));
      } catch (error) {
        if (!cancelled) {
          setModelError(error instanceof Error ? error.message : "加载模型配置失败");
        }
      } finally {
        if (!cancelled) {
          setIsLoadingModelConfig(false);
        }
      }
    }

    void loadModelConfig();
    return () => {
      cancelled = true;
    };
  }, []);

  function saveLocalSettings() {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...settings, selectedModel: modelName }));
    setSaved(true);
  }

  async function saveModelConfig() {
    setIsSavingModelConfig(true);
    setModelError("");
    setModelMessage("");

    try {
      const response = await fetch(new URL("/api/settings/model-config", apiBaseUrl), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: modelName,
          base_url: baseUrl,
          api_key: apiKeyInput,
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as ModelConfigResponse & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail || "保存模型配置失败");
      }

      setModelConfigured(Boolean(payload.configured));
      setMaskedApiKey(payload.maskedApiKey || "");
      setSystemConstraint(payload.systemConstraint || "");
      setApiKeyInput("");
      setSettings((current) => ({ ...current, selectedModel: modelName }));
      saveLocalSettings();
      setModelMessage("模型参数已保存");
    } catch (error) {
      setModelError(error instanceof Error ? error.message : "保存模型配置失败");
    } finally {
      setIsSavingModelConfig(false);
    }
  }

  function reset() {
    setSettings(defaultSettings);
    setModelName(defaultSettings.selectedModel);
    setApiKeyInput("");
    window.localStorage.removeItem(STORAGE_KEY);
    setSaved(true);
  }

  async function handleSave() {
    if (activeTab === 1) {
      await saveModelConfig();
      return;
    }
    saveLocalSettings();
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
            <>
              <div className="settings-row">
                <span className="settings-row-label">当前状态</span>
                <div className={`workspace-status ${modelConfigured ? "workspace-status-active" : ""}`}>
                  {isLoadingModelConfig ? "正在读取模型配置..." : modelConfigured ? "模型参数已配置" : "尚未配置模型参数"}
                </div>
              </div>

              <label className="settings-row">
                <span className="settings-row-label">模型名称</span>
                <input
                  type="text"
                  className="settings-input"
                  value={modelName}
                  onChange={(e) => setModelName(e.target.value)}
                  placeholder="例如 gpt-4o-mini"
                />
              </label>

              <label className="settings-row">
                <span className="settings-row-label">Base URL</span>
                <input
                  type="text"
                  className="settings-input settings-input-mono"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.openai.com/v1"
                />
              </label>

              <label className="settings-row">
                <span className="settings-row-label">模型密钥</span>
                <input
                  type="password"
                  className="settings-input settings-input-mono"
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder={maskedApiKey ? "留空则继续使用当前密钥" : "请输入新的模型密钥"}
                  autoComplete="new-password"
                />
                <span className="settings-hint">
                  已保存密钥仅做掩码显示，不会通过接口返回明文。
                </span>
                {maskedApiKey ? <span className="settings-secret-preview">当前密钥：{maskedApiKey}</span> : null}
              </label>

              <label className="settings-row">
                <span className="settings-row-label">系统防泄露约束</span>
                <textarea
                  className="settings-input settings-input-mono"
                  rows={4}
                  value={systemConstraint}
                  readOnly
                />
                <span className="settings-hint">
                  该系统约束会附加到模型请求中，用于限制模型输出任何密钥、令牌或隐藏配置。
                </span>
              </label>

              {modelMessage ? <div className="domain-tree-status">{modelMessage}</div> : null}
              {modelError ? <div className="domain-tree-error">{modelError}</div> : null}
            </>
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
                <span className="settings-row-label">切分策略</span>
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
                <span className="settings-hint">{getStrategyDescription(settings.splitStrategy)}</span>
              </label>

              <div className="settings-row">
                <span className="settings-row-label">最小长度</span>
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
              </div>

              <div className="settings-row">
                <span className="settings-row-label">最大切分长度</span>
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
              <span className="settings-hint">定义内容发送给模型时的提示模板。</span>
            </label>
          )}
        </div>

        <div className="settings-divider" />

        <div className="settings-footer">
          {saved && activeTab !== 1 ? <span className="settings-toast">已保存</span> : null}
          <button type="button" className="settings-btn settings-btn-ghost" onClick={reset}>
            恢复默认
          </button>
          <button
            type="button"
            className="settings-btn settings-btn-primary"
            onClick={() => {
              void handleSave();
            }}
            disabled={isSavingModelConfig}
          >
            {activeTab === 1 && isSavingModelConfig ? "保存中..." : "保存"}
          </button>
        </div>
      </section>
    </main>
  );
}
