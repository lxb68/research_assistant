"""把原始对话历史投影为按用途隔离、带可信度标签的上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


TRANSIENT_ERROR_PREFIXES = ("请求失败：", "研究对话请求失败", "研究任务已完成，但没有返回")
PRIOR_ANSWER_TRUST = "unverified_prior_answer"


@dataclass
class ConversationContext:
    """保存当前问题及从历史中提取出的意图、旧回答和来源引用。"""

    current_question: str
    usage_mode: str
    normalized_history: list[dict[str, Any]] = field(default_factory=list)
    user_intents: list[dict[str, Any]] = field(default_factory=list)
    prior_answers: list[dict[str, Any]] = field(default_factory=list)
    reference_sources: list[dict[str, Any]] = field(default_factory=list)

    def for_query_planning(self) -> dict[str, Any]:
        """返回查询规划需要的语义历史，旧回答始终是不可验证输入。"""
        return {
            "usage_mode": self.usage_mode,
            "historical_user_intents": self.user_intents,
            "prior_answers": self.prior_answers,
            "candidate_sources": self.reference_sources,
        }

    def for_model_context(self, *, user_limit: int = 6, answer_limit: int = 3) -> dict[str, Any]:
        """返回路由、直答和工具循环共享的结构化上下文视图。"""
        return {
            "usageMode": self.usage_mode,
            "historicalUserIntents": self.user_intents[-max(1, user_limit) :],
            "priorAnswers": self.prior_answers[-max(1, answer_limit) :],
            "referenceSources": self.reference_sources,
            "contextPolicy": {
                "currentUserIntentHasPriority": True,
                "priorAnswersAreEvidence": False,
                "priorAnswerAllowedUses": ["reference_resolution", "text_transformation"],
            },
        }


class ConversationContextProjector:
    """集中治理对话历史，避免各 Agent 各自解释旧回答的可信度。"""

    def __init__(self, *, max_messages: int = 8, max_sources: int = 20) -> None:
        self.max_messages = max(1, int(max_messages))
        self.max_sources = max(1, int(max_sources))

    def project(self, current_question: str, history: list[dict[str, Any]] | None) -> ConversationContext:
        """规范化历史，并生成按语义角色隔离的上下文。"""
        question = str(current_question or "").strip()
        normalized = self.normalize_history(list(history or []))[-self.max_messages :]
        user_intents: list[dict[str, Any]] = []
        prior_answers: list[dict[str, Any]] = []
        reference_sources: list[dict[str, Any]] = []
        seen_sources: set[tuple[str, int]] = set()

        for turn, message in enumerate(normalized, start=1):
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role == "user":
                user_intents.append({"turn": turn, "content": content[:6000]})
                continue

            normalized_sources: list[dict[str, Any]] = []
            for source in list(message.get("sources") or [])[: self.max_sources]:
                normalized_source = self._normalize_source(source)
                if normalized_source is None:
                    continue
                normalized_sources.append(normalized_source)
                source_key = (normalized_source["record_id"], normalized_source["chunk_index"])
                if source_key not in seen_sources:
                    seen_sources.add(source_key)
                    reference_sources.append(normalized_source)
            prior_answers.append(
                {
                    "turn": turn,
                    "content": content[:6000],
                    "trust": PRIOR_ANSWER_TRUST,
                    "allowed_as_evidence": False,
                    "allowed_uses": ["reference_resolution", "text_transformation"],
                    "sources": normalized_sources,
                }
            )

        return ConversationContext(
            current_question=question,
            usage_mode=self._classify_usage(question, bool(normalized)),
            normalized_history=normalized,
            user_intents=user_intents,
            prior_answers=prior_answers,
            reference_sources=reference_sources,
        )

    @staticmethod
    def normalize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """过滤无效消息和失败轮次，同时保留回答来源元数据。"""
        cleaned: list[dict[str, Any]] = []
        for message in history:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            if role == "assistant" and content.startswith(TRANSIENT_ERROR_PREFIXES):
                if cleaned and cleaned[-1]["role"] == "user":
                    cleaned.pop()
                continue
            normalized: dict[str, Any] = {"role": role, "content": content}
            if role == "assistant":
                sources = [dict(source) for source in list(message.get("sources") or []) if isinstance(source, dict)]
                if sources:
                    normalized["sources"] = sources[:20]
            cleaned.append(normalized)
        return cleaned

    @staticmethod
    def _normalize_source(source: Any) -> dict[str, Any] | None:
        if not isinstance(source, dict):
            return None
        record_id = str(source.get("record_id") or source.get("recordId") or "").strip()
        if not record_id:
            return None
        try:
            chunk_index = int(source.get("chunk_index") or source.get("chunkIndex") or 0)
        except (TypeError, ValueError):
            chunk_index = 0
        try:
            index = int(source.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        return {
            "index": index,
            "record_id": record_id,
            "title": str(source.get("title") or "")[:1000],
            "section": str(source.get("section") or "")[:1000],
            "chunk_index": chunk_index,
            "excerpt": str(source.get("excerpt") or "")[:1200],
        }

    @staticmethod
    def _classify_usage(question: str, has_history: bool) -> str:
        normalized = question.casefold()
        transform_markers = ("翻译", "改写", "重写", "润色", "压缩", "总结上", "translate", "rewrite")
        correction_markers = ("不对", "不是", "重新核对", "重新检查", "更正", "纠正", "靠谱吗", "可靠吗")
        reference_markers = ("它", "这个", "该结论", "上述", "上面", "刚才", "前者", "后者", "两者")
        if any(marker in normalized for marker in transform_markers):
            return "transform"
        if any(marker in normalized for marker in correction_markers):
            return "correction"
        if any(marker in normalized for marker in reference_markers):
            return "reference"
        return "followup" if has_history else "new_topic"


__all__ = [
    "ConversationContext",
    "ConversationContextProjector",
    "PRIOR_ANSWER_TRUST",
    "TRANSIENT_ERROR_PREFIXES",
]
