from pathlib import Path
import os


BACKEND_DIR = Path(__file__).resolve().parents[2]


def load_env_file() -> None:
    """读取 backend/.env，把简单的 KEY=VALUE 配置加载到环境变量。"""
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


settings = Settings()
