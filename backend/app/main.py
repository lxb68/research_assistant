"""FastAPI application composition root.

Feature behavior lives in ``app.api.routes``; this module only configures the
application and middleware so imports stay cheap and route ownership is clear.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import ALL_ROUTERS
from app.api.routes.mineru import _process_mineru_sync
from app.api.routes.system import install_debug_route
from app.core.config import settings
from app.core.logging_config import configure_app_logging
from app.services.mineru import MinerURequest
from app.services.stream_tasks import stream_task_manager
from app.services.background_jobs import background_job_manager
from app.services.background_job_handlers import register_background_job_handlers


configure_app_logging(settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start job recovery/cleanup and interrupt local active jobs on shutdown."""
    register_background_job_handlers(background_job_manager)
    background_job_manager.start()
    try:
        yield
    finally:
        background_job_manager.shutdown()
        stream_task_manager.shutdown()

app = FastAPI(
    title="Research Assistant API",
    description="用于文献检索与研究代理的 Python FastAPI 后端。",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in ALL_ROUTERS:
    app.include_router(router)

install_debug_route(app)


async def process_mineru(request: MinerURequest):
    """Compatibility facade for callers of the former monolithic endpoint."""
    return await asyncio.to_thread(_process_mineru_sync, request)


__all__ = ["MinerURequest", "_process_mineru_sync", "app", "process_mineru"]
