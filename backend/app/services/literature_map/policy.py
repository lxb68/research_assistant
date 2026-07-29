"""文献地图抽取的可注入策略，集中管理来源与文本预算。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LiteratureMapExtractionPolicy:
    """约束可采信证据，而不绑定任何研究领域的分类或关系词表。"""

    allowed_origin_types: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"paper_text", "table", "equation", "figure_caption"}
        )
    )
    max_abstract_chars: int = 4000
    max_evidence_chars: int = 5000
    max_section_chars: int = 1000
    max_quote_chars: int = 2000
    max_summary_chars: int = 4000

    def __post_init__(self) -> None:
        normalized = frozenset(
            str(value or "").strip()
            for value in self.allowed_origin_types
            if str(value or "").strip()
        )
        if not normalized:
            raise ValueError("allowed_origin_types 不能为空")
        object.__setattr__(self, "allowed_origin_types", normalized)
        for field_name in (
            "max_abstract_chars",
            "max_evidence_chars",
            "max_section_chars",
            "max_quote_chars",
            "max_summary_chars",
        ):
            if int(getattr(self, field_name)) <= 0:
                raise ValueError(f"{field_name} 必须大于 0")


__all__ = ["LiteratureMapExtractionPolicy"]
