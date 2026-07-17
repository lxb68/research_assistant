"""把论文源文件转换为稳定、连续的结构化检索单元。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.services.rag_chunking import BaseMarkdownBlock, MarkdownRAGChunker
from app.services.split import parse_markdown_sections


_EXCLUDED_CATEGORIES = {
    "references", "reference", "bibliography", "acknowledgements", "acknowledgments",
    "funding", "funding_statement", "conflict_of_interest", "competing_interests",
    "venue_description", "journal_description", "conference_description",
}
_EXCLUDED_SECTION_PATTERNS = (
    "references", "bibliography", "acknowledgement", "acknowledgment", "funding",
    "fund support", "conflict of interest", "competing interest", "declaration of interest",
    "journal description", "about the journal", "conference description", "about the conference",
    "参考文献", "引用文献", "致谢", "基金支持", "基金项目", "利益冲突", "竞争性利益",
    "期刊说明", "期刊简介", "会议说明", "会议简介",
)


class DocumentStructureIndexer:
    """只负责读取论文结构并建立连续分块，不执行相关性排序。"""

    def __init__(self, *, target_tokens: int, max_tokens: int, overlap_tokens: int) -> None:
        self.chunker = MarkdownRAGChunker(
            target_tokens=target_tokens,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )

    def index_paper(self, paper: dict[str, Any], *, chunk_factory: Callable[..., Any]) -> list[Any]:
        base = {
            "record_id": str(paper.get("id") or paper.get("recordId") or ""),
            "title": str(paper.get("title") or "未命名文献"),
            "year": str(paper.get("year") or ""),
            "source": str(paper.get("source") or ""),
            "url": str(paper.get("url") or ""),
        }
        split_chunks = paper.get("splitChunks") or paper.get("split_chunks")
        if isinstance(split_chunks, list):
            blocks: list[BaseMarkdownBlock] = []
            for index, raw in enumerate(split_chunks):
                if not isinstance(raw, dict):
                    continue
                content = str(raw.get("content") or "").strip()
                if not content:
                    continue
                headings = raw.get("headings") if isinstance(raw.get("headings"), list) else []
                section = " > ".join(str(h.get("heading") or "").strip() for h in headings if isinstance(h, dict) and str(h.get("heading") or "").strip())
                summary = str(raw.get("summary") or "").strip()
                category = str(raw.get("semanticCategory") or raw.get("semantic_category") or "").strip().lower()
                semantic_type = str(raw.get("semanticType") or raw.get("semantic_type") or "prose").strip().lower()
                if self.is_excluded(category=category, section=section, text=f"{summary}\n{content}"):
                    continue
                blocks.append(BaseMarkdownBlock(
                    content=content,
                    index=index,
                    headings=headings,
                    summary=summary,
                    semantic_category=category or "body",
                    semantic_type=semantic_type or "prose",
                    structure_id=str(raw.get("structureId") or raw.get("structure_id") or "").strip(),
                    structure_part_index=int(raw.get("structurePartIndex") or raw.get("structure_part_index") or 0),
                    structure_part_count=int(raw.get("structurePartCount") or raw.get("structure_part_count") or 0),
                    continues_from=raw.get("continuesFrom") or raw.get("continues_from"),
                    continues_to=raw.get("continuesTo") or raw.get("continues_to"),
                ))
            if blocks:
                outline = paper.get("splitOutline") or paper.get("split_outline") or []
                return self._materialize(base, self.chunker.build(blocks, outline=outline if isinstance(outline, list) else []), chunk_factory)

        content = self.read_markdown(paper) or str(paper.get("abstract") or "").strip()
        outline, sections = parse_markdown_sections(content)
        blocks = []
        for index, section_data in enumerate(sections):
            section_content = str(section_data.get("content") or "").strip()
            heading = str(section_data.get("heading") or "").strip()
            if not section_content or self.is_excluded(category="", section=heading, text=section_content):
                continue
            blocks.append(BaseMarkdownBlock(
                content=section_content,
                index=index,
                headings=[{"heading": heading, "level": int(section_data.get("level") or 1), "position": int(section_data.get("position") or index + 1)}] if heading else [],
            ))
        return self._materialize(base, self.chunker.build(blocks, outline=outline), chunk_factory)

    @staticmethod
    def _materialize(base: dict[str, Any], prepared: list[Any], chunk_factory: Callable[..., Any]) -> list[Any]:
        return [chunk_factory(
            **base,
            text=item.text,
            score=0,
            section=item.section,
            chunk_index=index,
            token_count=item.token_count,
            overlap_token_count=item.overlap_token_count,
            base_chunk_indices=item.base_chunk_indices,
            summary=item.summary,
            semantic_type=item.semantic_type,
            structure_id=item.structure_id,
            structure_sequence=item.structure_sequence,
            continues_from=item.continues_from,
            continues_to=item.continues_to,
            is_structure_start=item.is_structure_start,
            is_structure_end=item.is_structure_end,
        ) for index, item in enumerate(prepared)]

    @staticmethod
    def read_markdown(paper: dict[str, Any]) -> str:
        for key in ("markdownPath", "markdown_path"):
            value = str(paper.get(key) or "").strip()
            if value:
                path = Path(value)
                if path.exists() and path.is_file():
                    return path.read_text(encoding="utf-8", errors="ignore")
        output_dir = str(paper.get("markdownOutputDir") or paper.get("markdown_output_dir") or "").strip()
        path = Path(output_dir) / "full.md" if output_dir else None
        return path.read_text(encoding="utf-8", errors="ignore") if path and path.exists() and path.is_file() else ""

    @staticmethod
    def is_excluded(*, category: str, section: str, text: str) -> bool:
        if category.replace("-", "_").replace(" ", "_") in _EXCLUDED_CATEGORIES:
            return True
        normalized_section = section.lower().strip()
        if normalized_section and any(pattern in normalized_section for pattern in _EXCLUDED_SECTION_PATTERNS):
            return True
        first_line = next((line.strip(" #\t:：.-").lower() for line in text.splitlines() if line.strip()), "")
        return any(first_line.startswith(pattern) for pattern in _EXCLUDED_SECTION_PATTERNS)


__all__ = ["DocumentStructureIndexer"]
