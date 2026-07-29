"""从论文证据块抽取通用 Paper Card，不预设领域方法或关系词表。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from app.prompt_loader import load_prompt
from app.services.literature_map.models import (
    EvidenceReference,
    MapClaim,
    PaperCardDraft,
    PaperExtractionResult,
    RelationCandidate,
)
from app.services.literature_map.policy import LiteratureMapExtractionPolicy
from app.services.literature_map.versioning import stable_map_id
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT


CompletionCallable = Callable[..., str]


class LiteratureMapExtractor:
    """模型只提出候选；本类负责证据引用、逐字引文和实体边界校验。"""

    SYSTEM_PROMPT = load_prompt("literature_map/extractor.zh.md")

    def __init__(
        self,
        *,
        completion: CompletionCallable,
        model: dict[str, Any],
        extractor_version: str,
        timeout: int,
        policy: LiteratureMapExtractionPolicy | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.completion = completion
        self.model = model
        self.extractor_version = str(extractor_version or "").strip()
        self.timeout = timeout
        self.policy = policy or LiteratureMapExtractionPolicy()
        self.system_prompt = str(system_prompt or self.SYSTEM_PROMPT).strip()
        if not self.extractor_version:
            raise ValueError("extractor_version 不能为空")
        if not self.system_prompt:
            raise ValueError("system_prompt 不能为空")

    def extract(
        self,
        paper: dict[str, Any],
        evidence_chunks: list[dict[str, Any]],
    ) -> PaperExtractionResult:
        paper_id = str(paper.get("id") or paper.get("recordId") or "").strip()
        if not paper_id:
            raise ValueError("论文缺少稳定 paper_id")
        evidence_payload, evidence_index = self._prepare_evidence(
            paper_id,
            evidence_chunks,
        )
        if not evidence_payload:
            raise ValueError("没有可用于构建文献地图的论文正文证据")
        raw_response = self.completion(
            self.model,
            [
                {
                    "role": "system",
                    "content": f"{self.system_prompt}\n\n{SYSTEM_SECURITY_CONSTRAINT}",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "paper": {
                                "paper_id": paper_id,
                                "title": str(paper.get("title") or ""),
                                "year": str(paper.get("year") or ""),
                                "abstract": str(paper.get("abstract") or "")[
                                    : self.policy.max_abstract_chars
                                ],
                            },
                            "evidence": evidence_payload,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            timeout=self.timeout,
            response_format={"type": "json_object"},
        )
        payload = self._parse_response(raw_response)
        draft, diagnostics = self._normalize_payload(
            paper_id,
            payload,
            evidence_index,
        )
        diagnostics["rawResponseLength"] = len(str(raw_response or ""))
        return PaperExtractionResult(
            draft=draft,
            diagnostics=diagnostics,
            raw_response=str(raw_response or ""),
        )

    def _prepare_evidence(
        self,
        paper_id: str,
        evidence_chunks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        payload: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for raw in evidence_chunks:
            if not isinstance(raw, dict):
                continue
            record_id = str(
                raw.get("record_id") or raw.get("recordId") or paper_id
            ).strip()
            if record_id != paper_id:
                continue
            chunk_index = int(raw.get("chunk_index") or raw.get("chunkIndex") or 0)
            text = str(raw.get("text") or raw.get("content") or "").strip()
            origin_type = str(
                raw.get("origin_type") or raw.get("originType") or "paper_text"
            ).strip()
            if not text or origin_type not in self.policy.allowed_origin_types:
                continue
            reference = f"{record_id}:{chunk_index}"
            item = {
                "ref": reference,
                "section": str(raw.get("section") or "")[
                    : self.policy.max_section_chars
                ],
                "text": text[: self.policy.max_evidence_chars],
                "origin_type": origin_type,
                "extraction_confidence": max(
                    0.0,
                    min(
                        float(
                            raw.get("extraction_confidence")
                            or raw.get("extractionConfidence")
                            or 1
                        ),
                        1.0,
                    ),
                ),
            }
            payload.append(item)
            index[reference] = item
        return payload, index

    def _normalize_payload(
        self,
        paper_id: str,
        payload: dict[str, Any],
        evidence_index: dict[str, dict[str, Any]],
    ) -> tuple[PaperCardDraft, dict[str, Any]]:
        facets: dict[str, list[str]] = {}
        for item in payload.get("facets") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:200]
            values = list(
                dict.fromkeys(
                    value
                    for raw in item.get("values") or []
                    if (value := str(raw or "").strip()[:1000])
                )
            )
            if name and values:
                facets[name] = values

        claims: list[MapClaim] = []
        rejected_claims = 0
        for item in payload.get("claims") or []:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            predicate = str(item.get("predicate") or "").strip()
            object_value = str(item.get("object") or "").strip()
            evidence_refs = self._validated_evidence_refs(
                item.get("evidence"),
                evidence_index,
            )
            if not subject or not predicate or not object_value or not evidence_refs:
                rejected_claims += 1
                continue
            claim_id = stable_map_id(
                "claim",
                paper_id,
                str(item.get("kind") or ""),
                subject,
                predicate,
                object_value,
            )
            claims.append(
                MapClaim(
                    id=claim_id,
                    paper_id=paper_id,
                    kind=str(item.get("kind") or "statement").strip()[:120],
                    subject=subject[:1000],
                    predicate=predicate[:300],
                    object=object_value[:2000],
                    qualifiers=(
                        dict(item.get("qualifiers"))
                        if isinstance(item.get("qualifiers"), dict)
                        else {}
                    ),
                    attribution_type=str(
                        item.get("attribution_type")
                        or item.get("attributionType")
                        or "document_statement"
                    ).strip()[:120],
                    confidence=self._confidence(item.get("confidence")),
                    evidence_refs=evidence_refs,
                )
            )

        relation_candidates: list[RelationCandidate] = []
        rejected_relations = 0
        for item in payload.get("relation_candidates") or payload.get(
            "relationCandidates"
        ) or []:
            if not isinstance(item, dict):
                continue
            relation_type = str(
                item.get("relation_type") or item.get("relationType") or ""
            ).strip()
            target_label = str(
                item.get("target_label") or item.get("targetLabel") or ""
            ).strip()
            evidence_refs = self._validated_evidence_refs(
                item.get("evidence"),
                evidence_index,
            )
            if not relation_type or not target_label or not evidence_refs:
                rejected_relations += 1
                continue
            relation_candidates.append(
                RelationCandidate(
                    relation_type=relation_type[:300],
                    target_label=target_label[:1000],
                    target_paper_id=str(
                        item.get("target_paper_id")
                        or item.get("targetPaperId")
                        or ""
                    ).strip()[:300],
                    target_type=str(
                        item.get("target_type")
                        or item.get("targetType")
                        or "unresolved_label"
                    ).strip()[:120],
                    qualifiers=(
                        dict(item.get("qualifiers"))
                        if isinstance(item.get("qualifiers"), dict)
                        else {}
                    ),
                    confidence=self._confidence(item.get("confidence")),
                    evidence_refs=evidence_refs,
                )
            )

        return (
            PaperCardDraft(
                summary=str(payload.get("summary") or "").strip()[
                    : self.policy.max_summary_chars
                ],
                source_language=str(
                    payload.get("source_language")
                    or payload.get("sourceLanguage")
                    or ""
                ).strip()[:40],
                facets=facets,
                claims=claims,
                relation_candidates=relation_candidates,
            ),
            {
                "acceptedClaimCount": len(claims),
                "rejectedClaimCount": rejected_claims,
                "acceptedRelationCount": len(relation_candidates),
                "rejectedRelationCount": rejected_relations,
            },
        )

    def _validated_evidence_refs(
        self,
        raw_values: Any,
        evidence_index: dict[str, dict[str, Any]],
    ) -> list[EvidenceReference]:
        result: list[EvidenceReference] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_values if isinstance(raw_values, list) else []:
            if not isinstance(raw, dict):
                continue
            reference = str(raw.get("ref") or "").strip()
            quote = str(raw.get("quote") or "").strip()
            source = evidence_index.get(reference)
            if not source or not quote:
                continue
            if self._normalize_text(quote) not in self._normalize_text(source["text"]):
                continue
            key = (reference, quote)
            if key in seen:
                continue
            seen.add(key)
            record_id, chunk_index = reference.rsplit(":", 1)
            result.append(
                EvidenceReference(
                    record_id=record_id,
                    chunk_index=int(chunk_index),
                    quote=quote[: self.policy.max_quote_chars],
                    section=str(source.get("section") or ""),
                    origin_type=str(source.get("origin_type") or "paper_text"),
                    extraction_confidence=float(
                        source.get("extraction_confidence") or 0
                    ),
                )
            )
        return result

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return max(0.0, min(float(value or 0), 1.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_response(raw_response: str) -> dict[str, Any]:
        text = str(raw_response or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else text
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("文献地图抽取器未返回合法 JSON")
            payload = json.loads(text[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("文献地图抽取结果必须是 JSON 对象")
        return payload


__all__ = ["LiteratureMapExtractor"]
