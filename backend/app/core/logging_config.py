"""配置后端业务日志，确保在 Uvicorn 下也能输出性能诊断信息。"""

from __future__ import annotations

import logging


def configure_app_logging(level_name: str = "INFO") -> None:
    """初始化根日志处理器，并单独设置 app 命名空间的日志级别。"""
    normalized = str(level_name or "INFO").strip().upper()
    level = getattr(logging, normalized, logging.INFO)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    logging.getLogger("app").setLevel(level)


__all__ = ["configure_app_logging"]
