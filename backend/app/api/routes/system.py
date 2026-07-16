"""Health, capability and diagnostics routes."""

from fastapi import APIRouter, FastAPI

from app.services.paper_search import SUPPORTED_SOURCES


router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "research-assistant-fastapi-backend",
        "sources": sorted(SUPPORTED_SOURCES.keys()),
    }


@router.get("/api/papers/sources")
def paper_sources() -> dict:
    return {"sources": sorted(SUPPORTED_SOURCES.keys())}


def install_debug_route(app: FastAPI) -> None:
    @app.get("/api/debug/routes")
    def debug_routes() -> dict:
        return {
            "routes": sorted(
                {
                    f"{','.join(sorted(route.methods or []))} {route.path}"
                    for route in app.routes
                    if getattr(route, "path", "")
                },
            ),
        }


__all__ = ["install_debug_route", "router"]
