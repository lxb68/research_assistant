"""Feature-scoped FastAPI routers."""

from .domain_tree import router as domain_tree_router
from .jobs import router as jobs_router
from .mineru import router as mineru_router
from .papers import router as papers_router
from .research import router as research_router
from .settings import router as settings_router
from .system import router as system_router
from app.api.streaming import router as streaming_router

ALL_ROUTERS = (
    system_router,
    research_router,
    settings_router,
    papers_router,
    domain_tree_router,
    jobs_router,
    mineru_router,
    streaming_router,
)

__all__ = ["ALL_ROUTERS"]
