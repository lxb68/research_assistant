"""统一描述与校验文献可用内容能力。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


CAPABILITY_FIELDS = ("hasPdf", "hasAbstract", "hasParsedFullText")
_INPUT_ALIASES = {
    "hasPdf": ("hasPdf", "has_pdf"),
    "hasAbstract": ("hasAbstract", "has_abstract"),
    "hasParsedFullText": ("hasParsedFullText", "has_parsed_full_text", "hasFullText", "has_full_text"),
}


def normalize_document_requirements(value: Any) -> dict[str, bool]:
    """只接收明确的布尔约束，忽略模型生成的未知字段和值。"""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, bool] = {}
    for field, aliases in _INPUT_ALIASES.items():
        for alias in aliases:
            candidate = value.get(alias)
            if isinstance(candidate, bool):
                normalized[field] = candidate
                break
    return normalized


def paper_capabilities(paper: dict[str, Any]) -> dict[str, bool]:
    """根据数据库元数据和真实本地文件计算文献能力。"""
    return {
        "hasPdf": _existing_file(paper, "pdfPath", "pdf_path", suffix=".pdf"),
        "hasAbstract": bool(str(paper.get("abstract") or "").strip()),
        "hasParsedFullText": _has_parsed_full_text(paper),
    }


def paper_matches_requirements(paper: dict[str, Any], requirements: Any) -> bool:
    """判断一篇文献是否满足全部结构化能力约束。"""
    normalized = normalize_document_requirements(requirements)
    if not normalized:
        return True
    capabilities = paper_capabilities(paper)
    return all(capabilities[field] is expected for field, expected in normalized.items())


def filter_papers_by_requirements(
    papers: list[dict[str, Any]],
    requirements: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """过滤候选文献，并返回可审计的范围诊断。"""
    normalized = normalize_document_requirements(requirements)
    matched = [paper for paper in papers if paper_matches_requirements(paper, normalized)]
    return matched, {
        "requirements": normalized,
        "candidatePaperCount": len(papers),
        "matchedPaperCount": len(matched),
        "matchedPaperIds": [str(paper.get("id") or "") for paper in matched if str(paper.get("id") or "")],
    }


def _existing_file(paper: dict[str, Any], *keys: str, suffix: str = "") -> bool:
    for key in keys:
        raw_path = str(paper.get(key) or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.is_file() and (not suffix or path.suffix.casefold() == suffix.casefold()):
            return True
    return False


def _has_parsed_full_text(paper: dict[str, Any]) -> bool:
    if _existing_file(paper, "markdownPath", "markdown_path"):
        return True
    output_dir = str(paper.get("markdownOutputDir") or paper.get("markdown_output_dir") or "").strip()
    return bool(output_dir and (Path(output_dir) / "full.md").is_file())


__all__ = [
    "CAPABILITY_FIELDS",
    "filter_papers_by_requirements",
    "normalize_document_requirements",
    "paper_capabilities",
    "paper_matches_requirements",
]
