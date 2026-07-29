"""组合项目授权范围与语义画像，供研究入口统一调用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.project_scope import ProjectScopeService
from app.services.project_semantic_profile import ProjectSemanticProfileService


class ProjectResearchContextService:
    """保持授权和语义画像各自独立，并提供统一的研究参数装配入口。"""

    def __init__(
        self,
        metadata_db_path: str | Path,
        domain_tree_root: str | Path,
    ) -> None:
        self.scope = ProjectScopeService(metadata_db_path)
        self.semantic_profile = ProjectSemanticProfileService(
            metadata_db_path,
            domain_tree_root,
            project_repository=self.scope.projects,
        )

    def build_arguments(
        self,
        *,
        project_id: str,
        project_ids: list[str] | None,
        requested_paper_ids: list[str],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        arguments = self.scope.build_research_arguments(
            project_id=project_id,
            project_ids=project_ids,
            requested_paper_ids=requested_paper_ids,
            history=history,
        )
        arguments["scope_profile"] = self.semantic_profile.build(
            project_ids=arguments["project_ids"],
            authorized_paper_ids=arguments["authorized_paper_ids"],
        )
        return arguments


__all__ = ["ProjectResearchContextService"]
