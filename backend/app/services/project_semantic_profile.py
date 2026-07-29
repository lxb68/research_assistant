"""把项目知识产物投影为仅供查询消歧与检索导航使用的语义画像。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.services.domain_tree_store import DomainTreeStore
from app.services.paper_repository import PaperRepository
from app.services.project_repository import ProjectRepository


class ProjectSemanticProfileService:
    """从授权项目的领域树和文献元数据构造有界、不可充当事实证据的画像。"""

    def __init__(
        self,
        metadata_db_path: str | Path,
        domain_tree_root: str | Path,
        *,
        max_anchors: int = 64,
        max_documents: int = 80,
        project_repository: ProjectRepository | None = None,
        paper_repository: PaperRepository | None = None,
    ) -> None:
        self.projects = project_repository or ProjectRepository(metadata_db_path)
        self.papers = paper_repository or PaperRepository(metadata_db_path)
        self.domain_tree_root = Path(domain_tree_root)
        self.store = DomainTreeStore()
        self.max_anchors = max(1, int(max_anchors))
        self.max_documents = max(1, int(max_documents))

    def build(
        self,
        *,
        project_ids: list[str],
        authorized_paper_ids: list[str],
    ) -> dict[str, Any]:
        """返回可安全传给规划器的项目语义画像，不包含论文正文。"""
        normalized_project_ids = list(
            dict.fromkeys(str(value).strip() for value in project_ids if str(value).strip())
        )
        normalized_paper_ids = list(
            dict.fromkeys(
                str(value).strip()
                for value in authorized_paper_ids
                if str(value).strip()
            )
        )
        projects = [
            {
                "id": project["id"],
                "name": str(project.get("name") or "")[:200],
                "description": str(project.get("description") or "")[:1000],
            }
            for project_id in normalized_project_ids
            if (project := self.projects.get(project_id)) is not None
        ]
        anchors: list[dict[str, Any]] = []
        seen_labels: set[tuple[str, str, str]] = set()
        for project_id in normalized_project_ids:
            result = self.store.load_result(
                self.domain_tree_root / project_id,
                project_id,
            )
            if not result:
                continue
            self._append_anchors(
                anchors,
                seen_labels,
                project_id=project_id,
                nodes=result.get("domainTree") or [],
                parent_id="",
                path=(),
            )
            if len(anchors) >= self.max_anchors:
                break

        documents = [
            self._document_metadata(record)
            for record in self.papers.list_by_ids(normalized_paper_ids[: self.max_documents])
        ]
        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "projects": projects,
            "anchors": anchors[: self.max_anchors],
            "documents": documents,
            "allowedUses": [
                "query_disambiguation",
                "retrieval_vocabulary",
                "document_shortlisting",
            ],
            "allowedAsAnswerEvidence": False,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return payload

    def _append_anchors(
        self,
        anchors: list[dict[str, Any]],
        seen_labels: set[tuple[str, str, str]],
        *,
        project_id: str,
        nodes: list[Any],
        parent_id: str,
        path: tuple[int, ...],
    ) -> None:
        for index, raw_node in enumerate(nodes):
            if len(anchors) >= self.max_anchors:
                return
            if not isinstance(raw_node, dict):
                continue
            label = str(raw_node.get("label") or "").strip()
            if not label:
                continue
            normalized_label = " ".join(label.casefold().split())
            label_key = (project_id, parent_id, normalized_label)
            node_path = (*path, index)
            anchor_id = str(raw_node.get("id") or "").strip() or self._stable_anchor_id(
                project_id,
                node_path,
                label,
            )
            if label_key not in seen_labels:
                seen_labels.add(label_key)
                anchors.append(
                    {
                        "id": anchor_id,
                        "label": label[:500],
                        "parentId": parent_id,
                        "projectId": project_id,
                        "source": "curated_domain_tree",
                    }
                )
            children = raw_node.get("child")
            if isinstance(children, list):
                self._append_anchors(
                    anchors,
                    seen_labels,
                    project_id=project_id,
                    nodes=children,
                    parent_id=anchor_id,
                    path=node_path,
                )

    @staticmethod
    def _stable_anchor_id(project_id: str, path: tuple[int, ...], label: str) -> str:
        seed = f"{project_id}|{'.'.join(str(value) for value in path)}|{label}"
        return f"anchor:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"

    @staticmethod
    def _document_metadata(record: dict[str, Any]) -> dict[str, Any]:
        abstract = str(
            record.get("abstract")
            or record.get("abstractText")
            or record.get("summary")
            or ""
        ).strip()
        year = str(
            record.get("year")
            or record.get("publicationYear")
            or record.get("published")
            or ""
        ).strip()
        return {
            "recordId": str(record.get("id") or "")[:200],
            "title": str(record.get("title") or "")[:1000],
            "year": year[:40],
            "abstractSnippet": abstract[:1200],
        }


__all__ = ["ProjectSemanticProfileService"]
