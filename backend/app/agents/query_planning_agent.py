"""把上下文研究问题规划为受约束、可验证的动态检索任务。"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.core.config import settings
from app.services.context_resolver import ContextResolver
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT
from app.services.question_contract_builder import QuestionContractBuilder
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
   historical_user_intents 表示历史用户目标；prior_answers 是旧回答，仅可用于指代消解、识别待核验命题或文本变换，绝不能作为事实、研究结论或证据。
   当前用户问题和当前用户的纠正优先于所有旧回答；若用户质疑旧结论，standalone_question 必须表达“重新核验该命题”的真实意图。
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
        self.context_resolver = ContextResolver()
        self.contract_builder = QuestionContractBuilder(max_facets=self.max_facets)

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

        resolved_context = self.context_resolver.resolve(normalized_question, history)
        planning_context = resolved_context.for_planning()
        candidate_sources = resolved_context.candidate_sources
        planner_input = {
            "current_question": normalized_question,
            "usage_mode": planning_context["usage_mode"],
            "historical_user_intents": planning_context["historical_user_intents"],
            "prior_answers": planning_context["prior_answers"],
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
            plan = self.contract_builder.build(
                payload,
                question=normalized_question,
                candidate_sources=candidate_sources,
                explicit_paper_ids=explicit_paper_ids or [],
            ).to_dict()
        except Exception as error:
            setattr(error, "raw_response", str(raw_response or ""))
            raise
        return plan, str(raw_response or "")

    def _normalize_plan(
        self,
        payload: dict[str, Any],
        *,
        normalized_question: str,
        candidate_sources: list[dict[str, Any]],
        explicit_paper_ids: list[str],
    ) -> dict[str, Any]:
        """兼容旧入口；问题范围由独立 QuestionContractBuilder 维护。"""
        return self.contract_builder.build(
            payload,
            question=normalized_question,
            candidate_sources=candidate_sources,
            explicit_paper_ids=explicit_paper_ids,
        ).to_dict()

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
