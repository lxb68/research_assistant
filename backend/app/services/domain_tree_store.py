"""Filesystem read boundary for domain-tree analysis artifacts."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


class DomainTreeStore:
    def load_tags(self, output_dir: Path) -> list[dict[str, Any]] | None:
        payload = self._read_json(output_dir / "domain_tree.json")
        if isinstance(payload, dict):
            return self._tree_with_heading_limits(payload)
        return payload if isinstance(payload, list) else None

    def load_manifest(self, output_dir: Path) -> dict[str, Any]:
        payload = self._read_json(output_dir / "manifest.json")
        return payload if isinstance(payload, dict) else {}

    def load_result(self, output_dir: Path, project_id: str) -> dict[str, Any] | None:
        """读取并投影人工修订后的项目知识结果。"""
        result = self.load_raw_result(output_dir, project_id)
        if result is None:
            return None
        # 延迟导入避免存储边界与领域服务形成模块级循环依赖。
        from app.services.project_knowledge import apply_project_curation

        return apply_project_curation(output_dir, result, project_id)

    def load_raw_result(self, output_dir: Path, project_id: str) -> dict[str, Any] | None:
        """只读取模型生成产物，不应用人工修订。"""
        domain_payload = self._read_json(output_dir / "domain_tree.json")
        if domain_payload is None:
            return None
        if isinstance(domain_payload, dict):
            stored_project_id = str(domain_payload.get("projectId") or "").strip()
            if stored_project_id and stored_project_id != project_id:
                return None
        graph_status = str(domain_payload.get("graphStatus", "ready")) if isinstance(domain_payload, dict) else "ready"
        graph_payload = (
            self._read_json(output_dir / "knowledge_graph.json")
            if graph_status in {"ready", "degraded"}
            else {}
        )
        manifest_payload = self.load_manifest(output_dir)
        if isinstance(graph_payload, dict):
            graph_project_id = str(graph_payload.get("projectId") or "").strip()
            if graph_project_id and graph_project_id != project_id:
                return None
        manifest_project_id = str(manifest_payload.get("projectId") or "").strip()
        if manifest_project_id and manifest_project_id != project_id:
            return None
        catalog_path = output_dir / "catalog.txt"
        try:
            catalog_text = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
        except OSError:
            catalog_text = ""
        domain_tree = (
            self._tree_with_heading_limits(domain_payload)
            if isinstance(domain_payload, dict)
            else domain_payload
        )
        return {
            "projectId": domain_payload.get("projectId", project_id) if isinstance(domain_payload, dict) else project_id,
            "generatedAt": domain_payload.get("generatedAt", "") if isinstance(domain_payload, dict) else "",
            "action": domain_payload.get("action", "") if isinstance(domain_payload, dict) else "",
            "language": domain_payload.get("language", "") if isinstance(domain_payload, dict) else "",
            "requestedLanguage": domain_payload.get("requestedLanguage", "") if isinstance(domain_payload, dict) else "",
            "headingCounts": domain_payload.get("headingCounts", {}) if isinstance(domain_payload, dict) else {},
            "graphStatus": graph_status,
            "documentCount": domain_payload.get("documentCount", 0) if isinstance(domain_payload, dict) else 0,
            "generationMode": domain_payload.get("generationMode", "unknown") if isinstance(domain_payload, dict) else "unknown",
            "degraded": bool(domain_payload.get("degraded", False)) if isinstance(domain_payload, dict) else False,
            "degradeReason": domain_payload.get("degradeReason", "") if isinstance(domain_payload, dict) else "",
            "warnings": domain_payload.get("warnings", []) if isinstance(domain_payload, dict) else [],
            "domainTree": domain_tree if isinstance(domain_tree, list) else [],
            "knowledgeGraph": graph_payload if isinstance(graph_payload, dict) else {},
            "manifest": manifest_payload,
            "catalogText": catalog_text,
        }

    @staticmethod
    def _tree_with_heading_limits(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
        """按快照记录的数量上限投影领域树，兼容历史超限结果。"""
        raw_tree = payload.get("domainTree")
        if not isinstance(raw_tree, list):
            return None
        heading_counts = payload.get("headingCounts")
        limits = heading_counts if isinstance(heading_counts, dict) else {}
        primary_limit = DomainTreeStore._parse_heading_limit(limits.get("primary"), minimum=1)
        secondary_limit = DomainTreeStore._parse_heading_limit(limits.get("secondary"), minimum=0)
        nodes = raw_tree[:primary_limit] if primary_limit is not None else raw_tree
        projected: list[dict[str, Any]] = []
        for raw_node in nodes:
            if not isinstance(raw_node, dict):
                continue
            node = deepcopy(raw_node)
            children = node.get("child")
            if isinstance(children, list) and secondary_limit is not None:
                limited_children = children[:secondary_limit]
                if limited_children:
                    node["child"] = limited_children
                else:
                    node.pop("child", None)
            projected.append(node)
        return projected

    @staticmethod
    def _parse_heading_limit(value: Any, *, minimum: int) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= minimum else None

    def load_curation(self, output_dir: Path) -> dict[str, Any]:
        """读取人工修订记录；不存在或损坏时返回空修订。"""
        payload = self._read_json(output_dir / "knowledge_curation.json")
        return payload if isinstance(payload, dict) else {}

    def save_curation(self, output_dir: Path, payload: dict[str, Any]) -> None:
        """原子保存人工修订，避免读取端观察到半写入文件。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "knowledge_curation.json"
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)

    @staticmethod
    def _read_json(path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


__all__ = ["DomainTreeStore"]
