/* 管理真实生效的模型连接、文档分块与高级可靠性配置。 */

"use client";

import { useEffect, useMemo, useState } from "react";
import { buildApiUrl } from "@/lib/api";
import {
  DEFAULT_MAXIMUM_SPLIT_LENGTH,
  DEFAULT_MINIMUM_SPLIT_LENGTH,
  WORKSPACE_SETTINGS_STORAGE_KEY,
} from "@/lib/constants";

type ModelProtocol = "openai_compatible" | "ollama" | "anthropic" | "gemini";
type SettingsTab = "model" | "document" | "research" | "environment" | "advanced";
type ConnectionStatus = "unconfigured" | "unverified" | "testing" | "available" | "error";
type ExternalServiceId = "tencent_translation" | "mineru";

type DocumentSettings = {
  minimumLength: number;
  maximumSplitLength: number;
};

type ModelProvider = {
  id: string;
  name: string;
  protocol: ModelProtocol;
  baseUrl: string;
  requiresApiKey: boolean;
  modelPlaceholder: string;
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
  allowHeuristicFallback?: boolean;
  secretStorage?: "windows_dpapi" | "environment" | "legacy_plaintext" | "none";
  systemConstraint?: string;
  detail?: string;
};

type RuntimeIntegration = {
  id: string;
  name: string;
  configured: boolean;
  details: string;
};

type RuntimeConfigResponse = {
  restartRequired?: boolean;
  server?: Record<string, unknown>;
  research?: Record<string, unknown>;
  retrieval?: Record<string, unknown>;
  documents?: Record<string, unknown>;
  workers?: Record<string, unknown>;
  storage?: Record<string, unknown>;
  integrations?: RuntimeIntegration[];
};

type EnvField = {
  key: string;
  label: string;
  kind: "text" | "integer" | "float" | "boolean" | "secret" | "choice";
  value?: string | number | boolean;
  configured: boolean;
  source: "env_file" | "runtime_default";
  description: string;
  min?: number;
  max?: number;
  options?: string[];
};

type EnvConfigResponse = {
  restartRequired?: boolean;
  groups?: Array<{ id: string; label: string; description: string; fields: EnvField[] }>;
};

type EnvValue = string | number | boolean | null;

const fallbackProviders: ModelProvider[] = [
  {
    id: "openai",
    name: "OpenAI",
    protocol: "openai_compatible",
    baseUrl: "https://api.openai.com/v1",
    requiresApiKey: true,
    modelPlaceholder: "例如 gpt-4.1-mini",
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

const tabItems: Array<{ id: SettingsTab; label: string; description: string }> = [
  { id: "model", label: "模型连接", description: "生成模型与向量模型" },
  { id: "document", label: "文档处理", description: "控制 PDF 重新解析时的分块长度" },
  { id: "research", label: "研究配置", description: "学术检索与研究编排" },
  { id: "environment", label: "系统环境", description: "服务并发与运行状态" },
  { id: "advanced", label: "高级设置", description: "管理降级策略、安全信息和危险操作" },
];

const envSectionCatalog: Record<string, Array<{ id: string; label: string; keys: string[] }>> = {
  integrations: [
    { id: "translation", label: "腾讯云翻译", keys: ["TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENT_TRANSLATION_REGION"] },
    { id: "literature", label: "学术检索", keys: ["NCBI_EMAIL", "NCBI_API_KEY", "IEEE_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"] },
    { id: "remote_embedding", label: "远程向量", keys: ["RAG_EMBEDDING_API_KEY", "RAG_EMBEDDING_BASE_URL", "RAG_EMBEDDING_MODEL"] },
    { id: "local_embedding", label: "本地向量", keys: ["RAG_LOCAL_EMBEDDING_BASE_URL", "RAG_LOCAL_EMBEDDING_MODEL", "RAG_LOCAL_EMBEDDING_PROTOCOL", "RAG_LOCAL_EMBEDDING_API_KEY"] },
  ],
  research: [
    { id: "research_budget", label: "研究预算", keys: ["RESEARCH_AGENT_MAX_PAPERS", "RESEARCH_AGENT_MAX_SOURCES", "RESEARCH_AGENT_MAX_CONTEXT_CHARS", "RESEARCH_AGENT_REQUEST_TIMEOUT"] },
    { id: "orchestration", label: "任务编排", keys: ["ORCHESTRATOR_MIN_EVIDENCE", "ORCHESTRATOR_MAX_RETRIEVAL_ROUNDS", "ORCHESTRATOR_MAX_ACTION_ROUNDS", "ORCHESTRATOR_SEARCH_LIMIT_PER_SOURCE"] },
    { id: "chunking", label: "向量分块", keys: ["RAG_CHUNK_TARGET_TOKENS", "RAG_CHUNK_MAX_TOKENS", "RAG_CHUNK_OVERLAP_TOKENS"] },
    { id: "ranking", label: "混合排序", keys: ["RAG_BM25_WEIGHT", "RAG_VECTOR_WEIGHT"] },
    { id: "graph", label: "知识图谱", keys: ["HYBRID_GRAPH_ENABLED", "HYBRID_GRAPH_PROJECT_ID"] },
  ],
  documents: [
    { id: "mineru", label: "MinerU", keys: ["MINERU_API_TOKEN", "MINERU_API_BASE", "MINERU_MODEL_VERSION", "MINERU_ENABLE_LOCAL_CLI_FALLBACK", "MINERU_REQUEST_TIMEOUT_SECONDS", "MINERU_CLOUD_TIMEOUT_SECONDS"] },
  ],
  server: [
    { id: "http", label: "HTTP 服务", keys: ["HOST", "PORT", "CORS_ORIGINS", "REQUEST_TIMEOUT", "LOG_LEVEL"] },
    { id: "workers", label: "任务并发", keys: ["BACKGROUND_JOB_MAX_WORKERS", "BACKGROUND_JOB_MAX_PENDING_TASKS", "STREAM_MAX_WORKERS", "STREAM_MAX_PENDING_TASKS", "SEMANTIC_GRAPH_MAX_WORKERS"] },
  ],
};

const runtimeLabels: Record<string, string> = {
  host: "监听地址",
  port: "监听端口",
  corsOrigins: "允许的前端来源",
  requestTimeoutSeconds: "请求超时（秒）",
  logLevel: "日志级别",
  maxPapers: "最大候选论文数",
  maxSources: "最大证据来源数",
  maxContextChars: "最大上下文字符数",
  minimumEvidence: "最少证据数",
  maxRetrievalRounds: "最大检索轮次",
  maxActionRounds: "最大编排轮次",
  searchLimitPerSource: "每来源补充数量",
  chunkTargetTokens: "目标分块 Token",
  chunkMaxTokens: "最大分块 Token",
  chunkOverlapTokens: "重叠 Token",
  bm25Weight: "BM25 权重",
  vectorWeight: "向量权重",
  embeddingModel: "远程 Embedding 模型",
  embeddingBaseUrl: "远程 Embedding 地址",
  embeddingConfigured: "远程 Embedding 密钥",
  localEmbeddingModel: "本地 Embedding 模型",
  localEmbeddingBaseUrl: "本地 Embedding 地址",
  localEmbeddingProtocol: "本地 Embedding 协议",
  hybridGraphEnabled: "混合图谱检索",
  hybridGraphProjectId: "图谱项目 ID",
  backgroundJobWorkers: "后台任务工作线程",
  backgroundJobPendingLimit: "后台任务排队上限",
  streamWorkers: "流式任务工作线程",
  streamPendingLimit: "流式任务排队上限",
  semanticGraphWorkers: "图谱抽取工作线程",
  backend: "后端存储根目录",
  papers: "论文 PDF 目录",
  markdown: "Markdown 目录",
  paperDatabase: "论文数据库",
  vectorDatabase: "向量数据库",
};

const defaultDocumentSettings: DocumentSettings = {
  minimumLength: DEFAULT_MINIMUM_SPLIT_LENGTH,
  maximumSplitLength: DEFAULT_MAXIMUM_SPLIT_LENGTH,
};

function loadDocumentSettings(): DocumentSettings {
  if (typeof window === "undefined") return defaultDocumentSettings;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(WORKSPACE_SETTINGS_STORAGE_KEY) || "{}") as Partial<DocumentSettings>;
    return {
      minimumLength: Number(parsed.minimumLength) || DEFAULT_MINIMUM_SPLIT_LENGTH,
      maximumSplitLength: Number(parsed.maximumSplitLength) || DEFAULT_MAXIMUM_SPLIT_LENGTH,
    };
  } catch {
    return defaultDocumentSettings;
  }
}

async function responseError(response: Response, fallback: string) {
  const payload = await response.json().catch(() => ({}));
  return String(payload.detail || fallback);
}

function formatRuntimeValue(value: unknown): string {
  if (Array.isArray(value)) return value.join("，");
  if (typeof value === "boolean") return value ? "已启用" : "未启用";
  if (value === "" || value === null || value === undefined) return "未配置";
  return String(value);
}

export default function SettingsWorkspace() {
  const [activeTab, setActiveTab] = useState<SettingsTab>("model");
  const [documentSettings, setDocumentSettings] = useState<DocumentSettings>(loadDocumentSettings);
  const [savedDocumentSettings, setSavedDocumentSettings] = useState<DocumentSettings>(loadDocumentSettings);
  const [providers, setProviders] = useState<ModelProvider[]>(fallbackProviders);
  const [providerId, setProviderId] = useState("openai");
  const [protocol, setProtocol] = useState<ModelProtocol>("openai_compatible");
  const [modelName, setModelName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [maskedApiKey, setMaskedApiKey] = useState("");
  const [hasApiKey, setHasApiKey] = useState(false);
  const [secretStorage, setSecretStorage] = useState<ModelConfigResponse["secretStorage"]>("none");
  const [allowHeuristicFallback, setAllowHeuristicFallback] = useState(false);
  const [systemConstraint, setSystemConstraint] = useState("");
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfigResponse | null>(null);
  const [envConfig, setEnvConfig] = useState<EnvConfigResponse | null>(null);
  const [envChanges, setEnvChanges] = useState<Record<string, EnvValue>>({});
  const [activeEmbeddingSection, setActiveEmbeddingSection] = useState("remote_embedding");
  const [modelConfigured, setModelConfigured] = useState(false);
  const [modelDirty, setModelDirty] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("unconfigured");
  const [externalServiceStatus, setExternalServiceStatus] = useState<Record<ExternalServiceId, ConnectionStatus>>({
    tencent_translation: "unverified",
    mineru: "unverified",
  });
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);
  const [isModelMenuOpen, setIsModelMenuOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [isRefreshingRuntime, setIsRefreshingRuntime] = useState(false);
  const [isSavingEnv, setIsSavingEnv] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const selectedProvider = providers.find((provider) => provider.id === providerId) ?? fallbackProviders[2];
  const apiKeyRequired = selectedProvider.id === "custom"
    ? protocol === "anthropic" || protocol === "gemini"
    : selectedProvider.requiresApiKey;
  const documentDirty = JSON.stringify(documentSettings) !== JSON.stringify(savedDocumentSettings);
  const splitError = !Number.isFinite(documentSettings.minimumLength) || !Number.isFinite(documentSettings.maximumSplitLength)
    ? "切分长度必须是有效数字"
    : documentSettings.minimumLength < 500 || documentSettings.minimumLength > 5000
      ? "最小块长度必须在 500–5000 之间"
      : documentSettings.maximumSplitLength < 1000 || documentSettings.maximumSplitLength > 8000
        ? "最大块长度必须在 1000–8000 之间"
        : documentSettings.minimumLength > documentSettings.maximumSplitLength
          ? "最小长度不能大于最大切分长度"
          : "";
  const normalizedModelName = modelName.trim().toLocaleLowerCase();
  const visibleModels = useMemo(() => {
    if (!normalizedModelName) return discoveredModels;
    const exact = discoveredModels.find((model) => model.toLocaleLowerCase() === normalizedModelName);
    return exact ? discoveredModels : discoveredModels.filter((model) => model.toLocaleLowerCase().includes(normalizedModelName));
  }, [discoveredModels, normalizedModelName]);
  const envDirty = Object.keys(envChanges).length > 0;
  const hasUnsavedChanges = documentDirty || modelDirty || envDirty;

  function applyPublicConfig(payload: ModelConfigResponse) {
    setProviderId(payload.provider || "custom");
    setProtocol(payload.protocol || "openai_compatible");
    setModelName(payload.model || "");
    setBaseUrl(payload.baseUrl || "");
    setMaskedApiKey(payload.maskedApiKey || "");
    setHasApiKey(Boolean(payload.hasApiKey));
    setSecretStorage(payload.secretStorage || "none");
    setAllowHeuristicFallback(Boolean(payload.allowHeuristicFallback));
    setSystemConstraint(payload.systemConstraint || "");
    setModelConfigured(Boolean(payload.configured));
    setConnectionStatus(payload.configured ? "unverified" : "unconfigured");
    setModelDirty(false);
  }

  function markModelChanged() {
    setModelDirty(true);
    setConnectionStatus(modelConfigured ? "unverified" : "unconfigured");
    setMessage("");
    setError("");
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setIsLoading(true);
      try {
        const [configResponse, providersResponse, runtimeResponse, envResponse] = await Promise.all([
          fetch(buildApiUrl("/api/settings/model-config"), { cache: "no-store" }),
          fetch(buildApiUrl("/api/settings/model-providers"), { cache: "no-store" }),
          fetch(buildApiUrl("/api/settings/runtime-config"), { cache: "no-store" }),
          fetch(buildApiUrl("/api/settings/env-config"), { cache: "no-store" }),
        ]);
        if (!configResponse.ok) throw new Error(await responseError(configResponse, "加载模型配置失败"));
        if (!providersResponse.ok) throw new Error(await responseError(providersResponse, "加载模型供应商失败"));
        if (!runtimeResponse.ok) throw new Error(await responseError(runtimeResponse, "加载后端运行配置失败"));
        if (!envResponse.ok) throw new Error(await responseError(envResponse, "加载可编辑环境配置失败"));
        const config = await configResponse.json() as ModelConfigResponse;
        const catalog = await providersResponse.json() as { providers?: ModelProvider[] };
        const runtime = await runtimeResponse.json() as RuntimeConfigResponse;
        const environment = await envResponse.json() as EnvConfigResponse;
        if (!cancelled) {
          setProviders(catalog.providers?.length ? catalog.providers : fallbackProviders);
          applyPublicConfig(config);
          setRuntimeConfig(runtime);
          setEnvConfig(environment);
          if (!window.localStorage.getItem(WORKSPACE_SETTINGS_STORAGE_KEY)) {
            const fields = (environment.groups ?? []).flatMap((group) => group.fields);
            const minimumLength = Number(fields.find((field) => field.key === "SPLIT_MIN_LENGTH")?.value);
            const maximumSplitLength = Number(fields.find((field) => field.key === "SPLIT_MAX_LENGTH")?.value);
            if (minimumLength && maximumSplitLength) {
              const backendDefaults = { minimumLength, maximumSplitLength };
              setDocumentSettings(backendDefaults);
              setSavedDocumentSettings(backendDefaults);
            }
          }
        }
      } catch (loadError) {
        if (!cancelled) setError(loadError instanceof Error ? loadError.message : "加载设置失败");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    function warnBeforeUnload(event: BeforeUnloadEvent) {
      if (!hasUnsavedChanges) return;
      event.preventDefault();
    }
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [hasUnsavedChanges]);

  function selectProvider(nextId: string) {
    const provider = providers.find((item) => item.id === nextId) ?? fallbackProviders[2];
    setProviderId(provider.id);
    setProtocol(provider.protocol);
    setBaseUrl(provider.baseUrl);
    setModelName("");
    setApiKeyInput("");
    setMaskedApiKey("");
    setHasApiKey(false);
    setDiscoveredModels([]);
    setIsModelMenuOpen(false);
    markModelChanged();
  }

  function modelPayload() {
    return {
      provider: providerId,
      protocol,
      model: modelName.trim(),
      base_url: baseUrl.trim(),
      api_key: apiKeyInput,
      allow_heuristic_fallback: allowHeuristicFallback,
    };
  }

  function validateModelForm() {
    if (!baseUrl.trim()) return "请填写模型 Base URL";
    if (!modelName.trim()) return "请填写或选择模型";
    if (apiKeyRequired && !apiKeyInput.trim() && !hasApiKey) return "当前供应商需要 API Key";
    return "";
  }

  async function discoverModels() {
    setIsDiscovering(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/model-config/discover"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(modelPayload()),
      });
      if (!response.ok) throw new Error(await responseError(response, "发现模型失败"));
      const payload = await response.json() as { models?: string[] };
      const models = payload.models || [];
      setDiscoveredModels(models);
      setIsModelMenuOpen(models.length > 0);
      if (!modelName && models.length) setModelName(models[0]);
      setMessage(models.length ? `已发现 ${models.length} 个可用模型。` : "服务可访问，但没有返回模型列表。");
    } catch (discoverError) {
      setDiscoveredModels([]);
      setError(discoverError instanceof Error ? discoverError.message : "发现模型失败");
    } finally {
      setIsDiscovering(false);
    }
  }

  async function testConnection() {
    const validationError = validateModelForm();
    if (validationError) {
      setError(validationError);
      return;
    }
    setConnectionStatus("testing");
    setError("");
    setMessage("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/model-config/test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(modelPayload()),
      });
      if (!response.ok) throw new Error(await responseError(response, "模型连接测试失败"));
      const payload = await response.json() as { latencyMs?: number };
      setConnectionStatus("available");
      setMessage(`模型响应正常${payload.latencyMs ? `，耗时 ${payload.latencyMs} ms` : ""}。`);
    } catch (testError) {
      setConnectionStatus("error");
      setError(testError instanceof Error ? testError.message : "模型连接测试失败");
    }
  }

  async function saveModelConfig() {
    const validationError = validateModelForm();
    if (validationError) {
      setError(validationError);
      return;
    }
    setIsSaving(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/model-config"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(modelPayload()),
      });
      if (!response.ok) throw new Error(await responseError(response, "保存模型配置失败"));
      const payload = await response.json() as ModelConfigResponse;
      const wasAvailable = connectionStatus === "available";
      applyPublicConfig(payload);
      if (wasAvailable) setConnectionStatus("available");
      setApiKeyInput("");
      setMessage("模型配置已保存。建议在修改模型后重新测试连接。");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存模型配置失败");
    } finally {
      setIsSaving(false);
    }
  }

  async function saveDocumentSettings() {
    if (splitError) {
      setError(splitError);
      return;
    }
    setIsSavingEnv(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/env-config"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: { SPLIT_MIN_LENGTH: documentSettings.minimumLength, SPLIT_MAX_LENGTH: documentSettings.maximumSplitLength } }),
      });
      if (!response.ok) throw new Error(await responseError(response, "保存后端默认分块失败"));
      setEnvConfig(await response.json() as EnvConfigResponse);
      let current: Record<string, unknown> = {};
      try {
        current = JSON.parse(window.localStorage.getItem(WORKSPACE_SETTINGS_STORAGE_KEY) || "{}") as Record<string, unknown>;
      } catch {
        // 本地旧配置损坏时仅覆盖为当前有效设置。
      }
      window.localStorage.setItem(
        WORKSPACE_SETTINGS_STORAGE_KEY,
        JSON.stringify({ ...current, ...documentSettings }),
      );
      setSavedDocumentSettings(documentSettings);
      setMessage("分块设置已保存：下次重新解析立即使用，后端默认值将在重启后生效。");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存文档分块设置失败");
    } finally {
      setIsSavingEnv(false);
    }
  }

  function resetDocumentSettings() {
    setDocumentSettings(defaultDocumentSettings);
    setMessage("已恢复推荐值，保存后生效。");
    setError("");
  }

  async function clearModelConfig() {
    if (!window.confirm("确定清除后端保存的模型配置和密钥吗？环境变量提供的配置不会被删除。")) return;
    setIsSaving(true);
    setError("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/model-config"), { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response, "清除模型配置失败"));
      const payload = await response.json() as ModelConfigResponse;
      applyPublicConfig(payload);
      setApiKeyInput("");
      setMessage(payload.configured ? "已清除本地保存配置，当前仍使用环境变量配置。" : "模型配置和保存的密钥已清除。");
    } catch (clearError) {
      setError(clearError instanceof Error ? clearError.message : "清除模型配置失败");
    } finally {
      setIsSaving(false);
    }
  }

  async function refreshRuntimeConfig() {
    setIsRefreshingRuntime(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/runtime-config"), { cache: "no-store" });
      if (!response.ok) throw new Error(await responseError(response, "刷新后端运行配置失败"));
      setRuntimeConfig(await response.json() as RuntimeConfigResponse);
      setMessage("已刷新后端当前生效的运行配置。");
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : "刷新后端运行配置失败");
    } finally {
      setIsRefreshingRuntime(false);
    }
  }

  function updateEnvField(key: string, value: EnvValue) {
    setEnvChanges((current) => ({ ...current, [key]: value }));
    if (["TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENT_TRANSLATION_REGION"].includes(key)) {
      setExternalServiceStatus((current) => ({ ...current, tencent_translation: "unverified" }));
    }
    if (["MINERU_API_TOKEN", "MINERU_API_BASE"].includes(key)) {
      setExternalServiceStatus((current) => ({ ...current, mineru: "unverified" }));
    }
    setMessage("");
    setError("");
  }

  async function testExternalService(service: ExternalServiceId, visibleFields: EnvField[]) {
    const fieldValue = (key: string) => {
      if (Object.prototype.hasOwnProperty.call(envChanges, key)) return envChanges[key];
      return visibleFields.find((field) => field.key === key)?.value ?? "";
    };
    const payload = service === "tencent_translation"
      ? {
          service,
          secret_id: fieldValue("TENCENTCLOUD_SECRET_ID"),
          secret_key: fieldValue("TENCENTCLOUD_SECRET_KEY"),
          region: fieldValue("TENCENT_TRANSLATION_REGION"),
        }
      : {
          service,
          token: fieldValue("MINERU_API_TOKEN"),
          api_base: fieldValue("MINERU_API_BASE"),
        };
    setExternalServiceStatus((current) => ({ ...current, [service]: "testing" }));
    setMessage("");
    setError("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/external-service/test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const serviceName = service === "tencent_translation" ? "腾讯云翻译" : "MinerU";
      if (!response.ok) throw new Error(await responseError(response, `${serviceName}连接测试失败`));
      const result = await response.json() as { latencyMs?: number };
      setExternalServiceStatus((current) => ({ ...current, [service]: "available" }));
      setMessage(`${serviceName}连接正常${result.latencyMs !== undefined ? `，耗时 ${result.latencyMs} ms` : ""}。`);
    } catch (testError) {
      setExternalServiceStatus((current) => ({ ...current, [service]: "error" }));
      setError(testError instanceof Error ? testError.message : "外部服务连接测试失败");
    }
  }

  async function saveEnvConfig() {
    if (!envDirty) return;
    setIsSavingEnv(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(buildApiUrl("/api/settings/env-config"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: envChanges }),
      });
      if (!response.ok) throw new Error(await responseError(response, "保存环境配置失败"));
      setEnvConfig(await response.json() as EnvConfigResponse);
      setEnvChanges({});
      setMessage("已安全写入 backend/.env 并生成备份。请重启后端服务使新配置生效。");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存环境配置失败");
    } finally {
      setIsSavingEnv(false);
    }
  }

  function renderEnvSections(startIndex: string, allowedSectionIds: string[], primarySectionId = allowedSectionIds[0]) {
    const groups = envConfig?.groups ?? [];
    const sections = groups.flatMap((group) => (envSectionCatalog[group.id] ?? [])
      .filter((section) => allowedSectionIds.includes(section.id))
      .map((section) => ({ group, section })));

    return (
      <>
        {sections.map(({ group, section }, sectionIndex) => {
          const visibleFields = group.fields.filter((field) => section.keys.includes(field.key));
          const number = String(Number(startIndex) + sectionIndex).padStart(2, "0");
          const changedCount = visibleFields.filter((field) => Object.prototype.hasOwnProperty.call(envChanges, field.key)).length;
          const mineruToken = visibleFields.find((field) => field.key === "MINERU_API_TOKEN");
          const mineruFallback = visibleFields.find((field) => field.key === "MINERU_ENABLE_LOCAL_CLI_FALLBACK");
          const tokenValue = envChanges.MINERU_API_TOKEN;
          const fallbackValue = envChanges.MINERU_ENABLE_LOCAL_CLI_FALLBACK;
          const mineruTokenConfigured = tokenValue === null
            ? false
            : typeof tokenValue === "string" && tokenValue.trim()
              ? true
              : Boolean(mineruToken?.configured);
          const mineruFallbackEnabled = typeof fallbackValue === "boolean" ? fallbackValue : Boolean(mineruFallback?.value);
          const mineruNeedsSetup = section.id === "mineru"
            && !mineruTokenConfigured
            && !mineruFallbackEnabled;
          const secretConfigured = (key: string) => {
            const field = visibleFields.find((item) => item.key === key);
            const draft = envChanges[key];
            return draft === null ? false : typeof draft === "string" && draft.trim() ? true : Boolean(field?.configured);
          };
          const translationNeedsSetup = section.id === "translation"
            && (!secretConfigured("TENCENTCLOUD_SECRET_ID") || !secretConfigured("TENCENTCLOUD_SECRET_KEY"));
          const needsRequiredSetup = mineruNeedsSetup || translationNeedsSetup;
          const setupMessage = translationNeedsSetup ? "需要 SecretId 和 SecretKey" : "需要 Token 或本地降级";
          const externalService = section.id === "translation"
            ? "tencent_translation"
            : section.id === "mineru"
              ? "mineru"
              : null;
          const serviceStatus = externalService ? externalServiceStatus[externalService] : null;
          const serviceName = externalService === "tencent_translation" ? "腾讯云翻译" : "MinerU";
          const serviceTestDisabled = translationNeedsSetup || (externalService === "mineru" && !mineruTokenConfigured);
          return (
            <details key={section.id} className={`settings-card settings-env-section-card${needsRequiredSetup ? " needs-setup" : ""}`} open={section.id === primarySectionId || needsRequiredSetup}>
              <summary><div><span>{number}</span><strong>{section.label}</strong></div><small>{needsRequiredSetup ? setupMessage : changedCount ? `${changedCount} 项已修改` : `${visibleFields.length} 项配置`}</small></summary>
              <div className="settings-env-fields">
                {visibleFields.map((field) => {
                  const changed = Object.prototype.hasOwnProperty.call(envChanges, field.key);
                  const draftValue = changed ? envChanges[field.key] : field.value;
                  const inputId = `env-${field.key.toLowerCase()}`;
                  return (
                    <div key={field.key} className={`settings-env-field${changed ? " is-changed" : ""}`} title={field.key}>
                      <label htmlFor={inputId}><strong>{field.label}</strong>{changed ? <span>已修改</span> : null}</label>
                      <div className="settings-env-control">
                        {field.kind === "boolean" ? (
                          <select id={inputId} className="settings-input" value={String(draftValue)} onChange={(event) => updateEnvField(field.key, event.target.value === "true")}><option value="true">启用</option><option value="false">停用</option></select>
                        ) : field.kind === "choice" ? (
                          <select id={inputId} className="settings-input" value={String(draftValue ?? "")} onChange={(event) => updateEnvField(field.key, event.target.value)}>{(field.options ?? []).map((option) => <option key={option} value={option}>{option}</option>)}</select>
                        ) : (
                          <input id={inputId} type={field.kind === "secret" ? "password" : field.kind === "integer" || field.kind === "float" ? "number" : "text"} className={`settings-input${field.kind === "secret" || field.kind === "text" ? " settings-input-mono" : ""}`} value={field.kind === "secret" ? (typeof draftValue === "string" ? draftValue : "") : String(draftValue ?? "")} min={field.min} max={field.max} step={field.kind === "float" ? "0.01" : field.kind === "integer" ? "1" : undefined} autoComplete={field.kind === "secret" ? "new-password" : undefined} placeholder={field.kind === "secret" ? field.configured ? "已配置，留空保持原值" : "未配置" : undefined} onChange={(event) => updateEnvField(field.key, event.target.value)} />
                        )}
                        {field.kind === "secret" ? <button type="button" className="settings-env-clear" onClick={() => updateEnvField(field.key, null)}>清除</button> : null}
                      </div>
                      <p>{field.description}<span>{field.kind === "secret" ? draftValue === null ? " · 保存后清除" : field.configured ? " · 已配置" : " · 未配置" : field.source === "env_file" ? " · 来自 .env" : " · 当前默认值"}</span></p>
                    </div>
                  );
                })}
              </div>
              {externalService ? (
                <div className="settings-test-strip settings-env-test-strip">
                  <div>
                    <strong>{serviceStatus === "available" ? `${serviceName}连接可用` : serviceStatus === "testing" ? `正在测试${serviceName}` : serviceStatus === "error" ? `${serviceName}最近测试失败` : `${serviceName}尚未测试`}</strong>
                    <span>{externalService === "mineru" ? "执行只读任务查询，不上传文件或创建解析任务。" : "翻译一段最短测试文本，以验证签名、地域和接口权限。"}</span>
                  </div>
                  <button type="button" className="settings-btn settings-btn-secondary" disabled={serviceStatus === "testing" || serviceTestDisabled} onClick={() => void testExternalService(externalService, visibleFields)}>{serviceStatus === "testing" ? "测试中…" : "测试连接"}</button>
                </div>
              ) : null}
            </details>
          );
        })}
        <section className="settings-card settings-env-savebar"><span className={envDirty ? "settings-unsaved is-dirty" : "settings-unsaved"}>{envDirty ? `${Object.keys(envChanges).length} 项修改尚未保存` : "没有未保存的修改"}</span><div className="settings-card-actions"><button type="button" className="settings-btn settings-btn-ghost" disabled={!envDirty || isSavingEnv} onClick={() => setEnvChanges({})}>放弃修改</button><button type="button" className="settings-btn settings-btn-primary" disabled={!envDirty || isSavingEnv} onClick={() => void saveEnvConfig()}>{isSavingEnv ? "保存中…" : "保存到 .env"}</button></div></section>
      </>
    );
  }

  function renderEmbeddingSettings() {
    const options = (envConfig?.groups ?? []).flatMap((group) => (envSectionCatalog[group.id] ?? [])
      .filter((section) => ["remote_embedding", "local_embedding"].includes(section.id))
      .map((section) => ({ group, section })));
    const selected = options.find(({ section }) => section.id === activeEmbeddingSection) ?? options[0];
    const visibleFields = selected?.group.fields.filter((field) => selected.section.keys.includes(field.key)) ?? [];

    return (
      <>
        <section className="settings-card">
          <div className="settings-section-heading"><div><span>02</span><h2>向量模型</h2></div><select className="settings-input settings-section-select" value={selected?.section.id ?? ""} onChange={(event) => setActiveEmbeddingSection(event.target.value)} aria-label="选择向量服务类型">{options.map(({ section }) => <option key={section.id} value={section.id}>{section.label}</option>)}</select></div>
          <div className="settings-env-fields settings-env-fields-contained">
            {visibleFields.map((field) => {
              const changed = Object.prototype.hasOwnProperty.call(envChanges, field.key);
              const draftValue = changed ? envChanges[field.key] : field.value;
              const inputId = `env-${field.key.toLowerCase()}`;
              return (
                <div key={field.key} className={`settings-env-field${changed ? " is-changed" : ""}`} title={field.key}>
                  <label htmlFor={inputId}><strong>{field.label}</strong>{changed ? <span>已修改</span> : null}</label>
                  <div className="settings-env-control">
                    {field.kind === "choice" ? (
                      <select id={inputId} className="settings-input" value={String(draftValue ?? "")} onChange={(event) => updateEnvField(field.key, event.target.value)}>{(field.options ?? []).map((option) => <option key={option} value={option}>{option}</option>)}</select>
                    ) : (
                      <input id={inputId} type={field.kind === "secret" ? "password" : "text"} className="settings-input settings-input-mono" value={field.kind === "secret" ? (typeof draftValue === "string" ? draftValue : "") : String(draftValue ?? "")} autoComplete={field.kind === "secret" ? "new-password" : undefined} placeholder={field.kind === "secret" ? field.configured ? "已配置，留空保持原值" : "未配置" : undefined} onChange={(event) => updateEnvField(field.key, event.target.value)} />
                    )}
                    {field.kind === "secret" ? <button type="button" className="settings-env-clear" onClick={() => updateEnvField(field.key, null)}>清除</button> : null}
                  </div>
                  <p>{field.description}<span>{field.kind === "secret" ? draftValue === null ? " · 保存后清除" : field.configured ? " · 已配置" : " · 未配置" : field.source === "env_file" ? " · 来自 .env" : " · 当前默认值"}</span></p>
                </div>
              );
            })}
          </div>
          <div className="settings-card-actions settings-card-actions-end"><span className={envDirty ? "settings-unsaved is-dirty" : "settings-unsaved"}>{envDirty ? `${Object.keys(envChanges).length} 项修改尚未保存` : "没有未保存的修改"}</span><button type="button" className="settings-btn settings-btn-ghost" disabled={!envDirty || isSavingEnv} onClick={() => setEnvChanges({})}>放弃修改</button><button type="button" className="settings-btn settings-btn-primary" disabled={!envDirty || isSavingEnv} onClick={() => void saveEnvConfig()}>{isSavingEnv ? "保存中…" : "保存到 .env"}</button></div>
        </section>
      </>
    );
  }

  const statusLabel = connectionStatus === "available"
    ? "模型服务可用"
    : connectionStatus === "testing"
      ? "正在验证连接"
      : connectionStatus === "error"
        ? "最近验证失败"
        : modelConfigured
          ? "配置完整，尚未验证"
          : "尚未配置可用模型";

  return (
    <main className="dataset-browser-page">
      <section className="dataset-browser-panel settings-panel settings-shell">
        <div className="settings-layout">
          <nav className="settings-side-nav" aria-label="设置分区">
            {tabItems.map((item) => (
              <button key={item.id} type="button" className={activeTab === item.id ? "is-active" : ""} onClick={() => { setActiveTab(item.id); setMessage(""); setError(""); }}>
                <strong>{item.label}</strong><span>{item.description}</span>
              </button>
            ))}
          </nav>

          <div className="settings-content">
            {isLoading ? <div className="settings-card settings-loading">正在读取配置…</div> : null}

            {!isLoading && activeTab === "model" ? (
              <>
                <section className="settings-card">
                  <div className="settings-section-heading"><div><span>01</span><h2>模型服务</h2></div><p>配置连接、选择模型并验证服务可用性。</p></div>
                  <div className="settings-form-grid">
                    <label className="settings-row"><span className="settings-row-label">模型供应商</span><select className="settings-input" value={providerId} onChange={(event) => selectProvider(event.target.value)}>{providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select></label>
                    <div className="settings-row"><span className="settings-row-label">接口协议</span>{providerId === "custom" ? <select className="settings-input" value={protocol} onChange={(event) => { setProtocol(event.target.value as ModelProtocol); markModelChanged(); }}>{Object.entries(protocolLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select> : <div className="settings-readonly-value">{protocolLabels[protocol]}</div>}</div>
                    <label className="settings-row settings-span-2"><span className="settings-row-label">Base URL</span><input className="settings-input settings-input-mono" value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); markModelChanged(); }} placeholder={selectedProvider.baseUrl || "请输入模型服务根地址"} /><span className="settings-hint">填写 API 根地址，不要重复添加具体聊天端点。</span></label>
                    <label className="settings-row settings-span-2"><span className="settings-row-label">API Key{apiKeyRequired ? "（必填）" : "（可选）"}</span><input type="password" className="settings-input settings-input-mono" value={apiKeyInput} onChange={(event) => { setApiKeyInput(event.target.value); markModelChanged(); }} disabled={providerId === "ollama"} autoComplete="new-password" placeholder={maskedApiKey ? "留空则继续使用当前密钥" : apiKeyRequired ? "请输入供应商 API Key" : "本地服务通常可以留空"} />{maskedApiKey ? <span className="settings-secret-preview">当前密钥：{maskedApiKey}</span> : null}</label>
                  </div>
                  <div className="settings-subsection-title"><strong>模型选择</strong><span>发现可用模型，或直接填写模型 ID</span></div>
                  <div className="settings-row">
                    <span className="settings-row-label">模型 ID</span>
                    <div className="settings-model-picker"><div className="settings-model-combobox" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setIsModelMenuOpen(false); }}><input className="settings-input settings-input-mono settings-model-input" value={modelName} onChange={(event) => { setModelName(event.target.value); setIsModelMenuOpen(discoveredModels.length > 0); markModelChanged(); }} onFocus={() => setIsModelMenuOpen(discoveredModels.length > 0)} placeholder={selectedProvider.modelPlaceholder} role="combobox" aria-controls="available-model-options" aria-expanded={isModelMenuOpen} />{discoveredModels.length ? <button type="button" className="settings-model-toggle" onClick={() => setIsModelMenuOpen((current) => !current)}>⌄</button> : null}{isModelMenuOpen ? <div id="available-model-options" className="settings-model-menu" role="listbox">{visibleModels.length ? visibleModels.map((model) => <button key={model} type="button" className={`settings-model-option${model === modelName ? " is-selected" : ""}`} onClick={() => { setModelName(model); setIsModelMenuOpen(false); markModelChanged(); }}>{model}</button>) : <span className="settings-model-empty">没有匹配项，仍可直接填写模型 ID。</span>}</div> : null}</div><button type="button" className="settings-btn settings-btn-ghost" onClick={() => void discoverModels()} disabled={isDiscovering || !baseUrl.trim()}>{isDiscovering ? "发现中…" : "发现模型"}</button></div>
                  </div>
                  <div className="settings-test-strip">
                    <div><strong>{statusLabel}</strong><span>{connectionStatus === "available" ? "服务访问、身份验证与模型响应均正常。" : "连接测试会发送一次只要求回复 OK 的最小请求。"}</span></div>
                    <div className="settings-card-actions"><span className={modelDirty ? "settings-unsaved is-dirty" : "settings-unsaved"}>{modelDirty ? "有未保存修改" : "配置已保存并应用"}</span><button type="button" className="settings-btn settings-btn-secondary" onClick={() => void testConnection()} disabled={connectionStatus === "testing"}>{connectionStatus === "testing" ? "测试中…" : "测试连接"}</button><button type="button" className="settings-btn settings-btn-primary" onClick={() => void saveModelConfig()} disabled={isSaving}>{isSaving ? "保存中…" : "保存并应用"}</button></div>
                  </div>
                </section>
                {renderEmbeddingSettings()}
              </>
            ) : null}

            {!isLoading && activeTab === "document" ? (
              <>
                <section className="settings-card">
                  <div className="settings-section-heading"><div><span>01</span><h2>全文分块</h2></div><p>以下参数会在数据集浏览页重新解析 PDF 时传给后端。</p></div>
                  <div className="settings-row"><span className="settings-row-label">最小块长度</span><div className="settings-range-row"><input type="range" min={500} max={5000} step={100} className="settings-slider" value={documentSettings.minimumLength} onChange={(event) => setDocumentSettings((current) => ({ ...current, minimumLength: Number(event.target.value) }))} /><input type="number" min={500} max={5000} step={100} className="settings-input settings-input-num" value={documentSettings.minimumLength} onChange={(event) => setDocumentSettings((current) => ({ ...current, minimumLength: Number(event.target.value) }))} /></div><span className="settings-hint">推荐值：{DEFAULT_MINIMUM_SPLIT_LENGTH} 字符</span></div>
                  <div className="settings-row"><span className="settings-row-label">最大块长度</span><div className="settings-range-row"><input type="range" min={1000} max={8000} step={100} className="settings-slider" value={documentSettings.maximumSplitLength} onChange={(event) => setDocumentSettings((current) => ({ ...current, maximumSplitLength: Number(event.target.value) }))} /><input type="number" min={1000} max={8000} step={100} className="settings-input settings-input-num" value={documentSettings.maximumSplitLength} onChange={(event) => setDocumentSettings((current) => ({ ...current, maximumSplitLength: Number(event.target.value) }))} /></div><span className="settings-hint">推荐值：{DEFAULT_MAXIMUM_SPLIT_LENGTH} 字符</span></div>
                  {splitError ? <div className="settings-inline-error">{splitError}</div> : null}
                  <div className="settings-card-actions settings-card-actions-end"><span className={documentDirty ? "settings-unsaved is-dirty" : "settings-unsaved"}>{documentDirty ? "有未保存修改" : "配置已保存"}</span><button type="button" className="settings-btn settings-btn-ghost" onClick={resetDocumentSettings}>恢复推荐值</button><button type="button" className="settings-btn settings-btn-primary" onClick={() => void saveDocumentSettings()} disabled={Boolean(splitError) || isSavingEnv}>{isSavingEnv ? "保存中…" : "保存分块设置"}</button></div>
                </section>
                {renderEnvSections("02", ["mineru"])}
              </>
            ) : null}

            {!isLoading && activeTab === "research" ? renderEnvSections("01", ["translation", "literature", "research_budget", "orchestration", "chunking", "ranking", "graph"], "translation") : null}

            {!isLoading && activeTab === "environment" ? (
              <>
                {renderEnvSections("01", ["http", "workers"])}

                <details className="settings-card settings-runtime-details">
                  <summary><span><strong>查看当前生效值</strong><small>服务、任务并发和数据位置诊断</small></span><button type="button" className="settings-btn settings-btn-secondary" disabled={isRefreshingRuntime} onClick={(event) => { event.preventDefault(); void refreshRuntimeConfig(); }}>{isRefreshingRuntime ? "刷新中…" : "刷新"}</button></summary>
                  <div className="settings-config-columns settings-config-columns-3">
                    <div><h3>服务</h3><dl className="settings-config-list">{Object.entries(runtimeConfig?.server ?? {}).map(([key, value]) => <div key={key}><dt>{runtimeLabels[key] || key}</dt><dd>{formatRuntimeValue(value)}</dd></div>)}</dl></div>
                    <div><h3>任务执行</h3><dl className="settings-config-list">{Object.entries(runtimeConfig?.workers ?? {}).map(([key, value]) => <div key={key}><dt>{runtimeLabels[key] || key}</dt><dd>{formatRuntimeValue(value)}</dd></div>)}</dl></div>
                    <div><h3>数据位置</h3><dl className="settings-config-list settings-config-paths">{Object.entries(runtimeConfig?.storage ?? {}).map(([key, value]) => <div key={key}><dt>{runtimeLabels[key] || key}</dt><dd title={formatRuntimeValue(value)}>{formatRuntimeValue(value)}</dd></div>)}</dl></div>
                  </div>
                </details>
              </>
            ) : null}

            {!isLoading && activeTab === "advanced" ? (
              <>
                <section className="settings-card">
                  <div className="settings-section-heading"><div><span>01</span><h2>模型异常处理</h2></div><p>决定结构化规划失败时系统是否可以采用启发式规则继续。</p></div>
                  <div className="settings-radios">
                    <label className={`settings-radio${!allowHeuristicFallback ? " is-checked" : ""}`}><input type="radio" checked={!allowHeuristicFallback} onChange={() => { setAllowHeuristicFallback(false); markModelChanged(); }} /><span><strong>立即失败并提示用户（推荐）</strong><small>避免在模型输出无效时产生质量不可控的结果。</small></span></label>
                    <label className={`settings-radio${allowHeuristicFallback ? " is-checked" : ""}`}><input type="radio" checked={allowHeuristicFallback} onChange={() => { setAllowHeuristicFallback(true); markModelChanged(); }} /><span><strong>允许生成降级结果</strong><small>任务可继续执行，但结果会明确标记为启发式降级。</small></span></label>
                  </div>
                  <div className="settings-card-actions settings-card-actions-end"><span className={modelDirty ? "settings-unsaved is-dirty" : "settings-unsaved"}>{modelDirty ? "策略尚未保存" : "策略已保存"}</span><button type="button" className="settings-btn settings-btn-primary" onClick={() => void saveModelConfig()} disabled={isSaving}>{isSaving ? "保存中…" : "保存异常处理策略"}</button></div>
                </section>
                <section className="settings-card">
                  <div className="settings-section-heading"><div><span>02</span><h2>安全与存储</h2></div><p>密钥永远不会通过设置接口返回完整内容。</p></div>
                  <dl className="settings-facts"><div><dt>密钥来源</dt><dd>{secretStorage === "windows_dpapi" ? "Windows DPAPI 加密存储" : secretStorage === "environment" ? "后端环境变量" : secretStorage === "legacy_plaintext" ? "旧版明文配置（保存一次即可迁移）" : "未保存"}</dd></div><div><dt>后端配置状态</dt><dd>{modelConfigured ? "配置完整" : "配置不完整"}</dd></div><div><dt>浏览器存储</dt><dd>仅保存文档分块长度，不保存密钥</dd></div></dl>
                  <details className="settings-details"><summary>查看内置防泄露约束</summary><pre>{systemConstraint || "后端暂未返回安全约束。"}</pre></details>
                </section>
                <section className="settings-card settings-danger-card">
                  <div><h2>清除后端模型配置</h2><p>删除后端保存的模型地址、模型 ID 和加密密钥。环境变量不会被修改。</p></div><button type="button" className="settings-btn settings-btn-danger" onClick={() => void clearModelConfig()} disabled={isSaving}>清除配置</button>
                </section>
              </>
            ) : null}

            {message ? <div className="settings-feedback settings-feedback-success">{message}</div> : null}
            {error ? <div className="settings-feedback settings-feedback-error">{error}</div> : null}

          </div>
        </div>
      </section>
    </main>
  );
}
