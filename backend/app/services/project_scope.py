"""把项目成员关系投影为领域分析和研究问答的可信作用域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.project_repository import ProjectRepository


class ProjectScopeService:
    """在业务管线入口收紧客户端提交的论文与历史引用。"""

    def __init__(self, metadata_db_path: str | Path) -> None:
        self.projects = ProjectRepository(metadata_db_path)

    def build_research_arguments(
        self,
        *,
        project_id: str,
        project_ids: list[str] | None = None,
        requested_paper_ids: list[str],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected_project_ids = list(dict.fromkeys(
            value
            for raw_value in (project_ids or [project_id])
            if (value := str(raw_value).strip())
        ))
        if not selected_project_ids:
            selected_project_ids = [project_id]
        project_paper_ids = list(dict.fromkeys(
            paper_id
            for selected_project_id in selected_project_ids
            for paper_id in self.projects.list_paper_ids(selected_project_id)
        ))
        if not project_paper_ids:
            raise ValueError("所选项目没有可用于研究问答的论文")
        allowed = set(project_paper_ids)
        requested = list(dict.fromkeys(str(value).strip() for value in requested_paper_ids if str(value).strip()))
        outside = [paper_id for paper_id in requested if paper_id not in allowed]
        if outside:
            raise ValueError(f"请求包含不属于当前项目的论文：{', '.join(outside[:10])}")

        scoped_history: list[dict[str, Any]] = []
        for raw_message in history:
            message = dict(raw_message)
            sources = message.get("sources") or []
            message["sources"] = [
                source
                for source in sources
                if isinstance(source, dict)
                and str(source.get("record_id") or source.get("recordId") or "").strip() in allowed
            ]
            scoped_history.append(message)

        return {
            "history": scoped_history,
            "paper_ids": requested or project_paper_ids,
            "project_paper_ids": project_paper_ids,
            "project_id": selected_project_ids[0],
            "project_ids": selected_project_ids,
            "graph_project_id": selected_project_ids[0] if len(selected_project_ids) == 1 else "",
            "allow_external_search": False,
        }


__all__ = ["ProjectScopeService"]
