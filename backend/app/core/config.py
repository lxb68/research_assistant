"""从环境变量与 backend/.env 汇总后端运行配置。"""

from pathlib import Path
import os

from app.services.runtime_settings import RuntimeConfigManager, RuntimeSettingsProxy


BACKEND_DIR = Path(__file__).resolve().parents[2]


def load_env_file() -> None:
    """读取 backend/.env，并将简单的 KEY=VALUE 配置加载到环境变量。"""
    env_path = BACKEND_DIR / ".env"

    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()

        if not text or text.startswith("#") or "=" not in text:
            continue

        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


load_env_file()


class Settings:
    """后端运行配置，集中管理端口、跨域和第三方 API Key。"""

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "4000"))
    cors_origin = os.getenv("CORS_ORIGIN", "http://localhost:3000")
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    ]
    # 第三方 API Key 配置
    ncbi_api_key = os.getenv("NCBI_API_KEY", "")
    ncbi_email = os.getenv("NCBI_EMAIL", "")
    ieee_api_key = os.getenv("IEEE_API_KEY", "")
    semantic_scholar_api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    tencent_translation_secret_id = (
        os.getenv("TENCENT_TRANSLATION_SECRET_ID")
        or os.getenv("TENCENTCLOUD_SECRET_ID")
        or ""
    )
    tencent_translation_secret_key = (
        os.getenv("TENCENT_TRANSLATION_SECRET_KEY")
        or os.getenv("TENCENTCLOUD_SECRET_KEY")
        or ""
    )
    tencent_translation_region = (
        os.getenv("TENCENT_TRANSLATION_REGION")
        or os.getenv("TENCENTCLOUD_REGION")
        or "ap-guangzhou"
    )
    llm_translation_api_key = os.getenv("LLM_TRANSLATION_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    llm_translation_base_url = (
        os.getenv("LLM_TRANSLATION_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    llm_translation_model = (
        os.getenv("LLM_TRANSLATION_MODEL")
        or os.getenv("OPENAI_MODEL")
        or "gpt-4o-mini"
    )
    request_timeout = int(os.getenv("REQUEST_TIMEOUT", "15"))
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    # 流式接口共享固定执行池，避免按请求创建线程。
    stream_max_workers = max(1, int(os.getenv("STREAM_MAX_WORKERS", "4")))
    stream_max_pending_tasks = max(0, int(os.getenv("STREAM_MAX_PENDING_TASKS", "20")))
    stream_event_queue_size = max(8, int(os.getenv("STREAM_EVENT_QUEUE_SIZE", "256")))
    stream_heartbeat_seconds = max(1.0, float(os.getenv("STREAM_HEARTBEAT_SECONDS", "20")))
    stream_task_retention_seconds = max(60.0, float(os.getenv("STREAM_TASK_RETENTION_SECONDS", "86400")))
    stream_max_retained_tasks = max(1, int(os.getenv("STREAM_MAX_RETAINED_TASKS", "200")))
    background_job_db = os.getenv("BACKGROUND_JOB_DB") or str(
        BACKEND_DIR / "storage" / "metadata" / "background_jobs.sqlite3",
    )
    background_job_max_workers = max(1, int(os.getenv("BACKGROUND_JOB_MAX_WORKERS", "4")))
    background_job_max_pending_tasks = max(0, int(os.getenv("BACKGROUND_JOB_MAX_PENDING_TASKS", "20")))
    background_job_stale_seconds = max(5, int(os.getenv("BACKGROUND_JOB_STALE_SECONDS", "300")))
    background_job_heartbeat_seconds = max(1, int(os.getenv("BACKGROUND_JOB_HEARTBEAT_SECONDS", "10")))
    background_job_cleanup_interval_seconds = max(
        1,
        int(os.getenv("BACKGROUND_JOB_CLEANUP_INTERVAL_SECONDS", "60")),
    )
    background_job_ttl_hours = max(1, int(os.getenv("BACKGROUND_JOB_TTL_HOURS", "168")))
    background_job_max_history = max(1, int(os.getenv("BACKGROUND_JOB_MAX_HISTORY", "1000")))
    background_job_max_events_per_job = max(10, int(os.getenv("BACKGROUND_JOB_MAX_EVENTS_PER_JOB", "500")))
    literature_map_db = os.getenv("LITERATURE_MAP_DB") or str(
        BACKEND_DIR / "storage" / "metadata" / "literature_map.sqlite3",
    )
    literature_map_extractor_version = (
        os.getenv("LITERATURE_MAP_EXTRACTOR_VERSION", "literature-map-v1").strip()
        or "literature-map-v1"
    )
    literature_map_normalization_config = os.getenv(
        "LITERATURE_MAP_NORMALIZATION_CONFIG",
        "",
    ).strip()
    literature_map_timeout_seconds = max(
        10,
        int(os.getenv("LITERATURE_MAP_TIMEOUT_SECONDS", "120")),
    )
    conversation_db = os.getenv("CONVERSATION_DB") or str(
        BACKEND_DIR / "storage" / "metadata" / "conversations.sqlite3",
    )
    domain_tree_retry_attempts = max(1, int(os.getenv("DOMAIN_TREE_RETRY_ATTEMPTS", "3")))
    domain_tree_retry_base_delay_seconds = max(
        0.0,
        float(os.getenv("DOMAIN_TREE_RETRY_BASE_DELAY_SECONDS", "2")),
    )
    domain_tree_json_output = os.getenv(
        "DOMAIN_TREE_JSON_OUTPUT",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    model_max_output_tokens = max(1, int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "4096")))
    model_output_tokens_upper_bound = max(
        model_max_output_tokens,
        int(os.getenv("MODEL_OUTPUT_TOKENS_UPPER_BOUND", "65536")),
    )
    domain_tree_max_output_tokens = max(
        1,
        int(os.getenv("DOMAIN_TREE_MAX_OUTPUT_TOKENS", "16384")),
    )
    domain_tree_max_heading_count = max(
        1,
        int(os.getenv("DOMAIN_TREE_MAX_HEADING_COUNT", "50")),
    )
    domain_tree_job_max_workers = max(1, int(os.getenv("DOMAIN_TREE_JOB_MAX_WORKERS", "2")))
    domain_tree_job_ttl_hours = max(1, int(os.getenv("DOMAIN_TREE_JOB_TTL_HOURS", "168")))
    domain_tree_job_stale_seconds = max(1, int(os.getenv("DOMAIN_TREE_JOB_STALE_SECONDS", "300")))
    domain_tree_job_cleanup_interval_seconds = max(
        1,
        int(os.getenv("DOMAIN_TREE_JOB_CLEANUP_INTERVAL_SECONDS", "3600")),
    )
    domain_tree_job_max_history = max(1, int(os.getenv("DOMAIN_TREE_JOB_MAX_HISTORY", "1000")))
    domain_tree_job_db = os.getenv("DOMAIN_TREE_JOB_DB") or str(
        BACKEND_DIR / "storage" / "metadata" / "domain_tree_jobs.sqlite3",
    )
    semantic_graph_max_workers = max(1, min(int(os.getenv("SEMANTIC_GRAPH_MAX_WORKERS", "4")), 16))
    semantic_graph_chunk_size = max(1200, int(os.getenv("SEMANTIC_GRAPH_CHUNK_SIZE", "6000")))
    semantic_graph_chunk_overlap = max(
        0,
        min(
            int(os.getenv("SEMANTIC_GRAPH_CHUNK_OVERLAP", "400")),
            semantic_graph_chunk_size // 3,
        ),
    )
    semantic_graph_json_output = os.getenv(
        "SEMANTIC_GRAPH_JSON_OUTPUT",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    semantic_graph_max_output_tokens = max(
        1,
        int(os.getenv("SEMANTIC_GRAPH_MAX_OUTPUT_TOKENS", "4096")),
    )
    semantic_graph_type_language_model_correction = os.getenv(
        "SEMANTIC_GRAPH_TYPE_LANGUAGE_MODEL_CORRECTION",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    semantic_graph_progress_interval = max(
        1,
        int(os.getenv("SEMANTIC_GRAPH_PROGRESS_INTERVAL", "10")),
    )
    semantic_graph_failure_window = max(
        1,
        int(os.getenv("SEMANTIC_GRAPH_FAILURE_WINDOW", "20")),
    )
    semantic_graph_failure_window_minimum = max(
        1,
        min(
            int(os.getenv("SEMANTIC_GRAPH_FAILURE_WINDOW_MINIMUM", "10")),
            semantic_graph_failure_window,
        ),
    )
    semantic_graph_failure_rate_limit = min(
        1.0,
        max(0.0, float(os.getenv("SEMANTIC_GRAPH_FAILURE_RATE_LIMIT", "0.5"))),
    )
    semantic_graph_consecutive_fatal_limit = max(
        1,
        int(os.getenv("SEMANTIC_GRAPH_CONSECUTIVE_FATAL_LIMIT", "8")),
    )
    semantic_graph_max_requests = max(0, int(os.getenv("SEMANTIC_GRAPH_MAX_REQUESTS", "0")))
    semantic_graph_max_input_tokens = max(
        0,
        int(os.getenv("SEMANTIC_GRAPH_MAX_INPUT_TOKENS", "0")),
    )
    semantic_graph_estimated_chars_per_token = max(
        1.0,
        float(os.getenv("SEMANTIC_GRAPH_ESTIMATED_CHARS_PER_TOKEN", "4")),
    )
    semantic_graph_ready_ratio = min(
        1.0,
        max(0.0, float(os.getenv("SEMANTIC_GRAPH_READY_RATIO", "0.95"))),
    )
    semantic_graph_degraded_ratio = min(
        semantic_graph_ready_ratio,
        max(0.0, float(os.getenv("SEMANTIC_GRAPH_DEGRADED_RATIO", "0.5"))),
    )
    semantic_graph_auto_resume_attempts = max(
        0,
        min(int(os.getenv("SEMANTIC_GRAPH_AUTO_RESUME_ATTEMPTS", "2")), 2),
    )
    semantic_graph_auto_resume_delay_seconds = max(
        0.0,
        float(os.getenv("SEMANTIC_GRAPH_AUTO_RESUME_DELAY_SECONDS", "2")),
    )
    research_agent_max_papers = int(os.getenv("RESEARCH_AGENT_MAX_PAPERS", "100"))
    research_agent_max_sources = int(os.getenv("RESEARCH_AGENT_MAX_SOURCES", "6"))
    # 单次 facet 的候选召回量与最终回答证据量必须解耦。前者控制检索开销，
    # 后者只是上下文安全护栏，实际选择数量由核心要求覆盖和上下文预算动态决定。
    research_agent_max_evidence_groups = int(
        os.getenv("RESEARCH_AGENT_MAX_EVIDENCE_GROUPS", "12")
    )
    rag_chunk_target_tokens = int(os.getenv("RAG_CHUNK_TARGET_TOKENS", "500"))
    rag_chunk_max_tokens = int(os.getenv("RAG_CHUNK_MAX_TOKENS", "700"))
    rag_chunk_overlap_tokens = int(os.getenv("RAG_CHUNK_OVERLAP_TOKENS", "80"))
    research_agent_max_context_chars = int(os.getenv("RESEARCH_AGENT_MAX_CONTEXT_CHARS", "18000"))
    research_agent_request_timeout = int(os.getenv("RESEARCH_AGENT_REQUEST_TIMEOUT", "90"))
    agent_model_max_concurrency = max(
        1,
        min(int(os.getenv("AGENT_MODEL_MAX_CONCURRENCY", "4")), 8),
    )
    research_semantic_max_context_chars = max(
        4000,
        int(os.getenv("RESEARCH_SEMANTIC_MAX_CONTEXT_CHARS", "12000")),
    )
    research_semantic_max_output_tokens = max(
        512,
        min(int(os.getenv("RESEARCH_SEMANTIC_MAX_OUTPUT_TOKENS", "3072")), 8192),
    )
    auxiliary_model_provider = os.getenv("AUXILIARY_MODEL_PROVIDER", "").strip()
    auxiliary_model_protocol = os.getenv("AUXILIARY_MODEL_PROTOCOL", "").strip()
    auxiliary_model_name = os.getenv("AUXILIARY_MODEL_NAME", "").strip()
    auxiliary_model_base_url = os.getenv("AUXILIARY_MODEL_BASE_URL", "").strip()
    auxiliary_model_api_key = os.getenv("AUXILIARY_MODEL_API_KEY", "").strip()
    # 百炼使用 OpenAI 兼容 Embedding 协议；没有专用变量时复用官方 DASHSCOPE_API_KEY。
    rag_embedding_model = os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-v4")
    rag_embedding_base_url = (
        os.getenv("RAG_EMBEDDING_BASE_URL")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).rstrip("/")
    rag_embedding_api_key = os.getenv("RAG_EMBEDDING_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
    rag_embedding_timeout = int(os.getenv("RAG_EMBEDDING_TIMEOUT", "60"))
    # 本地后端默认兼容 Ollama；也可切换为 LM Studio 等 OpenAI 兼容嵌入服务。
    rag_local_embedding_model = os.getenv("RAG_LOCAL_EMBEDDING_MODEL", "")
    rag_local_embedding_base_url = (
        os.getenv("RAG_LOCAL_EMBEDDING_BASE_URL")
        or "http://127.0.0.1:11434"
    ).rstrip("/")
    rag_local_embedding_protocol = os.getenv("RAG_LOCAL_EMBEDDING_PROTOCOL", "ollama").strip().lower()
    rag_local_embedding_api_key = os.getenv("RAG_LOCAL_EMBEDDING_API_KEY", "")
    rag_local_embedding_timeout = int(os.getenv("RAG_LOCAL_EMBEDDING_TIMEOUT", "15"))
    rag_vector_store_path = os.getenv("RAG_VECTOR_STORE_PATH") or str(
        BACKEND_DIR / "storage" / "metadata" / "rag_vectors.sqlite3",
    )
    rag_bm25_weight = float(os.getenv("RAG_BM25_WEIGHT", "0.45"))
    rag_vector_weight = float(os.getenv("RAG_VECTOR_WEIGHT", "0.55"))
    rag_max_chunks_per_paper = int(os.getenv("RAG_MAX_CHUNKS_PER_PAPER", "1"))
    hybrid_graph_enabled = os.getenv("HYBRID_GRAPH_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    hybrid_graph_project_id = os.getenv("HYBRID_GRAPH_PROJECT_ID", "workspace-domain-tree").strip()
    hybrid_graph_max_relations = int(os.getenv("HYBRID_GRAPH_MAX_RELATIONS", "8"))
    hybrid_graph_max_evidence = int(os.getenv("HYBRID_GRAPH_MAX_EVIDENCE", "3"))
    orchestrator_min_evidence = int(os.getenv("ORCHESTRATOR_MIN_EVIDENCE", "2"))
    orchestrator_min_query_coverage = float(os.getenv("ORCHESTRATOR_MIN_QUERY_COVERAGE", "0.28"))
    query_planner_max_facets = int(os.getenv("QUERY_PLANNER_MAX_FACETS", "5"))
    rag_complex_target_evidence = int(os.getenv("RAG_COMPLEX_TARGET_EVIDENCE", "6"))
    orchestrator_max_retrieval_rounds = int(os.getenv("ORCHESTRATOR_MAX_RETRIEVAL_ROUNDS", "2"))
    orchestrator_max_action_rounds = int(os.getenv("ORCHESTRATOR_MAX_ACTION_ROUNDS", "5"))
    orchestrator_min_facet_coverage = float(os.getenv("ORCHESTRATOR_MIN_FACET_COVERAGE", "0.6"))
    orchestrator_min_method_evidence = int(os.getenv("ORCHESTRATOR_MIN_METHOD_EVIDENCE", "2"))
    evidence_selection_coverage_weight = float(
        os.getenv("EVIDENCE_SELECTION_COVERAGE_WEIGHT", "8.0")
    )
    evidence_selection_partial_weight = float(
        os.getenv("EVIDENCE_SELECTION_PARTIAL_WEIGHT", "0.5")
    )
    evidence_selection_relevance_weight = float(
        os.getenv("EVIDENCE_SELECTION_RELEVANCE_WEIGHT", "0.35")
    )
    evidence_selection_source_diversity_weight = float(
        os.getenv("EVIDENCE_SELECTION_SOURCE_DIVERSITY_WEIGHT", "0.3")
    )
    evidence_selection_temporal_diversity_weight = float(
        os.getenv("EVIDENCE_SELECTION_TEMPORAL_DIVERSITY_WEIGHT", "0.3")
    )
    evidence_selection_redundancy_weight = float(
        os.getenv("EVIDENCE_SELECTION_REDUNDANCY_WEIGHT", "1.25")
    )
    orchestrator_search_limit_per_source = int(os.getenv("ORCHESTRATOR_SEARCH_LIMIT_PER_SOURCE", "3"))
    error_recovery_max_cycles = int(os.getenv("ERROR_RECOVERY_MAX_CYCLES", "3"))
    error_recovery_base_delay_seconds = float(os.getenv("ERROR_RECOVERY_BASE_DELAY_SECONDS", "0.5"))
    agent_run_log_dir = os.getenv("AGENT_RUN_LOG_DIR") or str(
        BACKEND_DIR / "storage" / "logs" / "research_runs",
    )
    hunter_download_dir = os.getenv("HUNTER_DOWNLOAD_DIR") or str(
        BACKEND_DIR / "storage" / "papers",
    )
    backend_storage_dir = os.getenv("BACKEND_STORAGE_DIR") or str(
        BACKEND_DIR / "storage",
    )
    mineru_output_dir = os.getenv("MINERU_OUTPUT_DIR") or str(
        BACKEND_DIR / "storage" / "markdown",
    )
    mineru_api_token = os.getenv("MINERU_API_TOKEN", "")
    mineru_api_base = os.getenv("MINERU_API_BASE", "https://mineru.net/api/v4").rstrip("/")
    mineru_model_version = os.getenv("MINERU_MODEL_VERSION", "vlm").strip() or "vlm"
    mineru_poll_interval_seconds = float(os.getenv("MINERU_POLL_INTERVAL_SECONDS", "3"))
    mineru_cloud_timeout_seconds = int(os.getenv("MINERU_CLOUD_TIMEOUT_SECONDS", "1800"))
    mineru_request_timeout_seconds = int(os.getenv("MINERU_REQUEST_TIMEOUT_SECONDS", "120"))
    mineru_batch_size = min(200, max(1, int(os.getenv("MINERU_BATCH_SIZE", "50"))))
    mineru_upload_concurrency = max(1, int(os.getenv("MINERU_UPLOAD_CONCURRENCY", "6")))
    mineru_download_concurrency = max(1, int(os.getenv("MINERU_DOWNLOAD_CONCURRENCY", "4")))
    mineru_index_concurrency = max(1, int(os.getenv("MINERU_INDEX_CONCURRENCY", "2")))
    mineru_max_retries = max(0, int(os.getenv("MINERU_MAX_RETRIES", "4")))
    mineru_enable_local_cli_fallback = os.getenv(
        "MINERU_ENABLE_LOCAL_CLI_FALLBACK",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    split_min_length = int(os.getenv("SPLIT_MIN_LENGTH", "1500"))
    split_max_length = int(os.getenv("SPLIT_MAX_LENGTH", "2000"))
    hunter_metadata_db = os.getenv("HUNTER_METADATA_DB") or str(
        BACKEND_DIR / "storage" / "metadata" / "papers.sqlite3",
    )
    ccf_catalog_db = os.getenv("CCF_CATALOG_DB") or str(
        BACKEND_DIR / "storage" / "metadata" / "ccf_catalog.sqlite3",
    )
    sjr_catalog_db = os.getenv("SJR_CATALOG_DB") or str(
        BACKEND_DIR / "storage" / "metadata" / "sjr_catalog.sqlite3",
    )


_initial_settings = Settings()
runtime_config_manager = RuntimeConfigManager.from_object(_initial_settings)
settings = RuntimeSettingsProxy(runtime_config_manager)
