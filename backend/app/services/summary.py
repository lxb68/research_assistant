from __future__ import annotations

from typing import Any

# 生成章节路径、段落摘要和适合界面展示的简短文本。


def generate_enhanced_summary(
    section: dict[str, Any],
    outline: list[dict[str, Any]],
    part_index: int | None = None,
    total_parts: int | None = None,
) -> str:
    """为章节或拆分片段生成便于阅读的摘要路径。"""
    heading_paths = _build_heading_paths(section, outline)
    if not heading_paths:
        doc_title = _find_document_title(outline) or "文档"
        summary = f"{doc_title} 前言"
    elif len(heading_paths) == 1:
        summary = heading_paths[0]
    else:
        summary = _merge_heading_paths(heading_paths)

    if part_index is not None and total_parts is not None and total_parts > 1:
        summary = f"{summary} - Part {part_index}/{total_parts}"
    return summary


def generate_summary(
    section: dict[str, Any],
    outline: list[dict[str, Any]],
    part_index: int | None = None,
    total_parts: int | None = None,
) -> str:
    """保留旧调用方式的兼容包装。"""
    return generate_enhanced_summary(section, outline, part_index, total_parts)


def build_paragraph_summaries(
    content: str,
    *,
    max_summary_length: int = 120,
) -> list[dict[str, Any]]:
    """为片段中的每个非空段落生成简短摘要。"""
    paragraph_summaries: list[dict[str, Any]] = []
    paragraphs = [paragraph.strip() for paragraph in content.split("\n\n") if paragraph.strip()]

    for index, paragraph in enumerate(paragraphs, start=1):
        paragraph_summaries.append(
            {
                "index": index,
                "summary": summarize_paragraph(paragraph, max_length=max_summary_length),
                "charCount": len(paragraph),
            }
        )

    return paragraph_summaries


def summarize_paragraph(paragraph: str, *, max_length: int = 120) -> str:
    """优先保留首句；首句过长时截取前缀作为段落摘要。"""
    normalized = " ".join(paragraph.split())
    normalized = normalized.lstrip("#").strip()
    if not normalized:
        return ""

    lead_sentence = _pick_lead_sentence(normalized)
    if len(lead_sentence) <= max_length:
        return lead_sentence

    truncated = lead_sentence[: max_length - 3].rstrip(" ,;:，；：")
    return f"{truncated}..."


def _build_heading_paths(section: dict[str, Any], outline: list[dict[str, Any]]) -> list[str]:
    """构建标题、路径。"""
    headings = section.get("headings") or []
    if not headings and section.get("heading"):
        headings = [
            {
                "heading": section.get("heading", ""),
                "level": section.get("level", 1),
                "position": section.get("position", 0),
            }
        ]

    if not headings:
        return []

    paths: list[str] = []
    seen: set[str] = set()
    for heading in sorted(headings, key=lambda item: (item.get("position", 0), item.get("level", 999))):
        title = str(heading.get("heading", "")).strip()
        if not title:
            continue
        path = _build_single_heading_path(title, int(heading.get("level", 1) or 1), outline)
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def _build_single_heading_path(title: str, level: int, outline: list[dict[str, Any]]) -> str:
    """构建标题、路径。"""
    heading_index = -1
    for index, item in enumerate(outline):
        if str(item.get("title", "")).strip() == title and int(item.get("level", 0) or 0) == level:
            heading_index = index
            break

    if heading_index < 0:
        return title

    path_parts = [title]
    expected_parent_level = level - 1
    for index in range(heading_index - 1, -1, -1):
        item_level = int(outline[index].get("level", 0) or 0)
        if item_level == expected_parent_level:
            path_parts.insert(0, str(outline[index].get("title", "")).strip())
            expected_parent_level -= 1
            if expected_parent_level <= 0:
                break

    return " > ".join(part for part in path_parts if part)


def _merge_heading_paths(paths: list[str]) -> str:
    """合并标题、路径。"""
    split_paths = [path.split(" > ") for path in paths if path]
    if not split_paths:
        return "未命名段落"

    common_prefix: list[str] = []
    for items in zip(*split_paths):
        if len(set(items)) == 1:
            common_prefix.append(items[0])
        else:
            break

    if common_prefix and len(common_prefix) < min(len(path) for path in split_paths):
        suffixes = [" > ".join(path[len(common_prefix) :]) for path in split_paths]
        return f"{' > '.join(common_prefix)} > [{', '.join(suffix for suffix in suffixes if suffix)}]"

    return ", ".join(paths)


def _find_document_title(outline: list[dict[str, Any]]) -> str:
    """查找来源文档、文档标题。"""
    for item in outline:
        if int(item.get("level", 0) or 0) == 1 and str(item.get("title", "")).strip():
            return str(item.get("title", "")).strip()
    return ""


def _pick_lead_sentence(text: str) -> str:
    """选择首句。"""
    sentence_endings = ".!?。！？；;"
    for index, char in enumerate(text):
        if char in sentence_endings and index >= 20:
            return text[: index + 1].strip()
    return text
