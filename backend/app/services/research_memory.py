"""研究记忆与全局用户偏好的独立持久化服务。"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from app.core.config import settings
from app.services.model_client import chat_completion
from app.services.model_config import ModelConfigStore


MEMORY_TYPES = {"conclusion", "fact", "decision", "limitation", "hypothesis", "task"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _clean_text(value: str, limit: int) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", str(value or ""))
    text = re.sub(r"[#>*_`~]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _fallback_candidate(question: str, answer: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """模型不可用时生成可编辑候选，不把整条回答冒充为摘要。"""
    clean_question = _clean_text(question, 120)
    paragraphs = [
        _clean_text(item, 360)
        for item in re.split(r"\n\s*\n|(?<=[。！？.!?])\s+", answer)
        if _clean_text(item, 360)
    ]
    informative = [
        item for item in paragraphs
        if not item.startswith(("参考文献", "引用", "Sources", "References"))
    ]
    summary = " ".join(informative[:3])[:800] or clean_question
    lowered = f"{question} {answer}".lower()
    memory_type = "conclusion"
    for markers, candidate_type in (
        (("待办", "下一步", "需要继续", "todo"), "task"),
        (("假设", "尚待验证", "可能", "hypothesis"), "hypothesis"),
        (("局限", "限制", "不足", "limitation"), "limitation"),
        (("决定", "采用", "选择", "decision"), "decision"),
        (("数据显示", "实验结果", "事实", "fact"), "fact"),
    ):
        if any(marker in lowered for marker in markers):
            memory_type = candidate_type
            break
    return {
        "title": clean_question or "未命名研究记忆",
        "summary": summary,
        "type": memory_type,
        "tags": [],
        "confidence": 0.65 if sources else 0.45,
    }


class ResearchMemoryExtractor:
    """只负责把问答压缩成可由用户确认的结构化候选。"""

    def extract(self, question: str, answer: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
        fallback = _fallback_candidate(question, answer, sources)
        model = ModelConfigStore().build_model_payload()
        if not model:
            return fallback
        payload = {
            "question": _clean_text(question, 2000),
            "answer": str(answer or "")[:12000],
            "sources": [
                {
                    "index": source.get("index"),
                    "recordId": source.get("recordId") or source.get("record_id"),
                    "title": source.get("title"),
                    "excerpt": str(source.get("excerpt") or "")[:500],
                }
                for source in sources[:20]
            ],
        }
        try:
            raw = chat_completion(
                model,
                [
                    {
                        "role": "system",
                        "content": (
                            "你是研究记忆提炼器。只提取未来研究中可复用的信息，不复述整条回答。"
                            "输出一个 JSON 对象，字段为 title、summary、type、tags、confidence。"
                            "type 只能是 conclusion、fact、decision、limitation、hypothesis、task；"
                            "summary 不超过 500 个汉字，必须保留关键限定条件；tags 最多 8 个；"
                            "confidence 为 0 到 1。不要输出 Markdown。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0,
                timeout=min(settings.research_agent_request_timeout, 45),
                response_format={"type": "json_object"},
            )
            text = str(raw or "").strip()
            if text.startswith("```"):
                text = "\n".join(text.splitlines()[1:-1]).strip()
            parsed = json.loads(text)
            memory_type = str(parsed.get("type") or fallback["type"]).strip()
            tags = parsed.get("tags") if isinstance(parsed.get("tags"), list) else []
            return {
                "title": _clean_text(str(parsed.get("title") or fallback["title"]), 200),
                "summary": _clean_text(str(parsed.get("summary") or fallback["summary"]), 1200),
                "type": memory_type if memory_type in MEMORY_TYPES else fallback["type"],
                "tags": [_clean_text(str(tag), 60) for tag in tags[:8] if _clean_text(str(tag), 60)],
                "confidence": max(0.0, min(float(parsed.get("confidence", fallback["confidence"])), 1.0)),
            }
        except Exception:
            # 提炼失败不应阻断用户保存；候选仍需由用户确认，安全降级为确定性摘要。
            return fallback


class ResearchMemoryStore:
    """持久化项目研究记忆和会话无关的全局用户偏好。"""

    def __init__(self, path: str | Path = settings.conversation_db) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_memories (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    project_name TEXT NOT NULL DEFAULT '',
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    source_conversation_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    source_question TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_memories_project_updated
                    ON research_memories(session_id, project_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS user_preferences (
                    session_id TEXT PRIMARY KEY,
                    preferred_name TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT 'zh-CN',
                    answer_style TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """,
            )

    def list(self, *, session_id: str = "local", project_ids: list[str] | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            params: list[Any] = [session_id]
            where = "session_id = ?"
            normalized = [str(item).strip() for item in (project_ids or []) if str(item).strip()]
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                where += f" AND project_id IN ({placeholders})"
                params.extend(normalized)
            rows = connection.execute(
                f"SELECT * FROM research_memories WHERE {where} ORDER BY updated_at DESC LIMIT 500",
                params,
            ).fetchall()
            return [self._public_memory(row) for row in rows]

    def create(self, data: dict[str, Any], *, session_id: str = "local") -> dict[str, Any]:
        timestamp = _now()
        memory_id = str(data.get("id") or f"memory-{uuid4().hex}")
        memory_type = str(data.get("type") or "conclusion")
        if memory_type not in MEMORY_TYPES:
            raise ValueError("无效的研究记忆类型")
        title = _clean_text(str(data.get("title") or ""), 200)
        summary = _clean_text(str(data.get("summary") or ""), 1200)
        project_id = _clean_text(str(data.get("projectId") or ""), 200)
        if not title or not summary or not project_id:
            raise ValueError("研究记忆缺少标题、摘要或项目")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_memories (
                    id, session_id, project_id, project_name, type, title, summary,
                    tags_json, confidence, evidence_json, source_conversation_id,
                    source_message_id, source_question, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id, session_id, project_id, _clean_text(str(data.get("projectName") or ""), 200),
                    memory_type, title, summary,
                    json.dumps(list(data.get("tags") or [])[:8], ensure_ascii=False),
                    max(0.0, min(float(data.get("confidence") or 0), 1.0)),
                    json.dumps(list(data.get("evidence") or [])[:20], ensure_ascii=False),
                    _clean_text(str(data.get("sourceConversationId") or ""), 200),
                    _clean_text(str(data.get("sourceMessageId") or ""), 200),
                    _clean_text(str(data.get("sourceQuestion") or ""), 2000),
                    timestamp, timestamp,
                ),
            )
        return self.get(memory_id, session_id=session_id) or {}

    def update(self, memory_id: str, data: dict[str, Any], *, session_id: str = "local") -> dict[str, Any] | None:
        current = self.get(memory_id, session_id=session_id)
        if not current:
            return None
        merged = {**current, **data}
        memory_type = str(merged.get("type") or "conclusion")
        if memory_type not in MEMORY_TYPES:
            raise ValueError("无效的研究记忆类型")
        title = _clean_text(str(merged.get("title") or ""), 200)
        summary = _clean_text(str(merged.get("summary") or ""), 1200)
        if not title or not summary:
            raise ValueError("研究记忆标题和摘要不能为空")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE research_memories
                SET type = ?, title = ?, summary = ?, tags_json = ?, confidence = ?, updated_at = ?
                WHERE id = ? AND session_id = ?
                """,
                (
                    memory_type, title, summary,
                    json.dumps(list(merged.get("tags") or [])[:8], ensure_ascii=False),
                    max(0.0, min(float(merged.get("confidence") or 0), 1.0)),
                    _now(), memory_id, session_id,
                ),
            )
        return self.get(memory_id, session_id=session_id)

    def get(self, memory_id: str, *, session_id: str = "local") -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_memories WHERE id = ? AND session_id = ?",
                (memory_id, session_id),
            ).fetchone()
            return self._public_memory(row) if row else None

    def delete(self, memory_id: str, *, session_id: str = "local") -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM research_memories WHERE id = ? AND session_id = ?",
                (memory_id, session_id),
            )
            return cursor.rowcount > 0

    def get_preferences(self, *, session_id: str = "local") -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_preferences WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return {"preferredName": "", "language": "zh-CN", "answerStyle": "", "updatedAt": ""}
        return {
            "preferredName": row["preferred_name"],
            "language": row["language"],
            "answerStyle": row["answer_style"],
            "updatedAt": row["updated_at"],
        }

    def update_preferences(self, data: dict[str, Any], *, session_id: str = "local") -> dict[str, Any]:
        current = self.get_preferences(session_id=session_id)
        preferred_name = _clean_text(str(data.get("preferredName", current["preferredName"])), 80)
        language = _clean_text(str(data.get("language", current["language"])), 30) or "zh-CN"
        answer_style = _clean_text(str(data.get("answerStyle", current["answerStyle"])), 200)
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences (session_id, preferred_name, language, answer_style, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    preferred_name = excluded.preferred_name,
                    language = excluded.language,
                    answer_style = excluded.answer_style,
                    updated_at = excluded.updated_at
                """,
                (session_id, preferred_name, language, answer_style, timestamp),
            )
        return self.get_preferences(session_id=session_id)

    def clear_preferences(self, *, session_id: str = "local") -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM user_preferences WHERE session_id = ?", (session_id,))

    @staticmethod
    def _public_memory(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "projectName": row["project_name"],
            "type": row["type"],
            "title": row["title"],
            "summary": row["summary"],
            "tags": _json_loads(row["tags_json"], []),
            "confidence": row["confidence"],
            "evidence": _json_loads(row["evidence_json"], []),
            "sourceConversationId": row["source_conversation_id"],
            "sourceMessageId": row["source_message_id"],
            "sourceQuestion": row["source_question"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def build_response_context(self, *, session_id: str = "local", project_ids: list[str] | None = None) -> str:
        preferences = self.get_preferences(session_id=session_id)
        memories = self.list(session_id=session_id, project_ids=project_ids)[:12]
        context: dict[str, Any] = {}
        if any(preferences.get(key) for key in ("preferredName", "answerStyle")):
            context["userPreferences"] = preferences
        if memories:
            context["projectMemories"] = [
                {
                    "type": item["type"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "confidence": item["confidence"],
                }
                for item in memories
            ]
        return json.dumps(context, ensure_ascii=False) if context else ""


research_memory_store = ResearchMemoryStore()
research_memory_extractor = ResearchMemoryExtractor()


__all__ = [
    "MEMORY_TYPES",
    "ResearchMemoryExtractor",
    "ResearchMemoryStore",
    "research_memory_extractor",
    "research_memory_store",
]
