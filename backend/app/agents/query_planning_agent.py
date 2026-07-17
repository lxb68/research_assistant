"""把上下文研究问题规划为受约束、可验证的动态检索任务。"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.core.config import settings
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT
from app.services.retrieval_contracts import (
    SECTION_TYPES,
    normalize_execution_complexity,
    normalize_requirement,
    normalize_section_types,
)


CompletionCallable = Callable[..., str]


class QueryPlanningAgent:
    """只负责指代解析、复杂度判断和检索 facet 规划，不负责回答问题。"""

    SYSTEM_PROMPT = """你是研究检索查询规划器。根据当前问题、最近对话和候选来源，生成结构化检索计划。

要求：
1. standalone_question 必须脱离历史后仍语义完整，不得机械拼接无关历史。
2. target_paper_ids 和 target_chunks 只能使用 candidate_sources 或 explicit_paper_ids 中真实存在的值。
   target_chunks 只用于用户明确追问某个既有片段、引用或局部内容；当用户询问整篇论文、全文或宽范围主题时，保留 target_paper_ids，但 target_chunks 必须为空，避免旧摘要片段挤占全文检索结果。
3. 无法唯一解析“它、前者、这个片段”等指代时，needs_clarification=true。
4. question_type 从 simple_fact、mechanism、comparison、evaluation、synthesis 中选择。
5. complexity 从 simple、complex 中选择。单一事实查询通常为 simple；机制、比较、综合、多维分析通常为 complex。
6. complex 问题应动态拆成 2 至 5 个互补 retrieval_facets。每个 facet 描述一个回答所需的信息缺口，不能针对某篇固定论文套用预设关键词。
7. preferred_section_types 使用通用语义类型，例如 abstract、introduction、contribution、method、framework、experiment、result、conclusion。
8. 必须保持用户原问题的粒度，不得把“介绍、怎么做、主要流程”等概述问题擅自扩大成完整协议复现、精确通信轮次或全部安全性证明。
9. core_requirements 只列出回答用户原问题不可缺少的要点，并为每项声明 evidence_intent、preferred_section_types 和 minimum_direct_evidence；optional_details 可列出有则更好的深入细节。可选细节缺失不能导致整个问题不可回答。
10. 不要回答用户问题，不要调用工具，不要输出 Markdown 或额外文字。

只输出一个 JSON 对象：
{
  "standalone_question":"...",
  "question_type":"simple_fact|mechanism|comparison|evaluation|synthesis",
  "complexity":"simple|complex",
  "target_paper_ids":[],
  "target_chunks":[{"record_id":"...","chunk_index":0}],
  "retrieval_facets":[{"id":"facet-1","goal":"...","query":"...","concepts":[],"phrases":[],"preferred_section_types":[]}],
  "core_requirements":[{"id":"req-1","description":"...","evidence_intent":"fact|mechanism|comparison|evaluation|synthesis","preferred_section_types":[],"minimum_direct_evidence":1}],
  "optional_details":[],
  "needs_clarification":false,
  "clarification_question":""
}
"""

    ALLOWED_QUESTION_TYPES = {"simple_fact", "mechanism", "comparison", "evaluation", "synthesis"}
    ALLOWED_COMPLEXITIES = {"simple", "complex"}
    ALLOWED_SECTION_TYPES = SECTION_TYPES

    def __init__(
        self,
        *,
        completion: CompletionCallable,
        model: dict[str, Any],
        timeout: int,
        max_facets: int | None = None,
    ) -> None:
        self.completion = completion
        self.model = model
        self.timeout = timeout
        self.max_facets = max(1, min(int(max_facets or settings.query_planner_max_facets), 8))

    def plan(
        self,
        question: str,
        history: list[dict[str, Any]] | None,
        *,
        explicit_paper_ids: list[str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """生成规划并严格校验所有模型提供的来源引用。"""
        normalized_question = str(question or "").strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")

        context_messages, candidate_sources = self._normalize_context(history or [])
        planner_input = {
            "current_question": normalized_question,
            "history": context_messages,
            "candidate_sources": candidate_sources,
            "explicit_paper_ids": list(explicit_paper_ids or []),
        }
        raw_response = self.completion(
            self.model,
            [
                {"role": "system", "content": f"{self.SYSTEM_PROMPT}\n\n{SYSTEM_SECURITY_CONSTRAINT}"},
                {"role": "user", "content": json.dumps(planner_input, ensure_ascii=False)},
            ],
            temperature=0,
            timeout=self.timeout,
            response_format={"type": "json_object"},
        )
        try:
            payload = self._parse_response(raw_response)
            plan = self._normalize_plan(
                payload,
                normalized_question=normalized_question,
                candidate_sources=candidate_sources,
                explicit_paper_ids=explicit_paper_ids or [],
            )
        except Exception as error:
            setattr(error, "raw_response", str(raw_response or ""))
            raise
        return plan, str(raw_response or "")

    def _normalize_context(
        self,
        history: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        context_messages: list[dict[str, Any]] = []
        candidate_sources: list[dict[str, Any]] = []
        seen_sources: set[tuple[str, int]] = set()
        for message in history[-8:]:
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            normalized_sources: list[dict[str, Any]] = []
            for source in list(message.get("sources") or [])[:20]:
                if not isinstance(source, dict):
                    continue
                record_id = str(source.get("record_id") or source.get("recordId") or "").strip()
                if not record_id:
                    continue
                normalized_source = {
                    "index": int(source.get("index") or 0),
                    "record_id": record_id,
                    "title": str(source.get("title") or "")[:1000],
                    "section": str(source.get("section") or "")[:1000],
                    "chunk_index": int(source.get("chunk_index") or source.get("chunkIndex") or 0),
                    "excerpt": str(source.get("excerpt") or "")[:1200],
                }
                normalized_sources.append(normalized_source)
                source_key = (record_id, normalized_source["chunk_index"])
                if source_key not in seen_sources:
                    seen_sources.add(source_key)
                    candidate_sources.append(normalized_source)
            context_messages.append({"role": role, "content": content[:6000], "sources": normalized_sources})
        return context_messages, candidate_sources

    def _normalize_plan(
        self,
        payload: dict[str, Any],
        *,
        normalized_question: str,
        candidate_sources: list[dict[str, Any]],
        explicit_paper_ids: list[str],
    ) -> dict[str, Any]:
        known_ids = {str(source["record_id"]) for source in candidate_sources}
        explicit_ids = {str(record_id) for record_id in explicit_paper_ids if str(record_id)}
        allowed_ids = known_ids | explicit_ids
        proposed_values = payload.get("target_paper_ids")
        proposed_values = proposed_values if isinstance(proposed_values, list) else []
        proposed_ids = [str(value).strip() for value in proposed_values if str(value).strip()]
        target_paper_ids = list(dict.fromkeys(proposed_ids if not explicit_ids else explicit_paper_ids))
        invalid_ids = [record_id for record_id in target_paper_ids if record_id not in allowed_ids]
        target_paper_ids = [record_id for record_id in target_paper_ids if record_id in allowed_ids]

        known_chunks = {(str(source["record_id"]), int(source["chunk_index"])) for source in candidate_sources}
        target_chunks: list[dict[str, Any]] = []
        invalid_chunks: list[dict[str, Any]] = []
        proposed_chunks = payload.get("target_chunks")
        proposed_chunks = proposed_chunks if isinstance(proposed_chunks, list) else []
        for item in proposed_chunks:
            if not isinstance(item, dict):
                continue
            try:
                chunk_index = int(item.get("chunk_index") or 0)
            except (TypeError, ValueError):
                chunk_index = -1
            reference = {"record_id": str(item.get("record_id") or "").strip(), "chunk_index": chunk_index}
            if (reference["record_id"], reference["chunk_index"]) in known_chunks:
                if reference not in target_chunks:
                    target_chunks.append(reference)
            else:
                invalid_chunks.append(reference)
        for reference in target_chunks:
            if reference["record_id"] not in target_paper_ids:
                target_paper_ids.append(reference["record_id"])

        standalone_question = str(payload.get("standalone_question") or normalized_question).strip()
        question_type = str(payload.get("question_type") or "simple_fact").strip().lower()
        if question_type not in self.ALLOWED_QUESTION_TYPES:
            question_type = "simple_fact"
        complexity = str(payload.get("complexity") or "simple").strip().lower()
        if complexity not in self.ALLOWED_COMPLEXITIES:
            complexity = "simple"

        facets: list[dict[str, Any]] = []
        seen_facet_queries: set[str] = set()
        raw_facets = payload.get("retrieval_facets")
        raw_facets = raw_facets if isinstance(raw_facets, list) else []
        for index, item in enumerate(raw_facets[: self.max_facets], start=1):
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or "").strip()
            if not query or query.casefold() in seen_facet_queries:
                continue
            seen_facet_queries.add(query.casefold())
            section_types = normalize_section_types(
                item.get("preferred_section_types") or item.get("preferredSectionTypes") or []
            )
            concepts = item.get("concepts") if isinstance(item.get("concepts"), list) else []
            phrases = item.get("phrases") if isinstance(item.get("phrases"), list) else []
            facets.append(
                {
                    "id": str(item.get("id") or f"facet-{index}")[:80],
                    "goal": str(item.get("goal") or query)[:500],
                    "query": query[:2000],
                    "concepts": [str(value).strip()[:200] for value in concepts if str(value).strip()][:16],
                    "phrases": [str(value).strip()[:300] for value in phrases if str(value).strip()][:12],
                    "preferredSectionTypes": list(dict.fromkeys(section_types)),
                }
            )
        if not facets:
            facets = [{"id": "facet-1", "goal": standalone_question, "query": standalone_question, "preferredSectionTypes": []}]

        requirements = payload.get("core_requirements")
        if not isinstance(requirements, list):
            requirements = payload.get("answer_requirements")
        requirements = requirements if isinstance(requirements, list) else []
        requirement_specs = [
            normalized
            for index, value in enumerate(requirements[:8], start=1)
            if (normalized := normalize_requirement(value, index, question_type=question_type)) is not None
        ]
        core_requirements = [item["description"] for item in requirement_specs if item.get("required")]
        optional_values = payload.get("optional_details")
        optional_values = optional_values if isinstance(optional_values, list) else []
        optional_details = [str(value).strip()[:500] for value in optional_values if str(value).strip()][:8]
        needs_clarification = payload.get("needs_clarification") is True
        if (invalid_ids and not target_paper_ids) or (invalid_chunks and not target_chunks):
            needs_clarification = True
        clarification = str(payload.get("clarification_question") or "").strip()
        if needs_clarification and not clarification:
            clarification = "我无法唯一确定你指的是哪篇文献或哪个片段，请补充论文标题、章节或引用内容。"

        complexity = normalize_execution_complexity(
            complexity,
            question_type=question_type,
            facet_count=len(facets),
            requirement_count=len(core_requirements),
        )
        is_complex = complexity == "complex"
        target_evidence_count = (
            max(settings.orchestrator_min_evidence, settings.rag_complex_target_evidence)
            if is_complex
            else settings.orchestrator_min_evidence
        )
        return {
            "standaloneQuestion": standalone_question,
            "questionType": question_type,
            "complexity": complexity,
            "targetPaperIds": target_paper_ids,
            "targetChunks": target_chunks,
            "retrievalFacets": facets,
            "coreRequirements": core_requirements,
            "requirementSpecs": requirement_specs,
            "optionalDetails": optional_details,
            "answerRequirements": core_requirements,
            "requiresIterativeRetrieval": is_complex and bool(facets or requirement_specs),
            "targetEvidenceCount": max(1, min(int(target_evidence_count), 12)),
            "needsClarification": needs_clarification,
            "clarificationQuestion": clarification,
            "candidateSourceCount": len(candidate_sources),
            "invalidTargetIds": invalid_ids,
            "invalidTargetChunks": invalid_chunks,
        }

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
                raise ValueError("模型未返回有效的上下文查询规划结果")
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError as error:
                raise ValueError("模型未返回有效的上下文查询规划结果") from error
        if not isinstance(payload, dict):
            raise ValueError("模型返回的上下文查询规划结构无效")
        return payload


__all__ = ["QueryPlanningAgent"]
