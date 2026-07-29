"""为文献地图派生数据生成稳定版本指纹。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def compute_document_version(
    paper: dict[str, Any],
    *,
    document_text: str = "",
    evidence_chunks: Iterable[dict[str, Any]] | None = None,
) -> str:
    """根据真实文档内容生成稳定哈希，不依赖文件路径或修改时间。"""

    chunks = [
        {
            "record_id": str(item.get("record_id") or item.get("recordId") or ""),
            "chunk_index": int(item.get("chunk_index") or item.get("chunkIndex") or 0),
            "section": str(item.get("section") or ""),
            "text": str(item.get("text") or item.get("content") or ""),
            "origin_type": str(
                item.get("origin_type") or item.get("originType") or "paper_text"
            ),
        }
        for item in evidence_chunks or []
        if isinstance(item, dict)
    ]
    chunks.sort(key=lambda item: (item["record_id"], item["chunk_index"], item["section"]))
    payload = {
        "paper_id": str(paper.get("id") or paper.get("recordId") or ""),
        "title": str(paper.get("title") or ""),
        "year": str(paper.get("year") or ""),
        "doi": str(paper.get("doi") or ""),
        "document_text": str(document_text or ""),
        "chunks": chunks,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stable_map_id(prefix: str, *parts: str) -> str:
    normalized = "\x1f".join(str(part or "").strip().casefold() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


__all__ = ["compute_document_version", "stable_map_id"]
