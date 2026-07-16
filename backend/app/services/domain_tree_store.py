"""Filesystem read boundary for domain-tree analysis artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DomainTreeStore:
    def load_tags(self, output_dir: Path) -> list[dict[str, Any]] | None:
        payload = self._read_json(output_dir / "domain_tree.json")
        if isinstance(payload, dict):
            tree = payload.get("domainTree")
            return tree if isinstance(tree, list) else None
        return payload if isinstance(payload, list) else None

    def load_manifest(self, output_dir: Path) -> dict[str, Any]:
        payload = self._read_json(output_dir / "manifest.json")
        return payload if isinstance(payload, dict) else {}

    def load_result(self, output_dir: Path, project_id: str) -> dict[str, Any] | None:
        domain_payload = self._read_json(output_dir / "domain_tree.json")
        if domain_payload is None:
            return None
        graph_status = str(domain_payload.get("graphStatus", "ready")) if isinstance(domain_payload, dict) else "ready"
        graph_payload = self._read_json(output_dir / "knowledge_graph.json") if graph_status == "ready" else {}
        manifest_payload = self.load_manifest(output_dir)
        catalog_path = output_dir / "catalog.txt"
        try:
            catalog_text = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
        except OSError:
            catalog_text = ""
        domain_tree = domain_payload.get("domainTree") if isinstance(domain_payload, dict) else domain_payload
        return {
            "projectId": domain_payload.get("projectId", project_id) if isinstance(domain_payload, dict) else project_id,
            "generatedAt": domain_payload.get("generatedAt", "") if isinstance(domain_payload, dict) else "",
            "action": domain_payload.get("action", "") if isinstance(domain_payload, dict) else "",
            "language": domain_payload.get("language", "") if isinstance(domain_payload, dict) else "",
            "requestedLanguage": domain_payload.get("requestedLanguage", "") if isinstance(domain_payload, dict) else "",
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
    def _read_json(path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


__all__ = ["DomainTreeStore"]
