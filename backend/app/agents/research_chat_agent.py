"""结合本地论文、稀疏检索与向量检索完成研究问答。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from app.agents.hunter_agent import HunterAgent
from app.core.config import settings
from app.services.model_config import ModelConfigStore, SYSTEM_SECURITY_CONSTRAINT
from app.services.embedding_store import EmbeddingClient, SQLiteVectorStore
from app.services.rag_retriever import RAGRetriever


LogCallback = Callable[[str], None]


@dataclass(slots=True)
class ResearchAgentConfig:
    max_papers: int = settings.research_agent_max_papers
    max_sources: int = settings.research_agent_max_sources
    chunk_size: int = settings.research_agent_chunk_size
    max_context_chars: int = settings.research_agent_max_context_chars
    request_timeout: int = settings.research_agent_request_timeout


class ResearchChatAgent:
    """基于本地论文知识库进行检索增强研究问答。"""

    def __init__(
        self,
        *,
        config: ResearchAgentConfig | None = None,
        log_callback: LogCallback | None = None,
    ) -> None:
        self.config = config or ResearchAgentConfig()
        self.log_callback = log_callback
        self.hunter = HunterAgent(log_callback=self._log)
        embedding_client = EmbeddingClient(
            base_url=settings.rag_embedding_base_url,
            api_key=settings.rag_embedding_api_key,
            model=settings.rag_embedding_model,
            timeout=settings.rag_embedding_timeout,
        )
        self.retriever = RAGRetriever(
            chunk_size=self.config.chunk_size,
            max_chunks=self.config.max_sources,
            max_context_chars=self.config.max_context_chars,
            max_chunks_per_paper=settings.rag_max_chunks_per_paper,
            embedding_client=embedding_client,
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
    ) -> dict[str, Any]:
        normalized_question = str(question).strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")

        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")

        self._log("正在读取本地知识库")
        papers = self._load_papers(paper_ids)
        self._log(f"已读取 {len(papers)} 篇候选文献，开始检索相关证据")
        recent_questions = [
            str(message.get("content") or "").strip()
            for message in (history or [])[-4:]
            if message.get("role") == "user" and str(message.get("content") or "").strip()
        ]
        retrieval_query = self._expand_retrieval_query("\n".join([*recent_questions, normalized_question]))
        evidence = self.retriever.retrieve(retrieval_query, papers)
        self._log(f"检索模式：{self.retriever.last_retrieval_mode}")
        if not evidence:
            raise ValueError("知识库中没有可用于回答的已解析文献")

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
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        normalized_question = str(question).strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")
        papers = self._load_papers(paper_ids)
        recent_questions = [
            str(message.get("content") or "").strip()
            for message in (history or [])[-4:]
            if message.get("role") == "user" and str(message.get("content") or "").strip()
        ]
        retrieval_query = self._expand_retrieval_query("\n".join([*recent_questions, normalized_question]))
        evidence = self.retriever.retrieve(retrieval_query, papers)
        diagnostics = {"paperCount": len(papers), **self.retriever.last_diagnostics}
        return evidence, diagnostics

    def _load_papers(self, paper_ids: list[str] | None) -> list[dict[str, Any]]:
        if paper_ids:
            papers = [self.hunter.get_saved_paper(record_id) for record_id in paper_ids]
            return [paper for paper in papers if isinstance(paper, dict)]
        return self.hunter.list_saved_papers(limit=self.config.max_papers)

    def _expand_retrieval_query(self, query: str) -> str:
        translated = self.hunter.translate_search_query(query)
        if translated.strip().lower() == query.strip().lower():
            return query
        return f"{query}\n{translated}".strip()

    def _complete(
        self,
        *,
        model: dict[str, str],
        question: str,
        history: list[dict[str, str]],
        evidence: list[dict[str, Any]],
    ) -> str:
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

        response = requests.post(
            f"{model['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {model['api_key']}", "Content-Type": "application/json"},
            json={"model": model["model"], "messages": messages, "temperature": 0.2},
            timeout=self.config.request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices:
            raise RuntimeError("模型没有返回有效回答")
        answer = str(choices[0].get("message", {}).get("content") or "").strip()
        if not answer:
            raise RuntimeError("模型返回了空回答")
        return answer

    def _load_prompt(self) -> str:
        prompt_path = Path(__file__).resolve().parents[2] / "src" / "prompt" / "research_agent" / "zh.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"研究助手 Prompt 不存在：{prompt_path}")
        return prompt_path.read_text(encoding="utf-8")

    def _extract_citation_indices(self, answer: str, source_count: int) -> set[int]:
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
        if self.log_callback:
            self.log_callback(message)


__all__ = ["ResearchChatAgent", "ResearchAgentConfig"]
