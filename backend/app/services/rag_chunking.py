"""把 MinerU 基础块加工为适合稀疏检索和向量检索的 RAG 分块。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


_TOKEN_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9]+(?:[+._/\-][A-Za-z0-9]+)*|[^\s]"
)
_SENTENCE_PATTERN = re.compile(r".+?(?:[。！？!?；;](?=\s|$)|\.(?=\s|$)|$)", re.S)
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(slots=True)
class BaseMarkdownBlock:
    """表示 MinerU 生成、尚未针对检索再次优化的基础块。"""

    content: str
    index: int
    headings: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    semantic_category: str = "body"
    semantic_type: str = "prose"
    structure_id: str = ""
    structure_part_index: int = 0
    structure_part_count: int = 0
    continues_from: str | None = None
    continues_to: str | None = None


@dataclass(slots=True)
class PreparedRAGChunk:
    """表示完成 Token 控制和重叠处理的最终检索块。"""

    text: str
    section: str
    token_count: int
    base_chunk_indices: list[int]
    overlap_token_count: int = 0
    summary: str = ""
    semantic_type: str = "prose"
    structure_id: str = ""
    structure_sequence: int = 0
    continues_from: str | None = None
    continues_to: str | None = None
    is_structure_start: bool = False
    is_structure_end: bool = False


@dataclass(slots=True)
class _SectionGroup:
    """保存同一标题路径下连续基础块合并后的中间结果。"""

    section: str
    contents: list[tuple[str, BaseMarkdownBlock]]
    summaries: list[str]
    structure_id: str = ""
    semantic_type: str = "prose"


@dataclass(slots=True)
class _SemanticUnit:
    """保存一个语义单元文本及其对应的 MinerU 基础块索引。"""

    text: str
    base_chunk_indices: set[int]
    semantic_type: str = "prose"
    structure_id: str = ""


class MarkdownRAGChunker:
    """依次执行 Markdown 规范化、标题解析、语义分割、Token 切分和重叠。"""

    def __init__(
        self,
        *,
        target_tokens: int = 500,
        max_tokens: int = 700,
        overlap_tokens: int = 80,
    ) -> None:
        """初始化目标、硬上限和相邻块重叠 Token 数。"""
        self.max_tokens = max(1, int(max_tokens))
        self.target_tokens = max(1, min(int(target_tokens), self.max_tokens))
        self.overlap_tokens = max(0, min(int(overlap_tokens), self.max_tokens // 3))

    def build(
        self,
        blocks: Iterable[BaseMarkdownBlock],
        *,
        outline: list[dict[str, Any]] | None = None,
    ) -> list[PreparedRAGChunk]:
        """从基础块构建标题路径明确、大小受控且带重叠的最终检索块。"""
        normalized_blocks = [
            BaseMarkdownBlock(
                content=self.normalize_markdown(block.content),
                index=block.index,
                headings=block.headings,
                summary=re.sub(r"\s+", " ", block.summary).strip(),
                semantic_category=block.semantic_category,
                semantic_type=block.semantic_type or "prose",
                structure_id=block.structure_id,
                structure_part_index=block.structure_part_index,
                structure_part_count=block.structure_part_count,
                continues_from=block.continues_from,
                continues_to=block.continues_to,
            )
            for block in blocks
            if str(block.content or "").strip()
        ]
        groups = self._group_by_heading_path(normalized_blocks, outline or [])
        chunks: list[PreparedRAGChunk] = []
        for group in groups:
            units = [
                _SemanticUnit(
                    text=unit,
                    base_chunk_indices={block.index},
                    semantic_type=group.semantic_type,
                    structure_id=group.structure_id,
                )
                for content, block in group.contents
                for unit in self._semantic_units(content)
            ]
            chunks.extend(self._pack_group(group, units))
        return self._finalize_structure_continuity(chunks)

    def normalize_markdown(self, text: str) -> str:
        """统一换行、行尾空格和多余空行，同时保留 Markdown 结构。"""
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in normalized.split("\n")]
        output: list[str] = []
        blank_pending = False
        fenced = False
        for line in lines:
            if line.lstrip().startswith("```"):
                fenced = not fenced
            if not fenced and not line.strip():
                blank_pending = bool(output)
                continue
            if blank_pending:
                output.append("")
                blank_pending = False
            output.append(line)
        return "\n".join(output).strip()

    def estimate_tokens(self, text: str) -> int:
        """按中文字符、英文子词和标点估算通用嵌入模型 Token 数。"""
        count = 0
        for match in _TOKEN_PATTERN.finditer(str(text or "")):
            count += self._token_weight(match.group(0))
        return count

    def _group_by_heading_path(
        self,
        blocks: list[BaseMarkdownBlock],
        outline: list[dict[str, Any]],
    ) -> list[_SectionGroup]:
        """按标题层次合并同一标题路径下相邻的 MinerU 基础块。"""
        groups: list[_SectionGroup] = []
        for block in blocks:
            section = self._resolve_heading_path(block, outline)
            content = self._strip_repeated_heading(block.content, section)
            if not content:
                continue
            structure_id = str(block.structure_id or "")
            semantic_type = str(block.semantic_type or "prose")
            if (
                groups
                and groups[-1].section == section
                and groups[-1].structure_id == structure_id
                and groups[-1].semantic_type == semantic_type
            ):
                groups[-1].contents.append((content, block))
                if block.summary:
                    groups[-1].summaries.append(block.summary)
                continue
            groups.append(
                _SectionGroup(
                    section=section,
                    contents=[(content, block)],
                    summaries=[block.summary] if block.summary else [],
                    structure_id=structure_id,
                    semantic_type=semantic_type,
                )
            )
        return groups

    def _resolve_heading_path(self, block: BaseMarkdownBlock, outline: list[dict[str, Any]]) -> str:
        """根据大纲位置恢复 `一级 > 二级 > 三级` 标题路径。"""
        raw_headings = [item for item in block.headings if isinstance(item, dict)]
        positions = [int(item.get("position") or 0) for item in raw_headings if int(item.get("position") or 0) > 0]
        anchor = min(positions) if positions else 0
        stack: list[tuple[int, str]] = []
        if anchor and outline:
            ordered_outline = sorted(
                (item for item in outline if isinstance(item, dict)),
                key=lambda item: int(item.get("position") or 0),
            )
            for item in ordered_outline:
                position = int(item.get("position") or 0)
                if position > anchor:
                    break
                title = str(item.get("title") or item.get("heading") or "").strip()
                level = max(1, min(int(item.get("level") or 1), 6))
                if not title:
                    continue
                stack = [(existing_level, value) for existing_level, value in stack if existing_level < level]
                stack.append((level, title))

        if not stack:
            for item in sorted(raw_headings, key=lambda value: (int(value.get("position") or 0), int(value.get("level") or 1))):
                title = str(item.get("heading") or item.get("title") or "").strip()
                level = max(1, min(int(item.get("level") or 1), 6))
                if not title:
                    continue
                stack = [(existing_level, value) for existing_level, value in stack if existing_level < level]
                stack.append((level, title))

        values: list[str] = []
        for _, title in stack:
            if title not in values:
                values.append(title)
        return " > ".join(values) or "正文"

    def _strip_repeated_heading(self, content: str, section: str) -> str:
        """移除基础块开头已由元数据表达的重复 Markdown 标题。"""
        lines = content.splitlines()
        section_titles = {item.strip() for item in section.split(">") if item.strip()}
        while lines:
            match = _HEADING_PATTERN.match(lines[0].strip())
            if not match or match.group(2).strip() not in section_titles:
                break
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
        return "\n".join(lines).strip()

    def _semantic_units(self, text: str) -> list[str]:
        """优先按 Markdown 段落分割，超长段落再按句子或 Token 边界切分。"""
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n+", text) if item.strip()]
        units: list[str] = []
        for paragraph in paragraphs:
            if self.estimate_tokens(paragraph) <= self.max_tokens:
                units.append(paragraph)
                continue
            sentences = [match.group(0).strip() for match in _SENTENCE_PATTERN.finditer(paragraph) if match.group(0).strip()]
            if len(sentences) <= 1:
                units.extend(self._hard_token_split(paragraph, self.max_tokens))
                continue
            current: list[str] = []
            for sentence in sentences:
                if self.estimate_tokens(sentence) > self.max_tokens:
                    if current:
                        units.append(" ".join(current).strip())
                        current = []
                    units.extend(self._hard_token_split(sentence, self.max_tokens))
                    continue
                candidate = " ".join([*current, sentence]).strip()
                if current and self.estimate_tokens(candidate) > self.max_tokens:
                    units.append(" ".join(current).strip())
                    current = [sentence]
                else:
                    current.append(sentence)
            if current:
                units.append(" ".join(current).strip())
        return units

    def _pack_group(self, group: _SectionGroup, units: list[_SemanticUnit]) -> list[PreparedRAGChunk]:
        """按 Token 目标装箱，并把上一块尾部作为下一块的受控重叠。"""
        if not units:
            return []
        packed_units: list[list[_SemanticUnit]] = []
        current: list[_SemanticUnit] = []
        for unit in units:
            candidate = "\n\n".join(item.text for item in [*current, unit])
            if current and (
                self.estimate_tokens(candidate) > self.max_tokens
                or self.estimate_tokens("\n\n".join(item.text for item in current)) >= self.target_tokens
            ):
                packed_units.append(current)
                # 连续结构使用显式 continuesFrom/continuesTo 表达关系，避免重复算法步骤、
                # 表格行或代码行；普通文本仍沿用受控 Token 重叠。
                overlap = [] if group.structure_id else self._tail_overlap(current)
                current = [*overlap, unit]
                while len(current) > 1 and self.estimate_tokens("\n\n".join(item.text for item in current)) > self.max_tokens:
                    current.pop(0)
                continue
            current.append(unit)
        if current:
            packed_units.append(current)

        results: list[PreparedRAGChunk] = []
        previous_text = ""
        summary = "；".join(dict.fromkeys(group.summaries))[:500]
        for packed in packed_units:
            text = "\n\n".join(item.text for item in packed).strip()
            overlap_count = self._shared_prefix_tokens(previous_text, text) if previous_text else 0
            base_chunk_indices = sorted(
                {
                    base_index
                    for item in packed
                    for base_index in item.base_chunk_indices
                }
            )
            results.append(
                PreparedRAGChunk(
                    text=text,
                    section=group.section,
                    token_count=self.estimate_tokens(text),
                    base_chunk_indices=base_chunk_indices,
                    overlap_token_count=overlap_count,
                    summary=summary,
                    semantic_type=group.semantic_type,
                    structure_id=group.structure_id,
                )
            )
            previous_text = text
        return results

    def _finalize_structure_continuity(
        self,
        chunks: list[PreparedRAGChunk],
    ) -> list[PreparedRAGChunk]:
        """按最终 RAG 分片重新计算结构序号与连续引用。"""
        members: dict[str, list[PreparedRAGChunk]] = {}
        for chunk in chunks:
            if chunk.structure_id:
                members.setdefault(chunk.structure_id, []).append(chunk)

        for structure_id, structure_chunks in members.items():
            total = len(structure_chunks)
            for sequence, chunk in enumerate(structure_chunks):
                chunk.structure_sequence = sequence
                chunk.continues_from = f"{structure_id}:{sequence - 1}" if sequence > 0 else None
                chunk.continues_to = f"{structure_id}:{sequence + 1}" if sequence + 1 < total else None
                chunk.is_structure_start = sequence == 0
                chunk.is_structure_end = sequence + 1 == total
        return chunks

    def _tail_overlap(self, units: list[_SemanticUnit]) -> list[_SemanticUnit]:
        """选择上一块末尾不超过重叠预算的完整语义单元。"""
        if self.overlap_tokens <= 0:
            return []
        selected: list[_SemanticUnit] = []
        for unit in reversed(units):
            candidate = [unit, *selected]
            if self.estimate_tokens("\n\n".join(item.text for item in candidate)) > self.overlap_tokens:
                if not selected:
                    tail = self._hard_token_split(unit.text, self.overlap_tokens, from_end=True)
                    return [
                        _SemanticUnit(text=tail[-1], base_chunk_indices=set(unit.base_chunk_indices))
                    ] if tail else []
                break
            selected = candidate
        return selected

    def _hard_token_split(self, text: str, limit: int, *, from_end: bool = False) -> list[str]:
        """在没有语义边界时按估算 Token 的字符位置强制切分。"""
        matches = list(_TOKEN_PATTERN.finditer(text))
        if not matches:
            return [text.strip()] if text.strip() else []
        pieces: list[str] = []
        current_start: int | None = None
        current_end = 0
        current_weight = 0
        for match in matches:
            token = match.group(0)
            weight = self._token_weight(token)
            if weight > limit:
                if current_start is not None:
                    pieces.append(text[current_start:current_end].strip())
                    current_start = None
                    current_weight = 0
                character_limit = max(1, limit * 4)
                pieces.extend(
                    token[start : start + character_limit]
                    for start in range(0, len(token), character_limit)
                    if token[start : start + character_limit]
                )
                continue
            if current_start is not None and current_weight + weight > limit:
                pieces.append(text[current_start:current_end].strip())
                current_start = match.start()
                current_weight = 0
            if current_start is None:
                current_start = match.start()
            current_end = match.end()
            current_weight += weight
        if current_start is not None:
            pieces.append(text[current_start:current_end].strip())
        pieces = [piece for piece in pieces if piece]
        return pieces[-1:] if from_end else pieces

    def _token_weight(self, token: str) -> int:
        """估算单个中文字符、英文词或标点对应的 Token 权重。"""
        if re.fullmatch(r"[A-Za-z0-9]+(?:[+._/\-][A-Za-z0-9]+)*", token):
            return max(1, math.ceil(len(token) / 4))
        return 1

    def _shared_prefix_tokens(self, previous: str, current: str) -> int:
        """估算当前块开头与上一块结尾实际重复的 Token 数。"""
        previous_tokens = [match.group(0) for match in _TOKEN_PATTERN.finditer(previous)]
        current_tokens = [match.group(0) for match in _TOKEN_PATTERN.finditer(current)]
        maximum = min(len(previous_tokens), len(current_tokens), self.overlap_tokens)
        for size in range(maximum, 0, -1):
            if previous_tokens[-size:] == current_tokens[:size]:
                return size
        return 0


__all__ = ["BaseMarkdownBlock", "MarkdownRAGChunker", "PreparedRAGChunk"]
