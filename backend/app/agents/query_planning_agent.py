"""把上下文研究问题规划为受约束、可验证的动态检索任务。"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.core.config import settings
from app.prompt_loader import load_prompt
from app.services.context_resolver import ContextResolver
from app.services.model_config import SYSTEM_SECURITY_CONSTRAINT_ZH
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

    SYSTEM_PROMPT = load_prompt("retrieval/planner.zh.md")

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
        scope_profile: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """生成规划并严格校验所有模型提供的来源引用。"""
        normalized_question = str(question or "").strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")

        resolved_context = self.context_resolver.resolve(normalized_question, history)
        planning_context = resolved_context.for_planning()
        candidate_sources = resolved_context.candidate_sources
        normalized_scope_profile = self._normalize_scope_profile(scope_profile)
        planner_input = {
            "current_question": normalized_question,
            "history_available": planning_context["history_available"],
            "historical_user_intents": planning_context["historical_user_intents"],
            "prior_answers": planning_context["prior_answers"],
            "candidate_sources": candidate_sources,
            "explicit_paper_ids": list(explicit_paper_ids or []),
            "scope_profile": normalized_scope_profile,
        }
        raw_response = self.completion(
            self.model,
            [
                {
                    "role": "system",
                    "content": f"{self.SYSTEM_PROMPT}\n\n{SYSTEM_SECURITY_CONSTRAINT_ZH}",
                },
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
                has_history=bool(resolved_context.conversation.normalized_history),
                available_scope_anchors=normalized_scope_profile["anchors"],
            ).to_dict()
            anchor_labels = {
                str(item["id"]): str(item["label"])
                for item in normalized_scope_profile["anchors"]
            }
            plan["scopeAnchorLabels"] = [
                anchor_labels[anchor_id]
                for anchor_id in plan.get("scopeAnchorIds") or []
                if anchor_id in anchor_labels
            ]
            plan["scopeProfileFingerprint"] = normalized_scope_profile["fingerprint"]
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
        has_history: bool = False,
        scope_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """兼容旧入口；问题范围由独立 QuestionContractBuilder 维护。"""
        return self.contract_builder.build(
            payload,
            question=normalized_question,
            candidate_sources=candidate_sources,
            explicit_paper_ids=explicit_paper_ids,
            has_history=has_history,
            available_scope_anchors=self._normalize_scope_profile(scope_profile)["anchors"],
        ).to_dict()

    @staticmethod
    def _normalize_scope_profile(scope_profile: dict[str, Any] | None) -> dict[str, Any]:
        """限制画像大小和字段，避免项目产物无界进入规划提示词。"""
        source = scope_profile if isinstance(scope_profile, dict) else {}
        anchors = [
            {
                "id": str(item.get("id") or "")[:200],
                "label": str(item.get("label") or "")[:500],
                "parentId": str(item.get("parentId") or "")[:200],
                "projectId": str(item.get("projectId") or "")[:200],
            }
            for item in list(source.get("anchors") or [])[:64]
            if isinstance(item, dict)
            and str(item.get("id") or "").strip()
            and str(item.get("label") or "").strip()
        ]
        documents = [
            {
                "recordId": str(item.get("recordId") or "")[:200],
                "title": str(item.get("title") or "")[:1000],
                "year": str(item.get("year") or "")[:40],
                "abstractSnippet": str(item.get("abstractSnippet") or "")[:1200],
            }
            for item in list(source.get("documents") or [])[:80]
            if isinstance(item, dict) and str(item.get("recordId") or "").strip()
        ]
        projects = [
            {
                "id": str(item.get("id") or "")[:200],
                "name": str(item.get("name") or "")[:200],
                "description": str(item.get("description") or "")[:1000],
            }
            for item in list(source.get("projects") or [])[:20]
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        return {
            "schemaVersion": int(source.get("schemaVersion") or 1),
            "projects": projects,
            "anchors": anchors,
            "documents": documents,
            "allowedAsAnswerEvidence": False,
            "fingerprint": str(source.get("fingerprint") or "")[:128],
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
