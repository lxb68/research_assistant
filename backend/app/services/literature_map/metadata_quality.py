"""论文元数据的通用质量门禁，不猜测替换值。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class PaperMetadataValidator:
    version: str = "metadata-v1"
    minimum_year: int = 1600
    future_year_tolerance: int = 1
    maximum_venue_chars: int = 240

    def validate(self, paper: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        year = str(paper.get("year") or "").strip()
        current_year = datetime.now(timezone.utc).year
        if year and (
            not re.fullmatch(r"\d{4}", year)
            or not self.minimum_year <= int(year) <= current_year + self.future_year_tolerance
        ):
            warnings.append("year_out_of_range")
        venue = str(paper.get("venue") or "").strip()
        if len(venue) > self.maximum_venue_chars or "\n" in venue:
            warnings.append("venue_looks_like_body_text")
        elif venue and venue[0].islower():
            warnings.append("venue_may_be_truncated")
        doi = str(paper.get("doi") or "").strip()
        if doi and not re.fullmatch(r"10\.\d{4,9}/\S+", doi, flags=re.IGNORECASE):
            warnings.append("doi_format_invalid")
        if not str(paper.get("title") or "").strip():
            warnings.append("title_missing")
        return warnings


__all__ = ["PaperMetadataValidator"]
