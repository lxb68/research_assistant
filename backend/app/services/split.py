"""按文档结构和长度切分 Markdown，并为片段生成摘要信息。"""

from __future__ import annotations

import re
from typing import Any

from app.services.summary import build_paragraph_summaries, generate_enhanced_summary

DEFAULT_MIN_SPLIT_LENGTH = 800
DEFAULT_MAX_SPLIT_LENGTH = 1600

ABSTRACT_HEADINGS = {
    "abstract",
    "summary",
    "\u6458\u8981",
    "\u6458 \u8981",
    "\u5185\u5bb9\u6458\u8981",
    "executive summary",
}
INTRODUCTION_HEADINGS = {
    "introduction",
    "intro",
    "background",
    "overview",
    "\u5f15\u8a00",
    "\u524d\u8a00",
    "\u7b80\u4ecb",
    "\u6982\u8ff0",
    "\u80cc\u666f",
}
FRONT_MATTER_HEADINGS = {
    "title",
    "authors",
    "author",
    "author information",
    "author details",
    "affiliations",
    "affiliation",
    "correspondence",
    "corresponding author",
    "address",
    "addresses",
    "postal address",
    "contact information",
    "keywords",
    "keywords and phrases",
    "running title",
    "\u4f5c\u8005",
    "\u4f5c\u8005\u4fe1\u606f",
    "\u4f5c\u8005\u7b80\u4ecb",
    "\u5355\u4f4d",
    "\u673a\u6784",
    "\u6240\u5728\u5355\u4f4d",
    "\u901a\u8baf\u4f5c\u8005",
    "\u901a\u8baf\u5730\u5740",
    "\u5730\u5740",
    "\u90ae\u7f16",
    "\u90ae\u653f\u7f16\u7801",
    "\u5173\u952e\u8bcd",
}
REFERENCE_HEADINGS = {
    "references",
    "reference",
    "bibliography",
    "works cited",
    "literature cited",
    "\u53c2\u8003\u6587\u732e",
    "\u53c2\u8003\u8d44\u6599",
    "\u5f15\u7528\u6587\u732e",
}
BACK_MATTER_HEADINGS = {
    "acknowledgements",
    "acknowledgments",
    "acknowledgement",
    "acknowledgment",
    "funding",
    "funding statement",
    "funding information",
    "conflict of interest",
    "conflicts of interest",
    "competing interests",
    "declaration of interests",
    "declarations",
    "author contributions",
    "authors' contributions",
    "credit authorship contribution statement",
    "ethics statement",
    "ethics approval",
    "ethics approval and consent to participate",
    "data availability",
    "data availability statement",
    "availability of data and materials",
    "supplementary material",
    "supplementary materials",
    "appendix",
    "appendices",
    "supporting information",
    "\u9644\u5f55",
    "\u81f4\u8c22",
    "\u57fa\u91d1",
    "\u57fa\u91d1\u652f\u6301",
    "\u9879\u76ee\u8d44\u52a9",
    "\u5229\u76ca\u51b2\u7a81",
    "\u4f5c\u8005\u8d21\u732e",
    "\u4f26\u7406\u58f0\u660e",
    "\u6570\u636e\u53ef\u7528\u6027",
    "\u8865\u5145\u6750\u6599",
}
KEEP_WHOLE_CATEGORIES = {"front_matter", "references", "back_matter"}


def split_long_section(section: dict[str, Any], max_split_length: int) -> list[str]:
    """切分过长章节，并优先保持段落和句子边界完整。"""
    content = str(section.get("content", "")).strip()
    if not content:
        return []
    if len(content) <= max_split_length:
        return [content]

    paragraphs = _extract_paragraphs(content)
    chunks: list[str] = []
    current_parts: list[str] = []
    current_length = 0

    for paragraph in paragraphs:
        paragraph_length = len(paragraph)
        if paragraph_length > max_split_length:
            if current_parts:
                chunks.append(_join_paragraphs(current_parts))
                current_parts = []
                current_length = 0
            chunks.extend(_split_oversized_paragraph(paragraph, max_split_length))
            continue

        projected_length = current_length + paragraph_length + (2 if current_parts else 0)
        if current_parts and projected_length > max_split_length:
            chunks.append(_join_paragraphs(current_parts))
            current_parts = [paragraph]
            current_length = paragraph_length
            continue

        current_parts.append(paragraph)
        current_length = projected_length

    if current_parts:
        chunks.append(_join_paragraphs(current_parts))
    return [chunk for chunk in chunks if chunk.strip()]


def process_sections(
    sections: list[dict[str, Any]],
    outline: list[dict[str, Any]],
    min_split_length: int,
    max_split_length: int,
) -> list[dict[str, Any]]:
    """按结构和大小切分章节，同时保留段落摘要。"""
    if min_split_length <= 0 or max_split_length <= 0:
        raise ValueError("min_split_length and max_split_length must be positive")
    if min_split_length > max_split_length:
        raise ValueError("min_split_length cannot be greater than max_split_length")

    normalized_sections = [_normalize_section(section, index) for index, section in enumerate(sections)]
    semantic_sections = _group_semantic_sections(_annotate_semantic_categories(normalized_sections))
    structured_sections = _merge_small_neighbor_sections(
        semantic_sections,
        min_split_length=min_split_length,
        max_split_length=max_split_length,
    )

    result: list[dict[str, Any]] = []
    pending_small_section: dict[str, Any] | None = None

    for section in structured_sections:
        if section.get("semanticCategory") in KEEP_WHOLE_CATEGORIES:
            if pending_small_section is not None:
                result.extend(_emit_section_chunks(pending_small_section, outline, max_split_length))
                pending_small_section = None
            result.extend(_emit_whole_section_chunk(section, outline))
            continue

        content_length = len(section["content"])

        if content_length < min_split_length:
            pending_small_section = _merge_sections(pending_small_section, section)
            if pending_small_section and len(pending_small_section["content"]) >= min_split_length:
                result.extend(_emit_section_chunks(pending_small_section, outline, max_split_length))
                pending_small_section = None
            continue

        if pending_small_section is not None:
            merged_candidate = _merge_sections(pending_small_section, section)
            if len(merged_candidate["content"]) <= max_split_length:
                result.extend(_emit_section_chunks(merged_candidate, outline, max_split_length))
                pending_small_section = None
                continue

            result.extend(_emit_section_chunks(pending_small_section, outline, max_split_length))
            pending_small_section = None

        result.extend(_emit_section_chunks(section, outline, max_split_length))

    if pending_small_section is not None:
        if result:
            merged_content = _merge_chunk_content(result[-1]["content"], pending_small_section["content"])
            if len(merged_content) <= max_split_length:
                merged_headings = result[-1].get("headings", []) + pending_small_section.get("headings", [])
                synthetic_section = {
                    "content": merged_content,
                    "headings": merged_headings,
                    "semanticCategory": result[-1].get("semanticCategory", "body"),
                }
                refreshed = _emit_section_chunks(synthetic_section, outline, max_split_length)
                result[-1] = refreshed[0]
            else:
                result.extend(_emit_section_chunks(pending_small_section, outline, max_split_length))
        else:
            result.extend(_emit_section_chunks(pending_small_section, outline, max_split_length))

    return result


def split_markdown_document(
    markdown_text: str,
    *,
    min_split_length: int = DEFAULT_MIN_SPLIT_LENGTH,
    max_split_length: int = DEFAULT_MAX_SPLIT_LENGTH,
) -> dict[str, Any]:
    """把 Markdown 解析为大纲和章节，再执行切分与摘要。"""
    outline, sections = parse_markdown_sections(markdown_text)
    chunks = process_sections(
        sections,
        outline,
        min_split_length=min_split_length,
        max_split_length=max_split_length,
    )
    return {
        "outline": outline,
        "sections": sections,
        "chunks": chunks,
        "sectionCount": len(sections),
        "chunkCount": len(chunks),
    }


def parse_markdown_sections(markdown_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 Markdown 转换为结构化大纲和章节记录。"""
    lines = markdown_text.replace("\r\n", "\n").split("\n")
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    outline: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []

    current_heading = ""
    current_level = 0
    current_position = 0
    current_content_lines: list[str] = []

    def flush_current_section() -> None:
        """把当前累积内容整理为一个章节记录。"""
        nonlocal current_content_lines, current_heading, current_level, current_position
        content = "\n".join(current_content_lines).strip()
        if not content and not current_heading:
            current_content_lines = []
            return

        section: dict[str, Any] = {
            "heading": current_heading,
            "level": current_level,
            "position": current_position,
            "content": content,
        }
        if current_heading:
            section["headings"] = [
                {
                    "heading": current_heading,
                    "level": current_level,
                    "position": current_position,
                }
            ]
        sections.append(section)
        current_content_lines = []

    for line_number, line in enumerate(lines, start=1):
        match = heading_pattern.match(line)
        if match:
            flush_current_section()
            current_level = len(match.group(1))
            current_heading = match.group(2).strip()
            current_position = line_number
            outline.append(
                {
                    "title": current_heading,
                    "level": current_level,
                    "position": current_position,
                }
            )
            continue

        current_content_lines.append(line)

    flush_current_section()
    return outline, sections


def _normalize_section(section: dict[str, Any], index: int) -> dict[str, Any]:
    """规范化章节。"""
    content = str(section.get("content", "")).strip()
    heading = str(section.get("heading", "")).strip()
    level = int(section.get("level", 0) or 0)
    position = int(section.get("position", index) or index)

    headings = section.get("headings") or []
    normalized_headings = [
        {
            "heading": str(item.get("heading", "")).strip(),
            "level": int(item.get("level", level or 1) or (level or 1)),
            "position": int(item.get("position", position) or position),
        }
        for item in headings
        if str(item.get("heading", "")).strip()
    ]

    if not normalized_headings and heading:
        normalized_headings = [{"heading": heading, "level": level or 1, "position": position}]

    section_prefix = _build_heading_prefix(heading, level) if heading else ""
    rendered_content = content if not section_prefix else f"{section_prefix}\n{content}" if content else section_prefix
    return {
        **section,
        "heading": heading,
        "level": level,
        "position": position,
        "headings": normalized_headings,
        "content": rendered_content.strip(),
    }


def _merge_small_neighbor_sections(
    sections: list[dict[str, Any]],
    *,
    min_split_length: int,
    max_split_length: int,
) -> list[dict[str, Any]]:
    """合并章节。"""
    merged_sections: list[dict[str, Any]] = []

    for section in sections:
        if not merged_sections:
            merged_sections.append(section)
            continue

        previous = merged_sections[-1]
        if (
            section.get("semanticCategory") not in KEEP_WHOLE_CATEGORIES
            and previous.get("semanticCategory") not in KEEP_WHOLE_CATEGORIES
            and len(section["content"]) < min_split_length
            and _same_heading_branch(previous, section)
            and len(_merge_chunk_content(previous["content"], section["content"])) <= max_split_length
        ):
            merged_sections[-1] = _merge_sections(previous, section)
            continue

        merged_sections.append(section)

    return merged_sections


def _emit_section_chunks(
    section: dict[str, Any],
    outline: list[dict[str, Any]],
    max_split_length: int,
) -> list[dict[str, Any]]:
    """生成章节、证据片段。"""
    chunks = split_long_section(section, max_split_length)
    if not chunks:
        return []

    results: list[dict[str, Any]] = []
    total_parts = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        summary = generate_enhanced_summary(
            section,
            outline,
            part_index=index if total_parts > 1 else None,
            total_parts=total_parts if total_parts > 1 else None,
        )
        paragraph_summaries = build_paragraph_summaries(chunk)
        results.append(
            {
                "summary": summary,
                "content": chunk,
                "paragraphSummaries": paragraph_summaries,
                "headings": section.get("headings", []),
                "semanticCategory": section.get("semanticCategory", "body"),
                "charCount": len(chunk),
                "partIndex": index,
                "totalParts": total_parts,
            }
        )

    return results


def _emit_whole_section_chunk(section: dict[str, Any], outline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成章节。"""
    content = str(section.get("content", "")).strip()
    if not content:
        return []

    summary = str(section.get("summaryLabel", "")).strip() or generate_enhanced_summary(section, outline)
    return [
        {
            "summary": summary,
            "content": content,
            "paragraphSummaries": build_paragraph_summaries(content),
            "headings": section.get("headings", []),
            "semanticCategory": section.get("semanticCategory", "body"),
            "charCount": len(content),
            "partIndex": 1,
            "totalParts": 1,
        }
    ]


def _merge_sections(left: dict[str, Any] | None, right: dict[str, Any]) -> dict[str, Any]:
    """合并章节。"""
    if left is None:
        return dict(right)

    merged_headings = list(left.get("headings", []))
    for heading in right.get("headings", []):
        if heading not in merged_headings:
            merged_headings.append(heading)

    return {
        **left,
        "content": _merge_chunk_content(left.get("content", ""), right.get("content", "")),
        "headings": merged_headings,
        "semanticCategory": left.get("semanticCategory") or right.get("semanticCategory") or "body",
    }


def _merge_chunk_content(left: str, right: str) -> str:
    """合并相邻分块文本并保持段落边界。"""
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    return f"{left}\n\n{right}"


def _same_heading_branch(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """判断两个章节是否位于同一标题分支。"""
    left_headings = left.get("headings", [])
    right_headings = right.get("headings", [])
    if not left_headings or not right_headings:
        return True
    return left_headings[0].get("level") == right_headings[0].get("level")


def _extract_paragraphs(content: str) -> list[str]:
    """提取段落。"""
    return [paragraph.strip() for paragraph in re.split(r"\n\s*\n+", content) if paragraph.strip()]


def _join_paragraphs(paragraphs: list[str]) -> str:
    """拼接段落。"""
    return "\n\n".join(paragraph.strip() for paragraph in paragraphs if paragraph.strip())


def _split_oversized_paragraph(paragraph: str, max_split_length: int) -> list[str]:
    """按句子边界切分超长段落，必要时强制截断。"""
    sentences = _split_sentences(paragraph)
    if not sentences:
        return _hard_split_text(paragraph, max_split_length)

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        projected = f"{current}{sentence}" if current else sentence
        if current and len(projected) > max_split_length:
            chunks.append(current.strip())
            current = sentence
            continue
        if len(sentence) > max_split_length:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(_hard_split_text(sentence, max_split_length))
            continue
        current = projected

    if current.strip():
        chunks.append(current.strip())
    return chunks


def _split_sentences(paragraph: str) -> list[str]:
    """切分句子。"""
    matches = re.findall(r".+?(?:[.!?\u3002\uff01\uff1f\uff1b;](?=\s|$)|$)", paragraph, flags=re.S)
    return [match.strip() for match in matches if match.strip()]


def _hard_split_text(text: str, max_split_length: int) -> list[str]:
    """强制切分文本。"""
    pieces: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_split_length:
            pieces.append(remaining)
            break

        split_at = remaining.rfind(" ", 0, max_split_length + 1)
        if split_at < max_split_length // 2:
            split_at = max_split_length
        pieces.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return [piece for piece in pieces if piece]


def _build_heading_prefix(heading: str, level: int) -> str:
    """构建标题。"""
    safe_level = max(1, min(level or 1, 6))
    return f"{'#' * safe_level} {heading.strip()}".strip()


def _annotate_semantic_categories(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """标注语义类别。"""
    abstract_index = _find_first_heading_index(sections, ABSTRACT_HEADINGS)
    introduction_index = _find_first_heading_index(sections, INTRODUCTION_HEADINGS)
    core_start_index = abstract_index if abstract_index is not None else introduction_index

    annotated_sections: list[dict[str, Any]] = []
    reference_mode = False

    for index, section in enumerate(sections):
        heading_key = _normalize_heading_key(section.get("heading", ""))
        semantic_category = "body"

        if core_start_index is not None and index < core_start_index:
            semantic_category = "front_matter"
        elif core_start_index is None and _looks_like_front_matter_heading(heading_key):
            semantic_category = "front_matter"
        elif heading_key in BACK_MATTER_HEADINGS:
            semantic_category = "back_matter"
            reference_mode = False
        elif heading_key in REFERENCE_HEADINGS:
            semantic_category = "references"
            reference_mode = True
        elif reference_mode:
            semantic_category = "references"

        summary_label = ""
        if semantic_category == "front_matter":
            summary_label = "Front Matter"
        elif semantic_category == "references":
            summary_label = "References"
        elif semantic_category == "back_matter":
            summary_label = "Back Matter"

        annotated_sections.append(
            {
                **section,
                "semanticCategory": semantic_category,
                "summaryLabel": summary_label,
            }
        )

    return annotated_sections


def _group_semantic_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """分组章节。"""
    grouped_sections: list[dict[str, Any]] = []

    for section in sections:
        semantic_category = section.get("semanticCategory", "body")
        if (
            grouped_sections
            and semantic_category in KEEP_WHOLE_CATEGORIES
            and grouped_sections[-1].get("semanticCategory") == semantic_category
        ):
            grouped_sections[-1] = _merge_sections(grouped_sections[-1], section)
            continue

        grouped_sections.append(section)

    return grouped_sections


def _find_first_heading_index(sections: list[dict[str, Any]], candidates: set[str]) -> int | None:
    """查找标题。"""
    for index, section in enumerate(sections):
        if _normalize_heading_key(section.get("heading", "")) in candidates:
            return index
    return None


def _normalize_heading_key(value: Any) -> str:
    """规范化标题。"""
    text = str(value or "").strip().lower()
    text = re.sub(r"^[\d.\-()\[\]\s]+", "", text)
    return re.sub(r"\s+", " ", text)


def _looks_like_front_matter_heading(heading_key: str) -> bool:
    """判断标题。"""
    if not heading_key:
        return True
    if heading_key in FRONT_MATTER_HEADINGS:
        return True
    return any(token in heading_key for token in {"author", "affiliation", "address", "correspond", "\u4f5c\u8005", "\u5355\u4f4d", "\u901a\u8baf", "\u5730\u5740", "\u90ae\u7f16"})
