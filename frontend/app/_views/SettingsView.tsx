/* 编辑工作区、模型、任务和提示词配置，并同步到本地与后端。 */

"use client";

import { useEffect, useState } from "react";
import { buildApiUrl } from "@/lib/api";
import { WORKSPACE_SETTINGS_STORAGE_KEY } from "@/lib/constants";

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
  provider?: string;
  protocol?: ModelProtocol;
  model?: string;
  baseUrl?: string;
  requiresApiKey?: boolean;
  hasApiKey?: boolean;
  maskedApiKey?: string;
  systemConstraint?: string;
};

type ModelProtocol = "openai_compatible" | "ollama" | "anthropic" | "gemini";

type ModelProvider = {
  id: string;
  name: string;
  protocol: ModelProtocol;
  baseUrl: string;
  requiresApiKey: boolean;
  modelPlaceholder: string;
};

type ModelProvidersResponse = {
  providers?: ModelProvider[];
  detail?: string;
};

type ModelDiscoveryResponse = {
  models?: string[];
  count?: number;
  detail?: string;
};

// 后端目录加载前使用最小兜底项，确保设置页始终可以编辑自定义服务。
const fallbackProviders: ModelProvider[] = [
  {
    id: "openai",
    name: "OpenAI",
    protocol: "openai_compatible",
    baseUrl: "https://api.openai.com/v1",
    requiresApiKey: true,
    modelPlaceholder: "例如 gpt-4o-mini",
  },
  {
    id: "ollama",
    name: "Ollama（本地）",
    protocol: "ollama",
    baseUrl: "http://127.0.0.1:11434",
    requiresApiKey: false,
    modelPlaceholder: "例如 qwen3:8b",
  },
  {
    id: "custom",
    name: "自定义服务",
    protocol: "openai_compatible",
    baseUrl: "",
    requiresApiKey: false,
    modelPlaceholder: "请输入服务端使用的模型 ID",
  },
];

const protocolLabels: Record<ModelProtocol, string> = {
  openai_compatible: "OpenAI 兼容协议",
  ollama: "Ollama 原生协议",
  anthropic: "Anthropic Messages 协议",
  gemini: "Gemini generateContent 协议",
};

const defaultSettings: SettingsState = {
  projectName: "我的论文项目",
  projectDesc: "用于解析学术论文的自动化处理流程",
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
    desc: "适合文本中已经存在明确分段标记的场景。",
  },
  {
    value: "fixed-char" as const,
    label: "按固定字符数切分",
    desc: "按字符长度直接切分，便于快速控制块大小。",
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

/** 返回分块策略的中文说明。 */
function getStrategyDescription(strategy: SplitStrategy) {
  return strategies.find((item) => item.value === strategy)?.desc ?? "";
}

/** 管理工作区与模型配置表单。 */
export default function SettingsWorkspace() {
  const [settings, setSettings] = useState<SettingsState>(() => {
    if (typeof window === "undefined") return defaultSettings;
    try {
      const raw = window.localStorage.getItem(WORKSPACE_SETTINGS_STORAGE_KEY);
      if (!raw) return defaultSettings;
      const parsed = JSON.parse(raw) as Partial<SettingsState>;
      return { ...defaultSettings, ...parsed };
    } catch {
      return defaultSettings;
    }
  });
  const [activeTab, setActiveTab] = useState<TabIndex>(0);
  const [saved, setSaved] = useState(false);
  const [providers, setProviders] = useState<ModelProvider[]>(fallbackProviders);
  const [providerId, setProviderId] = useState("openai");
  const [protocol, setProtocol] = useState<ModelProtocol>("openai_compatible");
  const [modelName, setModelName] = useState(defaultSettings.selectedModel);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [maskedApiKey, setMaskedApiKey] = useState("");
  const [modelConfigured, setModelConfigured] = useState(false);
  const [isLoadingModelConfig, setIsLoadingModelConfig] = useState(true);
  const [isSavingModelConfig, setIsSavingModelConfig] = useState(false);
  const [isDiscoveringModels, setIsDiscoveringModels] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);
  const [modelMessage, setModelMessage] = useState("");
  const [modelError, setModelError] = useState("");
  const [systemConstraint, setSystemConstraint] = useState("");
  const selectedProvider = providers.find((item) => item.id === providerId) ?? fallbackProviders[2];
  const apiKeyRequired = selectedProvider.id === "custom"
    ? protocol === "anthropic" || protocol === "gemini"
    : selectedProvider.requiresApiKey;

  /** 以类型安全方式更新单个设置字段。 */
  function set<K extends keyof SettingsState>(key: K, value: SettingsState[K]) {
    setSettings((current) => ({ ...current, [key]: value }));
    if (key === "selectedModel") {
      setModelName(String(value));
    }
    setSaved(false);
  }

  useEffect(() => {
    let cancelled = false;

    /** 从后端加载脱敏后的模型配置。 */
    async function loadModelConfig() {
      setIsLoadingModelConfig(true);
      setModelError("");

      try {
        const [configResponse, providersResponse] = await Promise.all([
          fetch(buildApiUrl("/api/settings/model-config")),
          fetch(buildApiUrl("/api/settings/model-providers")),
        ]);
        const payload = (await configResponse.json().catch(() => ({}))) as ModelConfigResponse & { detail?: string };
        const providersPayload = (await providersResponse.json().catch(() => ({}))) as ModelProvidersResponse;
        if (!configResponse.ok) {
          throw new Error(payload.detail || "加载模型配置失败");
        }
        if (!providersResponse.ok) {
          throw new Error(providersPayload.detail || "加载模型供应商失败");
        }

        if (cancelled) {
          return;
        }

        const nextProviders = providersPayload.providers?.length ? providersPayload.providers : fallbackProviders;
        setProviders(nextProviders);
        setProviderId(payload.provider || "custom");
        setProtocol(payload.protocol || "openai_compatible");
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

  /** 把工作区设置保存到浏览器。 */
  function saveLocalSettings() {
    window.localStorage.setItem(
      WORKSPACE_SETTINGS_STORAGE_KEY,
      JSON.stringify({ ...settings, selectedModel: modelName }),
    );
    setSaved(true);
  }

  /** 校验并向后端保存模型配置。 */
  async function saveModelConfig() {
    setIsSavingModelConfig(true);
    setModelError("");
    setModelMessage("");

    try {
      const response = await fetch(buildApiUrl("/api/settings/model-config"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          provider: providerId,
          protocol,
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
      setProviderId(payload.provider || providerId);
      setProtocol(payload.protocol || protocol);
      setMaskedApiKey(payload.maskedApiKey || "");
      setSystemConstraint(payload.systemConstraint || "");
      setApiKeyInput("");
      setSettings((current) => ({ ...current, selectedModel: modelName }));
      saveLocalSettings();
      setModelMessage("模型参数已保存。");
    } catch (error) {
      setModelError(error instanceof Error ? error.message : "保存模型配置失败");
    } finally {
      setIsSavingModelConfig(false);
    }
  }

  /** 切换供应商，并应用其默认协议、地址和模型提示。 */
  function selectProvider(nextProviderId: string) {
    const nextProvider = providers.find((item) => item.id === nextProviderId) ?? fallbackProviders[2];
    setProviderId(nextProvider.id);
    setProtocol(nextProvider.protocol);
    setBaseUrl(nextProvider.baseUrl);
    setModelName("");
    setApiKeyInput("");
    setMaskedApiKey("");
    setDiscoveredModels([]);
    setModelConfigured(false);
    setModelError("");
    setModelMessage(`${nextProvider.name} 已选中，请发现或填写模型后保存。`);
  }

  /** 从当前云端账号或本地运行时发现可用模型列表。 */
  async function discoverAvailableModels() {
    setIsDiscoveringModels(true);
    setModelError("");
    setModelMessage("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/model-config/discover"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: providerId,
          protocol,
          base_url: baseUrl,
          api_key: apiKeyInput,
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as ModelDiscoveryResponse;
      if (!response.ok) {
        throw new Error(payload.detail || "发现模型失败");
      }
      const models = payload.models || [];
      setDiscoveredModels(models);
      if (!modelName && models.length > 0) {
        setModelName(models[0]);
      }
      setModelMessage(models.length > 0 ? `已发现 ${models.length} 个可用模型。` : "服务连接成功，但没有返回可用模型。");
    } catch (error) {
      setDiscoveredModels([]);
      setModelError(error instanceof Error ? error.message : "发现模型失败");
    } finally {
      setIsDiscoveringModels(false);
    }
  }

  /** 恢复设置表单的默认值。 */
  function reset() {
    setSettings(defaultSettings);
    setProviderId("openai");
    setProtocol("openai_compatible");
    setModelName(defaultSettings.selectedModel);
    setBaseUrl("https://api.openai.com/v1");
    setApiKeyInput("");
    setDiscoveredModels([]);
    window.localStorage.removeItem(WORKSPACE_SETTINGS_STORAGE_KEY);
    setSaved(true);
  }

  /** 保存本地设置和模型配置。 */
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
                  {isLoadingModelConfig
                    ? "正在读取模型配置..."
                    : modelConfigured
                      ? `${selectedProvider.name} / ${modelName} 已配置`
                      : "尚未配置可用模型"}
                </div>
              </div>

              <label className="settings-row">
                <span className="settings-row-label">模型供应商</span>
                <select
                  className="settings-input"
                  value={providerId}
                  onChange={(event) => selectProvider(event.target.value)}
                >
                  {providers.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.name}
                    </option>
                  ))}
                </select>
                <span className="settings-hint">
                  选择供应商会自动填入推荐协议和 Base URL；自定义服务可手工调整协议。
                </span>
              </label>

              {providerId === "custom" ? (
                <label className="settings-row">
                  <span className="settings-row-label">接口协议</span>
                  <select
                    className="settings-input"
                    value={protocol}
                    onChange={(event) => setProtocol(event.target.value as ModelProtocol)}
                  >
                    {Object.entries(protocolLabels).map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
              ) : (
                <div className="settings-row">
                  <span className="settings-row-label">接口协议</span>
                  <div className="settings-readonly-value">{protocolLabels[protocol]}</div>
                </div>
              )}

              <label className="settings-row">
                <span className="settings-row-label">模型名称</span>
                <div className="settings-model-picker">
                  <input
                    type="text"
                    className="settings-input settings-input-mono"
                    value={modelName}
                    onChange={(e) => setModelName(e.target.value)}
                    placeholder={selectedProvider.modelPlaceholder}
                    list="available-model-options"
                  />
                  <button
                    type="button"
                    className="settings-btn settings-btn-ghost settings-discover-btn"
                    onClick={() => { void discoverAvailableModels(); }}
                    disabled={isDiscoveringModels || !baseUrl.trim()}
                  >
                    {isDiscoveringModels ? "发现中..." : "发现模型"}
                  </button>
                </div>
                <datalist id="available-model-options">
                  {discoveredModels.map((model) => <option key={model} value={model} />)}
                </datalist>
                <span className="settings-hint">
                  {providerId === "ollama"
                    ? "从本机 Ollama 的 /api/tags 读取已安装模型，请先启动 Ollama 服务。"
                    : "可点击“发现模型”读取账号可用模型，也可以直接填写模型 ID。"}
                </span>
              </label>

              <label className="settings-row">
                <span className="settings-row-label">Base URL</span>
                <input
                  type="text"
                  className="settings-input settings-input-mono"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder={selectedProvider.baseUrl || "请输入模型服务根地址"}
                />
                <span className="settings-hint">填写 API 根地址即可，不要重复添加具体聊天端点。</span>
              </label>

              <label className="settings-row">
                <span className="settings-row-label">API Key{apiKeyRequired ? "（必填）" : "（可选）"}</span>
                <input
                  type="password"
                  className="settings-input settings-input-mono"
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder={
                    providerId === "ollama"
                      ? "Ollama 本地服务不需要密钥"
                      : maskedApiKey
                        ? "留空则继续使用当前供应商密钥"
                        : apiKeyRequired
                          ? "请输入供应商 API Key"
                          : "本地服务通常可以留空"
                  }
                  disabled={providerId === "ollama"}
                  autoComplete="new-password"
                />
                <span className="settings-hint">
                  密钥只保存在后端并以掩码形式显示；切换供应商时不会复用其他供应商的密钥。
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
              <span className="settings-hint">定义内容发送给模型时使用的提示模板。</span>
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
