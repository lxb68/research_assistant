"""结合本地论文、稀疏检索与向量检索完成研究问答。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.agents.hunter_agent import HunterAgent
from app.core.config import settings
from app.services.model_config import ModelConfigStore, SYSTEM_SECURITY_CONSTRAINT
from app.services.model_client import chat_completion
from app.services.embedding_store import EmbeddingClient, SQLiteVectorStore
from app.services.rag_retriever import RAGRetriever


LogCallback = Callable[[str], None]


@dataclass(slots=True)
class ResearchAgentConfig:
    """集中描述研究问答代理的运行参数。"""
    max_papers: int = settings.research_agent_max_papers
    max_sources: int = settings.research_agent_max_sources
    target_chunk_tokens: int = settings.rag_chunk_target_tokens
    max_chunk_tokens: int = settings.rag_chunk_max_tokens
    overlap_tokens: int = settings.rag_chunk_overlap_tokens
    max_context_chars: int = settings.research_agent_max_context_chars
    request_timeout: int = settings.research_agent_request_timeout


class ResearchChatAgent:
    """基于本地论文知识库进行检索增强研究问答。"""

    QUERY_PLANNER_SYSTEM_PROMPT = """你是研究对话的上下文查询规划器。根据当前问题、最近对话和候选证据来源，生成可独立检索的问题并解析指代。

要求：
1. standalone_question 必须是脱离对话历史后仍语义完整的研究问题；可以补入历史中明确出现的论文标题、方法、结论或实验对象。
2. 不要把无关的历史问题机械拼接进 standalone_question。
3. target_paper_ids 和 target_chunks 只能使用 candidate_sources 中真实存在的值，不能编造 ID。
4. 用户明确追问某篇论文时填写 target_paper_ids；追问某个引用、章节或片段时填写 target_chunks。
5. 如果“它、前者、这个片段、上述方法”等指代无法唯一确定，设置 needs_clarification=true，并给出简短 clarification_question。
6. 问题本身完整且没有限定来源时，目标数组保持为空。

只输出一个 JSON 对象，不要输出 Markdown 或额外文字：
{"standalone_question":"...","target_paper_ids":[],"target_chunks":[{"record_id":"...","chunk_index":0}],"needs_clarification":false,"clarification_question":""}
"""

    def __init__(
        self,
        *,
        config: ResearchAgentConfig | None = None,
        log_callback: LogCallback | None = None,
    ) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.config = config or ResearchAgentConfig()
        self.log_callback = log_callback
        self.hunter = HunterAgent(log_callback=self._log)
        bailian_embedding_client = EmbeddingClient(
            base_url=settings.rag_embedding_base_url,
            api_key=settings.rag_embedding_api_key,
            model=settings.rag_embedding_model,
            timeout=settings.rag_embedding_timeout,
            provider="bailian",
            protocol="openai_compatible",
            batch_size=10,
            requires_api_key=True,
        )
        local_embedding_client = EmbeddingClient(
            base_url=settings.rag_local_embedding_base_url,
            api_key=settings.rag_local_embedding_api_key,
            model=settings.rag_local_embedding_model,
            timeout=settings.rag_local_embedding_timeout,
            provider=f"local_{settings.rag_local_embedding_protocol}",
            protocol=settings.rag_local_embedding_protocol,
            batch_size=16,
            requires_api_key=False,
        )
        self.retriever = RAGRetriever(
            target_chunk_tokens=self.config.target_chunk_tokens,
            max_chunk_tokens=self.config.max_chunk_tokens,
            overlap_tokens=self.config.overlap_tokens,
            max_chunks=self.config.max_sources,
            max_context_chars=self.config.max_context_chars,
            max_chunks_per_paper=settings.rag_max_chunks_per_paper,
            # 顺序即降级优先级：百炼 → 本地 Embedding → TF-IDF（检索器内部兜底）。
            embedding_clients=[bailian_embedding_client, local_embedding_client],
            vector_store=SQLiteVectorStore(settings.rag_vector_store_path),
            bm25_weight=settings.rag_bm25_weight,
            vector_weight=settings.rag_vector_weight,
        )

    def run(
        self,
        question: str,
        *,
        history: list[dict[str, str]] | None = None,
        paper_ids: list[str] | None = None,
        retrieval_query: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """执行当前代理的主要业务流程并返回结构化结果。"""
        normalized_question = str(question).strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")

        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")

        reused_evidence = evidence is not None
        if evidence is None:
            self._log("正在读取本地知识库")
            papers = self._load_papers(paper_ids)
            self._log(f"已读取 {len(papers)} 篇候选文献，开始检索相关证据")
            query = str(retrieval_query or normalized_question).strip()
            evidence = self.retriever.retrieve(
                self._expand_retrieval_query(query),
                papers,
                minimum_evidence_count=settings.orchestrator_min_evidence,
            )
            self._log(f"检索模式：{self.retriever.last_retrieval_mode}")
            embedding_backend = str(self.retriever.last_diagnostics.get("embeddingBackend") or "")
            if embedding_backend:
                self._log(f"向量后端：{embedding_backend}")
            embedding_failures = self.retriever.last_diagnostics.get("embeddingFailures") or []
            if embedding_failures:
                self._log(f"Embedding 自动降级：{', '.join(str(item) for item in embedding_failures)}")
        else:
            self._log(f"复用编排器已选取的 {len(evidence)} 条证据，正在生成回答")
        if not evidence:
            raise ValueError("知识库中没有可用于回答的已解析文献")

        if not reused_evidence:
            self._log(f"已选取 {len(evidence)} 条相关证据，正在生成回答")
        answer = self._complete(
            model=model,
            question=normalized_question,
            history=history or [],
            evidence=evidence,
        )
        retrieved_sources = [
            {
                "index": index,
                "recordId": item["record_id"],
                "title": item["title"],
                "year": item.get("year", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "section": item.get("section", ""),
                "chunkIndex": item.get("chunk_index", 0),
                "tokenCount": item.get("token_count", 0),
                "baseChunkIndices": item.get("base_chunk_indices", []),
                "excerpt": item["text"][:320],
                "score": round(float(item["score"]), 4),
            }
            for index, item in enumerate(evidence, start=1)
        ]
        cited_indices = self._extract_citation_indices(answer, len(retrieved_sources))
        sources = [source for source in retrieved_sources if source["index"] in cited_indices]
        self._log("研究回答生成完成")
        return {
            "answer": answer,
            "sources": sources,
            "retrievedSources": retrieved_sources,
            "citationDiagnostics": {
                "retrievedCount": len(retrieved_sources),
                "citedCount": len(sources),
                "citedIndices": sorted(cited_indices),
            },
            "model": model["model"],
            "retrievalMode": self.retriever.last_retrieval_mode,
            "retrievalDiagnostics": self.retriever.last_diagnostics,
        }

    def retrieve_evidence(
        self,
        question: str,
        *,
        history: list[dict[str, str]] | None = None,
        paper_ids: list[str] | None = None,
        retrieval_query: str | None = None,
        target_chunks: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """从本地论文库检索与问题相关的证据片段。"""
        normalized_question = str(question).strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")
        papers = self._load_papers(paper_ids)
        query = str(retrieval_query or normalized_question).strip()
        evidence = self.retriever.retrieve(
            self._expand_retrieval_query(query),
            papers,
            minimum_evidence_count=settings.orchestrator_min_evidence,
        )
        resolved_target_evidence = self.retriever.resolve_chunk_references(papers, target_chunks or [])
        if resolved_target_evidence:
            merged: list[dict[str, Any]] = []
            seen: set[tuple[str, int]] = set()
            for item in [*resolved_target_evidence, *evidence]:
                key = (str(item.get("record_id") or ""), int(item.get("chunk_index") or 0))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if len(merged) >= self.config.max_sources:
                    break
            evidence = merged
        diagnostics = {"paperCount": len(papers), **self.retriever.last_diagnostics}
        diagnostics.update(
            {
                "evidenceCount": len(evidence),
                "distinctPaperCount": len({str(item.get("record_id") or "") for item in evidence}),
                "selectedPaperIds": list(dict.fromkeys(str(item.get("record_id") or "") for item in evidence)),
                "requestedChunkRefs": [
                    {
                        "recordId": str(item.get("record_id") or ""),
                        "chunkIndex": int(item.get("chunk_index") or 0),
                    }
                    for item in target_chunks or []
                ],
                "resolvedChunkRefs": [
                    {
                        "recordId": str(item.get("record_id") or ""),
                        "chunkIndex": int(item.get("chunk_index") or 0),
                    }
                    for item in resolved_target_evidence
                ],
            }
        )
        return evidence, diagnostics

    def plan_retrieval(
        self,
        question: str,
        history: list[dict[str, str]] | None,
        *,
        explicit_paper_ids: list[str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """结合历史文本和结构化来源，把追问规划为独立、受约束的检索任务。"""
        normalized_question = str(question or "").strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")
        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")

        context_messages: list[dict[str, Any]] = []
        candidate_sources: list[dict[str, Any]] = []
        seen_sources: set[tuple[str, int]] = set()
        for message in (history or [])[-8:]:
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            normalized_sources: list[dict[str, Any]] = []
            for source in list(message.get("sources") or [])[:20]:
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

        planner_input = {
            "current_question": normalized_question,
            "history": context_messages,
            "candidate_sources": candidate_sources,
            "explicit_paper_ids": list(explicit_paper_ids or []),
        }
        raw_response = chat_completion(
            model,
            [
                {
                    "role": "system",
                    "content": f"{self.QUERY_PLANNER_SYSTEM_PROMPT}\n\n{SYSTEM_SECURITY_CONSTRAINT}",
                },
                {"role": "user", "content": json.dumps(planner_input, ensure_ascii=False)},
            ],
            temperature=0,
            timeout=self.config.request_timeout,
            response_format={"type": "json_object"},
        )
        try:
            payload = self._parse_planner_response(raw_response)
        except Exception as error:
            setattr(error, "raw_response", str(raw_response or ""))
            raise

        known_ids = {str(source["record_id"]) for source in candidate_sources}
        explicit_ids = {str(record_id) for record_id in explicit_paper_ids or [] if str(record_id)}
        allowed_ids = known_ids | explicit_ids
        proposed_values = payload.get("target_paper_ids")
        if not isinstance(proposed_values, list):
            proposed_values = []
        proposed_ids = [str(value).strip() for value in proposed_values if str(value).strip()]
        target_paper_ids = list(dict.fromkeys(proposed_ids if not explicit_ids else explicit_paper_ids or []))
        invalid_ids = [record_id for record_id in target_paper_ids if record_id not in allowed_ids]
        target_paper_ids = [record_id for record_id in target_paper_ids if record_id in allowed_ids]

        known_chunks = {
            (str(source["record_id"]), int(source["chunk_index"]))
            for source in candidate_sources
        }
        target_chunks: list[dict[str, Any]] = []
        invalid_chunks: list[dict[str, Any]] = []
        proposed_chunks = payload.get("target_chunks")
        if not isinstance(proposed_chunks, list):
            proposed_chunks = []
        for item in proposed_chunks:
            if not isinstance(item, dict):
                continue
            try:
                chunk_index = int(item.get("chunk_index") or 0)
            except (TypeError, ValueError):
                invalid_chunks.append({"record_id": str(item.get("record_id") or "").strip(), "chunk_index": -1})
                continue
            reference = {
                "record_id": str(item.get("record_id") or "").strip(),
                "chunk_index": chunk_index,
            }
            if (reference["record_id"], reference["chunk_index"]) in known_chunks:
                if reference not in target_chunks:
                    target_chunks.append(reference)
            else:
                invalid_chunks.append(reference)
        for reference in target_chunks:
            if reference["record_id"] not in target_paper_ids:
                target_paper_ids.append(reference["record_id"])

        needs_clarification = payload.get("needs_clarification") is True
        if (invalid_ids and not target_paper_ids) or (invalid_chunks and not target_chunks):
            needs_clarification = True
        clarification = str(payload.get("clarification_question") or "").strip()
        if needs_clarification and not clarification:
            clarification = "我无法唯一确定你指的是哪篇文献或哪个片段，请补充论文标题、章节或引用内容。"
        plan = {
            "standaloneQuestion": str(payload.get("standalone_question") or normalized_question).strip(),
            "targetPaperIds": target_paper_ids,
            "targetChunks": target_chunks,
            "needsClarification": needs_clarification,
            "clarificationQuestion": clarification,
            "candidateSourceCount": len(candidate_sources),
            "invalidTargetIds": invalid_ids,
            "invalidTargetChunks": invalid_chunks,
        }
        return plan, str(raw_response or "")

    def _parse_planner_response(self, raw_response: str) -> dict[str, Any]:
        """解析查询规划器 JSON，拒绝非对象结果。"""
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

    def _load_papers(self, paper_ids: list[str] | None) -> list[dict[str, Any]]:
        """加载论文。"""
        if paper_ids:
            papers = [self.hunter.get_saved_paper(record_id) for record_id in paper_ids]
            return [paper for paper in papers if isinstance(paper, dict)]
        return self.hunter.list_saved_papers(limit=self.config.max_papers)

    def _expand_retrieval_query(self, query: str) -> str:
        """扩展检索词。"""
        translated = self.hunter.translate_search_query(query)
        if translated.strip().lower() == query.strip().lower():
            return query
        return f"{query}\n{translated}".strip()

    def _complete(
        self,
        *,
        model: dict[str, Any],
        question: str,
        history: list[dict[str, str]],
        evidence: list[dict[str, Any]],
    ) -> str:
        """调用已配置模型，根据证据上下文生成研究回答。"""
        context = self.retriever.build_context(evidence)
        system_prompt = self._load_prompt().replace("{{evidence}}", context)
        system_prompt = f"{system_prompt}\n\n{SYSTEM_SECURITY_CONSTRAINT}"
        messages = [{"role": "system", "content": system_prompt}]
        for message in history[-8:]:
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content[:6000]})
        messages.append({"role": "user", "content": question})

        # 供应商协议差异统一由适配器处理，Agent 只维护通用消息结构。
        return chat_completion(
            model,
            messages,
            temperature=0.2,
            timeout=self.config.request_timeout,
        )

    def _load_prompt(self) -> str:
        """加载提示词。"""
        prompt_path = Path(__file__).resolve().parents[2] / "src" / "prompt" / "research_agent" / "zh.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"研究助手 Prompt 不存在：{prompt_path}")
        return prompt_path.read_text(encoding="utf-8")

    def _extract_citation_indices(self, answer: str, source_count: int) -> set[int]:
        """从回答文本中提取有效引用序号。"""
        indices: set[int] = set()
        for content in re.findall(r"\[([0-9,，\-–—\s]+)\]", answer):
            normalized = content.replace("，", ",").replace("–", "-").replace("—", "-")
            for part in normalized.split(","):
                value = part.strip()
                if not value:
                    continue
                if "-" in value:
                    bounds = [item.strip() for item in value.split("-", 1)]
                    if len(bounds) == 2 and all(item.isdigit() for item in bounds):
                        start, end = int(bounds[0]), int(bounds[1])
                        if start <= end and end - start <= 20:
                            indices.update(range(start, end + 1))
                    continue
                if value.isdigit():
                    indices.add(int(value))
        return {index for index in indices if 1 <= index <= source_count}

    def _log(self, message: str) -> None:
        """把运行消息转发给已配置的日志回调。"""
        if self.log_callback:
            self.log_callback(message)


__all__ = ["ResearchChatAgent", "ResearchAgentConfig"]
