"""按文档结构和长度切分 Markdown，并为片段生成摘要信息。"""

from __future__ import annotations

import hashlib
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

# PDF 文本提取器经常保留章节文字，却丢失 Markdown 标题标记。这里仅维护跨领域
# 通用的论文结构词，用于结构退化时的保守恢复，不绑定论文标题或固定章节编号。
COMMON_SECTION_HEADINGS = {
    *ABSTRACT_HEADINGS,
    *INTRODUCTION_HEADINGS,
    *REFERENCE_HEADINGS,
    *BACK_MATTER_HEADINGS,
    "related work",
    "related works",
    "preliminaries",
    "preliminary",
    "method",
    "methods",
    "methodology",
    "materials and methods",
    "technical overview",
    "implementation",
    "experimental setup",
    "experimental results",
    "experiments",
    "evaluation",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "limitations",
    "security analysis",
    "security estimation",
    "相关工作",
    "预备知识",
    "方法",
    "实验设置",
    "实验结果",
    "实验",
    "评估",
    "结果",
    "讨论",
    "结论",
    "局限性",
    "安全性分析",
}
_NUMBERED_HEADING_PATTERN = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,4})\s+(.+?)\s*$")
_NUMBER_ONLY_HEADING_PATTERN = re.compile(r"^\d{1,2}(?:\.\d{1,3}){0,4}$")
_ABSTRACT_PREFIX_PATTERN = re.compile(r"^(abstract|summary|摘要)\s*[.:：]\s*(.+)$", re.IGNORECASE)

# 这些模式只识别 Markdown/HTML 的结构语法，不绑定论文名称、章节号或领域术语。
# 命中的结构即使因长度限制被拆开，也会共享 structureId 和显式连续关系。
_EXPLICIT_STRUCTURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "algorithm",
        re.compile(
            r"<div\b[^>]*class=[\"'][^\"']*(?:algorithm|pseudocode)[^\"']*[\"'][^>]*>.*?</div>",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    ("table", re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)),
    ("code", re.compile(r"```[^\n]*\n.*?```", re.DOTALL)),
    ("equation", re.compile(r"\$\$.*?\$\$|\\\[.*?\\\]", re.DOTALL)),
)
_MARKDOWN_TABLE_PATTERN = re.compile(
    r"(?m)^(?:\s*\|.*\|\s*\n)"
    r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
    r"(?:\n\s*\|.*\|\s*)+"
)


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
                # 重新切分后可能产生多个语义单元，不能只保留第一块。
                result[-1:] = refreshed
            else:
                result.extend(_emit_section_chunks(pending_small_section, outline, max_split_length))
        else:
            result.extend(_emit_section_chunks(pending_small_section, outline, max_split_length))

    return _finalize_chunk_continuity(result)


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
        "schemaVersion": 2,
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
    explicit_heading_count = sum(bool(heading_pattern.match(line)) for line in lines)
    recover_plain_headings = _should_recover_plain_text_headings(markdown_text, explicit_heading_count)
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

    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        line_number = line_index + 1
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
            line_index += 1
            continue

        recovered = (
            _match_plain_text_heading(lines, line_index)
            if recover_plain_headings
            else None
        )
        if recovered:
            flush_current_section()
            current_heading = recovered["title"]
            current_level = recovered["level"]
            current_position = line_number
            outline.append(
                {
                    "title": current_heading,
                    "level": current_level,
                    "position": current_position,
                    "recovered": True,
                }
            )
            remainder = str(recovered.get("remainder") or "").strip()
            if remainder:
                current_content_lines.append(remainder)
            line_index += int(recovered["consumed"])
            continue

        current_content_lines.append(line)
        line_index += 1

    flush_current_section()
    return outline, sections


def _should_recover_plain_text_headings(markdown_text: str, explicit_heading_count: int) -> bool:
    """仅在长文档标题结构明显退化时启用普通文本标题恢复。"""
    return explicit_heading_count <= 1 and len(markdown_text.strip()) >= 2000


def _match_plain_text_heading(lines: list[str], index: int) -> dict[str, Any] | None:
    """保守识别 PDF 提取文本中的编号标题、标准章节标题和摘要前缀。"""
    value = lines[index].strip()
    if not value or value.startswith("#"):
        return None

    abstract_match = _ABSTRACT_PREFIX_PATTERN.match(value)
    if abstract_match:
        return {
            "title": abstract_match.group(1).strip().title(),
            "level": 2,
            "consumed": 1,
            "remainder": abstract_match.group(2).strip(),
        }

    numbered_match = _NUMBERED_HEADING_PATTERN.match(value)
    if numbered_match and _looks_like_plain_heading_title(numbered_match.group(2)):
        number = numbered_match.group(1)
        return {
            "title": f"{number} {numbered_match.group(2).strip()}",
            "level": min(6, 2 + number.count(".")),
            "consumed": 1,
        }

    if _NUMBER_ONLY_HEADING_PATTERN.match(value):
        next_index = _next_nonempty_line_index(lines, index + 1, max_lookahead=2)
        if next_index is not None:
            title = lines[next_index].strip()
            if _looks_like_plain_heading_title(title):
                return {
                    "title": f"{value} {title}",
                    "level": min(6, 2 + value.count(".")),
                    "consumed": next_index - index + 1,
                }

    if _normalize_heading_key(value) in COMMON_SECTION_HEADINGS:
        return {"title": value, "level": 2, "consumed": 1}
    return None


def _next_nonempty_line_index(lines: list[str], start: int, *, max_lookahead: int) -> int | None:
    """在很小的窗口内查找下一行，避免跨正文误拼章节标题。"""
    end = min(len(lines), start + max_lookahead)
    for index in range(start, end):
        if lines[index].strip():
            return index
    return None


def _looks_like_plain_heading_title(value: str) -> bool:
    """判断短文本是否具备章节标题形态，过滤页眉、公式、作者与正文句子。"""
    text = str(value or "").strip()
    if not text or len(text) > 120 or len(text.split()) > 16:
        return False
    if re.search(r"https?://|@|[,;；。！？!?]$", text, flags=re.IGNORECASE):
        return False
    normalized = _normalize_heading_key(text)
    if normalized in COMMON_SECTION_HEADINGS:
        return True
    if re.search(r"[\u4e00-\u9fff]", text):
        return not bool(re.search(r"[。！？；]", text)) and len(text) <= 40

    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    if not words:
        return False
    significant = [word for word in words if word.casefold() not in {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"}]
    if not significant:
        return False
    title_case_count = sum(word[0].isupper() or word.isupper() for word in significant)
    return title_case_count / len(significant) >= 0.6


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
    """按语义单元生成证据片段，并保留跨片段连续结构。"""
    semantic_units = _extract_semantic_units(section, max_split_length=max_split_length)
    if not semantic_units:
        return []

    results: list[dict[str, Any]] = []
    total_parts = len(semantic_units)
    for index, unit in enumerate(semantic_units, start=1):
        chunk = str(unit["content"])
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
                "semanticType": unit["semanticType"],
                "structureId": unit.get("structureId", ""),
                "structurePartIndex": unit.get("structurePartIndex", 0),
                "structurePartCount": unit.get("structurePartCount", 0),
                "isStructureStart": bool(unit.get("isStructureStart")),
                "isStructureEnd": bool(unit.get("isStructureEnd")),
                "charCount": len(chunk),
                "partIndex": index,
                "totalParts": total_parts,
            }
        )

    return results


def _extract_semantic_units(
    section: dict[str, Any],
    *,
    max_split_length: int,
) -> list[dict[str, Any]]:
    """把章节拆成普通文本与显式结构单元，结构内部只在必要时分片。"""
    content = str(section.get("content") or "").strip()
    if not content:
        return []

    matches: list[tuple[int, int, str]] = []
    for semantic_type, pattern in _EXPLICIT_STRUCTURE_PATTERNS:
        matches.extend((match.start(), match.end(), semantic_type) for match in pattern.finditer(content))
    matches.extend(
        (match.start(), match.end(), "table")
        for match in _MARKDOWN_TABLE_PATTERN.finditer(content)
    )

    # 模式可能嵌套（例如算法 HTML 中包含公式）；优先采用起点更早、跨度更大的外层结构。
    selected: list[tuple[int, int, str]] = []
    for start, end, semantic_type in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
        if selected and start < selected[-1][1]:
            continue
        selected.append((start, end, semantic_type))

    units: list[dict[str, Any]] = []
    cursor = 0
    structure_ordinal = 0
    for start, end, semantic_type in selected:
        prose = content[cursor:start].strip()
        if prose:
            units.extend(_prose_units(prose, max_split_length=max_split_length))

        structure_text = content[start:end].strip()
        structure_ordinal += 1
        structure_id = _make_structure_id(
            section,
            semantic_type=semantic_type,
            ordinal=structure_ordinal,
            content=structure_text,
        )
        parts = _split_structured_text(structure_text, max_split_length=max_split_length)
        for part_index, part in enumerate(parts, start=1):
            units.append(
                {
                    "content": part,
                    "semanticType": semantic_type,
                    "structureId": structure_id,
                    "structurePartIndex": part_index,
                    "structurePartCount": len(parts),
                    "isStructureStart": part_index == 1,
                    "isStructureEnd": part_index == len(parts),
                }
            )
        cursor = end

    trailing = content[cursor:].strip()
    if trailing:
        units.extend(_prose_units(trailing, max_split_length=max_split_length))
    return units


def _prose_units(text: str, *, max_split_length: int) -> list[dict[str, Any]]:
    """按原有段落和句子策略拆分普通文本。"""
    return [
        {
            "content": part,
            "semanticType": "prose",
            "structureId": "",
            "structurePartIndex": 0,
            "structurePartCount": 0,
            "isStructureStart": False,
            "isStructureEnd": False,
        }
        for part in split_long_section({"content": text}, max_split_length)
    ]


def _split_structured_text(text: str, *, max_split_length: int) -> list[str]:
    """在保留行边界的前提下拆分超长结构，避免把步骤、表格行或代码行混入普通段落。"""
    if len(text) <= max_split_length:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines(keepends=True):
        if len(line) > max_split_length:
            if current:
                parts.append("".join(current).strip())
                current = []
                current_length = 0
            parts.extend(_hard_split_text(line.strip(), max_split_length))
            continue
        if current and current_length + len(line) > max_split_length:
            parts.append("".join(current).strip())
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _make_structure_id(
    section: dict[str, Any],
    *,
    semantic_type: str,
    ordinal: int,
    content: str,
) -> str:
    """生成内容稳定的结构标识，重新索引同一文档时保持可追踪性。"""
    heading_key = " > ".join(
        str(item.get("heading") or "").strip()
        for item in section.get("headings") or []
        if isinstance(item, dict) and str(item.get("heading") or "").strip()
    )
    fingerprint_source = f"{heading_key}\n{semantic_type}\n{ordinal}\n{content}".encode("utf-8")
    fingerprint = hashlib.sha1(fingerprint_source).hexdigest()[:16]
    return f"structure-{semantic_type}-{fingerprint}"


def _finalize_chunk_continuity(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为同一结构的连续分片写入稳定的前后引用和文档顺序。"""
    structure_members: dict[str, list[int]] = {}
    for sequence, chunk in enumerate(chunks):
        chunk["sequence"] = sequence
        structure_id = str(chunk.get("structureId") or "")
        if structure_id:
            structure_members.setdefault(structure_id, []).append(sequence)

    for structure_id, sequences in structure_members.items():
        for member_index, sequence in enumerate(sequences):
            chunk = chunks[sequence]
            chunk["continuesFrom"] = (
                f"{structure_id}:{member_index - 1}" if member_index > 0 else None
            )
            chunk["continuesTo"] = (
                f"{structure_id}:{member_index + 1}"
                if member_index + 1 < len(sequences)
                else None
            )
    return chunks


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
