"""以字段白名单安全读写 backend/.env，且不向浏览器返回密钥原文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import tempfile
from threading import Lock
from typing import Any, Literal

from app.core.config import BACKEND_DIR, settings


FieldKind = Literal["text", "integer", "float", "boolean", "secret", "choice"]


@dataclass(frozen=True)
class EnvFieldSpec:
    key: str
    label: str
    group: str
    kind: FieldKind
    runtime_attr: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()


GROUPS = (
    ("integrations", "外部服务", "配置检索、解析、翻译和向量服务的连接参数。密钥只写不读。"),
    ("research", "研究与检索", "控制候选论文、证据预算、分块和编排上限。"),
    ("documents", "文档处理", "配置 MinerU 解析服务及默认分块范围。"),
    ("server", "服务与并发", "配置服务监听、跨域、超时和后台任务并发。"),
)


FIELDS = (
    EnvFieldSpec("NCBI_EMAIL", "NCBI 邮箱", "integrations", "text", "ncbi_email", "PubMed 请求标识邮箱。"),
    EnvFieldSpec("NCBI_API_KEY", "NCBI API Key", "integrations", "secret", "ncbi_api_key", "提高 PubMed API 请求配额。"),
    EnvFieldSpec("IEEE_API_KEY", "IEEE API Key", "integrations", "secret", "ieee_api_key", "用于 IEEE Xplore 检索。"),
    EnvFieldSpec("SEMANTIC_SCHOLAR_API_KEY", "Semantic Scholar API Key", "integrations", "secret", "semantic_scholar_api_key", "用于 Semantic Scholar 检索。"),
    EnvFieldSpec("MINERU_API_TOKEN", "MinerU Token", "documents", "secret", "mineru_api_token", "用于 MinerU 云端文档解析。"),
    EnvFieldSpec("TENCENTCLOUD_SECRET_ID", "腾讯云 SecretId", "integrations", "secret", "tencent_translation_secret_id", "用于腾讯云翻译。"),
    EnvFieldSpec("TENCENTCLOUD_SECRET_KEY", "腾讯云 SecretKey", "integrations", "secret", "tencent_translation_secret_key", "用于腾讯云翻译。"),
    EnvFieldSpec("TENCENT_TRANSLATION_REGION", "腾讯云地域", "integrations", "text", "tencent_translation_region", "例如 ap-guangzhou。"),
    EnvFieldSpec("RAG_EMBEDDING_API_KEY", "远程 Embedding API Key", "integrations", "secret", "rag_embedding_api_key", "用于 OpenAI 兼容的向量服务。"),
    EnvFieldSpec("RAG_EMBEDDING_BASE_URL", "远程 Embedding 地址", "integrations", "text", "rag_embedding_base_url", "OpenAI 兼容 API 根地址。"),
    EnvFieldSpec("RAG_EMBEDDING_MODEL", "远程 Embedding 模型", "integrations", "text", "rag_embedding_model", "远程向量模型 ID。"),
    EnvFieldSpec("RAG_LOCAL_EMBEDDING_BASE_URL", "本地 Embedding 地址", "integrations", "text", "rag_local_embedding_base_url", "Ollama 或 OpenAI 兼容服务地址。"),
    EnvFieldSpec("RAG_LOCAL_EMBEDDING_MODEL", "本地 Embedding 模型", "integrations", "text", "rag_local_embedding_model", "留空表示不指定本地模型。"),
    EnvFieldSpec("RAG_LOCAL_EMBEDDING_PROTOCOL", "本地 Embedding 协议", "integrations", "choice", "rag_local_embedding_protocol", "本地向量服务协议。", options=("ollama", "openai_compatible")),
    EnvFieldSpec("RAG_LOCAL_EMBEDDING_API_KEY", "本地 Embedding API Key", "integrations", "secret", "rag_local_embedding_api_key", "本地服务无需鉴权时可留空。"),
    EnvFieldSpec("RESEARCH_AGENT_MAX_PAPERS", "最大候选论文数", "research", "integer", "research_agent_max_papers", "单次研究可纳入的候选论文上限。", 1, 1000),
    EnvFieldSpec("RESEARCH_AGENT_MAX_SOURCES", "最大证据来源数", "research", "integer", "research_agent_max_sources", "单次回答最多使用的证据来源。", 1, 50),
    EnvFieldSpec("RESEARCH_AGENT_MAX_CONTEXT_CHARS", "最大上下文字符数", "research", "integer", "research_agent_max_context_chars", "发送给模型的证据上下文预算。", 1000, 200000),
    EnvFieldSpec("RESEARCH_AGENT_REQUEST_TIMEOUT", "研究请求超时（秒）", "research", "integer", "research_agent_request_timeout", "研究流程的模型请求超时。", 5, 600),
    EnvFieldSpec("ORCHESTRATOR_MIN_EVIDENCE", "最少证据数", "research", "integer", "orchestrator_min_evidence", "满足回答要求的最少证据数量。", 1, 20),
    EnvFieldSpec("ORCHESTRATOR_MAX_RETRIEVAL_ROUNDS", "最大检索轮次", "research", "integer", "orchestrator_max_retrieval_rounds", "编排器允许的补充检索轮次。", 1, 3),
    EnvFieldSpec("ORCHESTRATOR_MAX_ACTION_ROUNDS", "最大动作轮次", "research", "integer", "orchestrator_max_action_rounds", "单次任务的工具动作上限。", 1, 20),
    EnvFieldSpec("ORCHESTRATOR_SEARCH_LIMIT_PER_SOURCE", "单来源检索上限", "research", "integer", "orchestrator_search_limit_per_source", "每个检索源返回的候选数。", 1, 20),
    EnvFieldSpec("RAG_CHUNK_TARGET_TOKENS", "目标分块 Token", "research", "integer", "rag_chunk_target_tokens", "向量索引的目标分块长度。", 50, 5000),
    EnvFieldSpec("RAG_CHUNK_MAX_TOKENS", "最大分块 Token", "research", "integer", "rag_chunk_max_tokens", "向量索引的最大分块长度。", 50, 10000),
    EnvFieldSpec("RAG_CHUNK_OVERLAP_TOKENS", "分块重叠 Token", "research", "integer", "rag_chunk_overlap_tokens", "相邻向量分块的重叠长度。", 0, 2000),
    EnvFieldSpec("RAG_BM25_WEIGHT", "BM25 权重", "research", "float", "rag_bm25_weight", "混合检索的关键词权重。", 0, 1),
    EnvFieldSpec("RAG_VECTOR_WEIGHT", "向量权重", "research", "float", "rag_vector_weight", "混合检索的向量权重。", 0, 1),
    EnvFieldSpec("HYBRID_GRAPH_ENABLED", "启用知识图谱混合检索", "research", "boolean", "hybrid_graph_enabled", "是否在检索中融合项目知识图谱。"),
    EnvFieldSpec("HYBRID_GRAPH_PROJECT_ID", "知识图谱项目 ID", "research", "text", "hybrid_graph_project_id", "混合检索使用的项目标识。"),
    EnvFieldSpec("MINERU_API_BASE", "MinerU API 地址", "documents", "text", "mineru_api_base", "MinerU 云端 API 根地址。"),
    EnvFieldSpec("MINERU_MODEL_VERSION", "MinerU 模型版本", "documents", "text", "mineru_model_version", "MinerU 解析模型版本。"),
    EnvFieldSpec("MINERU_ENABLE_LOCAL_CLI_FALLBACK", "允许本地 CLI 降级", "documents", "boolean", "mineru_enable_local_cli_fallback", "云端失败后是否尝试本地 MinerU CLI。"),
    EnvFieldSpec("MINERU_REQUEST_TIMEOUT_SECONDS", "MinerU 请求超时（秒）", "documents", "integer", "mineru_request_timeout_seconds", "单次 MinerU HTTP 请求超时。", 5, 600),
    EnvFieldSpec("MINERU_CLOUD_TIMEOUT_SECONDS", "MinerU 总超时（秒）", "documents", "integer", "mineru_cloud_timeout_seconds", "一次云端解析任务的总等待上限。", 30, 7200),
    EnvFieldSpec("SPLIT_MIN_LENGTH", "默认最小分块字符数", "documents", "integer", "split_min_length", "文档重新解析时的默认最小分块。", 100, 20000),
    EnvFieldSpec("SPLIT_MAX_LENGTH", "默认最大分块字符数", "documents", "integer", "split_max_length", "文档重新解析时的默认最大分块。", 100, 50000),
    EnvFieldSpec("HOST", "监听地址", "server", "text", "host", "后端监听的主机地址。"),
    EnvFieldSpec("PORT", "监听端口", "server", "integer", "port", "后端 HTTP 端口。", 1, 65535),
    EnvFieldSpec("CORS_ORIGINS", "允许的前端来源", "server", "text", "cors_origins", "多个来源使用英文逗号分隔。"),
    EnvFieldSpec("REQUEST_TIMEOUT", "外部请求超时（秒）", "server", "integer", "request_timeout", "通用外部 HTTP 请求超时。", 1, 600),
    EnvFieldSpec("LOG_LEVEL", "日志级别", "server", "choice", "log_level", "后端日志输出级别。", options=("DEBUG", "INFO", "WARNING", "ERROR")),
    EnvFieldSpec("BACKGROUND_JOB_MAX_WORKERS", "后台任务并发数", "server", "integer", "background_job_max_workers", "后台任务执行线程数。", 1, 32),
    EnvFieldSpec("BACKGROUND_JOB_MAX_PENDING_TASKS", "后台任务等待上限", "server", "integer", "background_job_max_pending_tasks", "允许排队的后台任务数。", 0, 1000),
    EnvFieldSpec("STREAM_MAX_WORKERS", "流式任务并发数", "server", "integer", "stream_max_workers", "流式任务执行线程数。", 1, 32),
    EnvFieldSpec("STREAM_MAX_PENDING_TASKS", "流式任务等待上限", "server", "integer", "stream_max_pending_tasks", "允许排队的流式任务数。", 0, 1000),
    EnvFieldSpec("SEMANTIC_GRAPH_MAX_WORKERS", "语义图谱并发数", "server", "integer", "semantic_graph_max_workers", "语义图谱构建线程数。", 1, 16),
)


class EnvConfigStore:
    """仅允许更新明确接入运行流程的字段，并保留 .env 原有注释和未知项。"""

    _lock = Lock()

    def __init__(self, env_path: Path | None = None) -> None:
        self.env_path = env_path or BACKEND_DIR / ".env"
        self.specs = {field.key: field for field in FIELDS}

    def get_public_config(self) -> dict[str, Any]:
        file_values = self._read_values()
        grouped: dict[str, list[dict[str, Any]]] = {group[0]: [] for group in GROUPS}
        for spec in FIELDS:
            runtime_value = getattr(settings, spec.runtime_attr)
            configured = bool(str(file_values.get(spec.key, runtime_value) or "").strip())
            item: dict[str, Any] = {
                "key": spec.key,
                "label": spec.label,
                "kind": spec.kind,
                "configured": configured,
                "source": "env_file" if spec.key in file_values else "runtime_default",
                "description": spec.description,
            }
            if spec.kind != "secret":
                value = file_values.get(spec.key, runtime_value)
                if isinstance(value, list):
                    value = ",".join(str(part) for part in value)
                item["value"] = self._coerce_for_output(spec, value)
            if spec.minimum is not None:
                item["min"] = spec.minimum
            if spec.maximum is not None:
                item["max"] = spec.maximum
            if spec.options:
                item["options"] = list(spec.options)
            grouped[spec.group].append(item)
        return {
            "restartRequired": True,
            "groups": [
                {"id": group_id, "label": label, "description": description, "fields": grouped[group_id]}
                for group_id, label, description in GROUPS
            ],
        }

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(values) - set(self.specs))
        if unknown:
            raise ValueError(f"不允许修改配置项：{', '.join(unknown)}")
        normalized: dict[str, str | None] = {}
        for key, value in values.items():
            spec = self.specs[key]
            if spec.kind == "secret" and value == "":
                continue
            normalized[key] = None if value is None else self._validate(spec, value)
        if normalized:
            self._atomic_update(normalized)
        return {**self.get_public_config(), "backupCreated": bool(normalized)}

    def _read_values(self) -> dict[str, str]:
        if not self.env_path.exists():
            return {}
        result: dict[str, str] = {}
        for line in self.env_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            result[key.strip()] = self._decode(value.strip())
        return result

    @staticmethod
    def _decode(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    @staticmethod
    def _coerce_for_output(spec: EnvFieldSpec, value: Any) -> Any:
        if spec.kind == "integer":
            return int(value)
        if spec.kind == "float":
            return float(value)
        if spec.kind == "boolean":
            return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
        return str(value)

    def _validate(self, spec: EnvFieldSpec, value: Any) -> str:
        if spec.kind == "integer":
            try:
                parsed: int | float = int(str(value))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{spec.label} 必须是整数") from error
        elif spec.kind == "float":
            try:
                parsed = float(str(value))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{spec.label} 必须是数字") from error
        elif spec.kind == "boolean":
            if isinstance(value, bool):
                return "true" if value else "false"
            normalized = str(value).strip().lower()
            if normalized not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError(f"{spec.label} 必须是布尔值")
            return "true" if normalized in {"true", "1", "yes", "on"} else "false"
        else:
            text = str(value).strip()
            if "\n" in text or "\r" in text or '"' in text:
                raise ValueError(f"{spec.label} 包含不支持的字符")
            if spec.kind == "choice" and text not in spec.options:
                raise ValueError(f"{spec.label} 必须是：{', '.join(spec.options)}")
            return text
        if spec.minimum is not None and parsed < spec.minimum:
            raise ValueError(f"{spec.label} 不能小于 {spec.minimum:g}")
        if spec.maximum is not None and parsed > spec.maximum:
            raise ValueError(f"{spec.label} 不能大于 {spec.maximum:g}")
        return str(parsed)

    @staticmethod
    def _encode(value: str) -> str:
        return f'"{value}"' if not value or any(character.isspace() for character in value) or "#" in value else value

    def _atomic_update(self, updates: dict[str, str | None]) -> None:
        with self._lock:
            original = self.env_path.read_text(encoding="utf-8") if self.env_path.exists() else ""
            output: list[str] = []
            handled: set[str] = set()
            for line in original.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in updates:
                        if key not in handled and updates[key] is not None:
                            output.append(f"{key}={self._encode(updates[key] or '')}")
                        handled.add(key)
                        continue
                output.append(line)
            for key, value in updates.items():
                if key not in handled and value is not None:
                    output.append(f"{key}={self._encode(value)}")

            self.env_path.parent.mkdir(parents=True, exist_ok=True)
            if self.env_path.exists():
                shutil.copy2(self.env_path, self.env_path.with_name(".env.bak"))
            descriptor, temp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=self.env_path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write("\n".join(output).rstrip() + "\n")
                os.replace(temp_name, self.env_path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)


__all__ = ["EnvConfigStore", "FIELDS", "GROUPS"]
