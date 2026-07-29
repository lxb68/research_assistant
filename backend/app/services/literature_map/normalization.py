"""不绑定领域词表的可配置词汇规范化。"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.services.literature_map.models import MapClaim


def lexical_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    normalized = re.sub(r"[\s\-/]+", "_", normalized)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized).strip("_")


@dataclass(frozen=True, slots=True)
class NormalizedValue:
    raw: str
    canonical: str
    confidence: float
    version: str


@dataclass(slots=True)
class VocabularyNormalizer:
    """基础词法归一化始终可用，语义别名从配置注入。"""

    version: str = "lexical-v1"
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path | None) -> "VocabularyNormalizer":
        if not path:
            return cls()
        config_path = Path(path)
        if not config_path.is_file():
            return cls()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        aliases = payload.get("aliases") if isinstance(payload, dict) else {}
        return cls(
            version=str(payload.get("version") or "configured-v1"),
            aliases={
                str(category): {
                    lexical_key(raw): str(canonical)
                    for raw, canonical in values.items()
                    if str(raw).strip() and str(canonical).strip()
                }
                for category, values in aliases.items()
                if isinstance(values, dict)
            },
        )

    def normalize(self, category: str, value: str) -> NormalizedValue:
        raw = str(value or "").strip()
        key = lexical_key(raw)
        configured = self.aliases.get(category, {}).get(key)
        return NormalizedValue(
            raw=raw,
            canonical=configured or key or raw,
            confidence=1.0 if configured else (0.8 if key else 0.0),
            version=self.version,
        )

    def normalize_facets(
        self,
        facets: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for raw_name, values in facets.items():
            name = self.normalize("facet", raw_name).canonical
            if not name:
                continue
            bucket = result.setdefault(name, [])
            for value in values:
                normalized = str(value or "").strip()
                if normalized and normalized not in bucket:
                    bucket.append(normalized)
        return result

    def normalize_claim(self, claim: MapClaim) -> MapClaim:
        normalized = self.normalize("claim_kind", claim.raw_kind or claim.kind)
        evidence_score = min(1.0, len(claim.evidence_refs) / 2)
        model_confidence = float(
            claim.qualifiers.get("modelConfidence", claim.confidence)
        )
        # 同时考虑模型判断与证据覆盖，避免直接展示未经校准的高分。
        calibrated_confidence = min(
            model_confidence,
            0.70 + 0.25 * evidence_score,
        )
        return replace(
            claim,
            kind=normalized.canonical,
            raw_kind=normalized.raw,
            canonical_kind=normalized.canonical,
            normalizer_version=self.version,
            normalization_confidence=normalized.confidence,
            confidence=calibrated_confidence,
            qualifiers={
                **claim.qualifiers,
                "modelConfidence": model_confidence,
            },
        )


__all__ = ["NormalizedValue", "VocabularyNormalizer", "lexical_key"]
