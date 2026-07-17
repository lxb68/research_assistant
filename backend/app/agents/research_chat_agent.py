"""结合本地论文、稀疏检索与向量检索完成研究问答。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.agents.hunter_agent import HunterAgent
from app.agents.query_planning_agent import QueryPlanningAgent
from app.core.config import settings
from app.services.model_config import ModelConfigStore, SYSTEM_SECURITY_CONSTRAINT
from app.services.model_client import chat_completion
from app.services.rag_factory import build_default_rag_retriever
from app.services.rag_retriever import EvidenceChunk, RAGRetriever


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
        self.retriever: RAGRetriever = build_default_rag_retriever(
            target_chunk_tokens=self.config.target_chunk_tokens,
            max_chunk_tokens=self.config.max_chunk_tokens,
            overlap_tokens=self.config.overlap_tokens,
            max_chunks=self.config.max_sources,
            max_context_chars=self.config.max_context_chars,
        )

    def run(
        self,
        question: str,
        *,
        history: list[dict[str, str]] | None = None,
        paper_ids: list[str] | None = None,
        retrieval_query: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        answer_requirements: list[str] | None = None,
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
            answer_requirements=answer_requirements or [],
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
        retrieval_facets: list[dict[str, Any]] | None = None,
        question_type: str = "simple_fact",
        target_evidence_count: int | None = None,
        existing_evidence: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """从本地论文库检索与问题相关的证据片段。"""
        normalized_question = str(question).strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")
        papers = self._load_papers(paper_ids)
        query = str(retrieval_query or normalized_question).strip()
        facets = [dict(item) for item in retrieval_facets or [] if isinstance(item, dict)]
        desired_count = max(
            settings.orchestrator_min_evidence,
            min(int(target_evidence_count or settings.orchestrator_min_evidence), self.config.max_sources),
        )
        if facets:
            evidence, retrieval_diagnostics = self._retrieve_facets(
                papers,
                facets,
                question_type=question_type,
                target_evidence_count=desired_count,
                existing_evidence=existing_evidence or [],
            )
        else:
            evidence = self.retriever.retrieve(
                self._expand_retrieval_query(query),
                papers,
                minimum_evidence_count=settings.orchestrator_min_evidence,
            )
            retrieval_diagnostics = dict(self.retriever.last_diagnostics)
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
        full_text_paper_count = sum(bool(self.retriever._read_markdown(paper)) for paper in papers)
        diagnostics = {
            "paperCount": len(papers),
            "fullTextPaperCount": full_text_paper_count,
            "fullTextAvailable": bool(papers) and full_text_paper_count == len(papers),
            **retrieval_diagnostics,
        }
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

    def _retrieve_facets(
        self,
        papers: list[dict[str, Any]],
        facets: list[dict[str, Any]],
        *,
        question_type: str,
        target_evidence_count: int,
        existing_evidence: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """执行多路 facet 检索，并按来源章节和覆盖维度融合结果。"""
        merged: dict[tuple[str, int], dict[str, Any]] = {}
        retrieval_runs: list[dict[str, Any]] = []
        per_facet_limit = max(2, min(4, target_evidence_count // max(1, len(facets)) + 1))
        for facet in facets[: settings.query_planner_max_facets]:
            facet_id = str(facet.get("id") or f"facet-{len(retrieval_runs) + 1}")
            facet_query = str(facet.get("query") or "").strip()
            if not facet_query:
                continue
            preferred_types = {
                str(value).strip().lower()
                for value in facet.get("preferredSectionTypes") or facet.get("preferred_section_types") or []
                if str(value).strip()
            }
            expanded_facet_query = self._expand_facet_query(
                facet_query,
                question_type=question_type,
                preferred_types=preferred_types,
            )
            results = self.retriever.retrieve(
                self._expand_retrieval_query(expanded_facet_query),
                papers,
                minimum_evidence_count=per_facet_limit,
                section_score_adjuster=lambda section, preferred=preferred_types: self._section_intent_adjustment(
                    question_type=question_type,
                    section_type=self._classify_section_type(section),
                    preferred_types=preferred,
                ),
                chunk_score_adjuster=lambda chunk, preferred=preferred_types: self._content_intent_adjustment(
                    chunk,
                    question_type=question_type,
                    preferred_types=preferred,
                ),
            )
            run_diagnostics = dict(self.retriever.last_diagnostics)
            run_diagnostics.update({"facetId": facet_id, "query": facet_query, "expandedQuery": expanded_facet_query})
            retrieval_runs.append(run_diagnostics)
            for rank, raw_item in enumerate(results[:per_facet_limit], start=1):
                item = dict(raw_item)
                key = (str(item.get("record_id") or ""), int(item.get("chunk_index") or 0))
                section_type = self._classify_evidence_type(
                    str(item.get("section") or ""),
                    str(item.get("text") or ""),
                )
                section_adjustment = self._section_intent_adjustment(
                    question_type=question_type,
                    section_type=section_type,
                    preferred_types=preferred_types,
                )
                contribution = float(item.get("score") or 0) + section_adjustment + 1 / (20 + rank)
                if key not in merged:
                    item["matched_facet_ids"] = [facet_id]
                    item["section_type"] = section_type
                    item["fusion_score"] = contribution
                    merged[key] = item
                else:
                    current = merged[key]
                    if facet_id not in current["matched_facet_ids"]:
                        current["matched_facet_ids"].append(facet_id)
                        current["fusion_score"] += 0.08
                    current["fusion_score"] = max(float(current["fusion_score"]), contribution)
                    current["score"] = max(float(current.get("score") or 0), float(item.get("score") or 0))

        for raw_item in existing_evidence:
            item = dict(raw_item)
            key = (str(item.get("record_id") or ""), int(item.get("chunk_index") or 0))
            item.setdefault("matched_facet_ids", [])
            item.setdefault(
                "section_type",
                self._classify_evidence_type(
                    str(item.get("section") or ""),
                    str(item.get("text") or ""),
                ),
            )
            item.setdefault("fusion_score", float(item.get("score") or 0) + 0.05)
            if key not in merged:
                merged[key] = item

        ranked = sorted(merged.values(), key=lambda item: float(item.get("fusion_score") or 0), reverse=True)
        selected: list[dict[str, Any]] = []
        selected_keys: set[tuple[str, int]] = set()
        per_section: dict[tuple[str, str], int] = {}
        context_chars = 0

        def try_select(item: dict[str, Any]) -> bool:
            nonlocal context_chars
            key = (str(item.get("record_id") or ""), int(item.get("chunk_index") or 0))
            if key in selected_keys:
                return False
            section_key = (str(item.get("record_id") or ""), str(item.get("section") or ""))
            if per_section.get(section_key, 0) >= 2:
                return False
            text_size = len(str(item.get("text") or ""))
            if context_chars + text_size > self.config.max_context_chars:
                return False
            selected.append(item)
            selected_keys.add(key)
            per_section[section_key] = per_section.get(section_key, 0) + 1
            context_chars += text_size
            return True

        # 先覆盖每个动态检索 facet，再用全局融合分补满剩余上下文。
        for facet in facets:
            facet_id = str(facet.get("id") or "")
            if not facet_id:
                continue
            for item in ranked:
                if facet_id in (item.get("matched_facet_ids") or []) and try_select(item):
                    break
            if len(selected) >= target_evidence_count:
                break
        if len(selected) < target_evidence_count:
            for item in ranked:
                try_select(item)
                if len(selected) >= target_evidence_count:
                    break

        selected_facet_ids = {
            str(facet_id)
            for item in selected
            for facet_id in item.get("matched_facet_ids") or []
            if str(facet_id)
        }
        requested_facet_ids = {str(item.get("id") or "") for item in facets if str(item.get("id") or "")}
        section_types = {str(item.get("section_type") or "background") for item in selected}
        method_types = {"method", "framework", "protocol", "algorithm", "implementation", "overview"}
        query_coverages = [float(item.get("queryCoverage") or 0) for item in retrieval_runs]
        diagnostics = {
            "retrievalMode": "+".join(dict.fromkeys(str(item.get("retrievalMode") or "") for item in retrieval_runs)),
            "embeddingBackend": "+".join(dict.fromkeys(str(item.get("embeddingBackend") or "") for item in retrieval_runs)),
            "embeddingFailures": list(dict.fromkeys(
                str(failure)
                for item in retrieval_runs
                for failure in item.get("embeddingFailures") or []
            )),
            "chunkingStrategy": "mineru_structure_semantic_token_overlap",
            "candidateCount": max((int(item.get("candidateCount") or 0) for item in retrieval_runs), default=0),
            "evidenceCount": len(selected),
            "distinctPaperCount": len({str(item.get("record_id") or "") for item in selected}),
            "selectedPaperIds": list(dict.fromkeys(str(item.get("record_id") or "") for item in selected)),
            "queryCoverage": round(sum(query_coverages) / max(1, len(query_coverages)), 4),
            "topScore": round(max((float(item.get("score") or 0) for item in selected), default=0), 4),
            "facetCount": len(requested_facet_ids),
            "coveredFacetCount": len(requested_facet_ids & selected_facet_ids),
            "facetCoverage": round(len(requested_facet_ids & selected_facet_ids) / max(1, len(requested_facet_ids)), 4),
            "coveredFacetIds": sorted(requested_facet_ids & selected_facet_ids),
            "missingFacetIds": sorted(requested_facet_ids - selected_facet_ids),
            "coveredSectionTypes": sorted(section_types),
            "methodEvidenceCount": sum(str(item.get("section_type") or "") in method_types for item in selected),
            "targetEvidenceCount": target_evidence_count,
            "retrievalRuns": retrieval_runs,
        }
        return selected, diagnostics

    @staticmethod
    def _expand_facet_query(
        query: str,
        *,
        question_type: str,
        preferred_types: set[str],
    ) -> str:
        """按通用章节意图扩展 facet，不依赖论文名称或固定章节号。"""
        additions: list[str] = []
        if question_type == "mechanism":
            if preferred_types & {"overview", "framework", "background"}:
                additions.append(
                    "complete workflow end-to-end putting everything together full framework "
                    "require ensure input output numbered steps"
                )
            if preferred_types & {"method", "protocol", "algorithm", "implementation"}:
                additions.append("method protocol algorithm implementation detailed steps")
        elif question_type == "evaluation":
            additions.append("experiment evaluation result metrics comparison")
        return "\n".join([query, *additions]).strip()

    @staticmethod
    def _classify_section_type(section: str) -> str:
        """把论文标题归一为通用章节类型，供跨领域重排使用。"""
        section_path = str(section or "")
        parts = [part.strip() for part in section_path.split(" > ") if part.strip()]
        # 第一层通常是论文标题，不能让标题中的 Framework、Training 等词污染章节类型。
        relevant_path = " > ".join(parts[1:]) if len(parts) > 1 else section_path
        lowered = relevant_path.casefold()
        patterns = (
            ("experiment", ("experiment", "evaluation", "effectiveness", "benchmark", "实验", "评估")),
            ("result", ("result", "finding", "结果")),
            ("comparison", ("comparison", "compare", "related work", "比较", "相关工作")),
            ("protocol", ("protocol", "协议")),
            ("algorithm", ("algorithm", "算法")),
            ("framework", ("framework", "architecture", "框架", "架构")),
            ("implementation", ("implementation", "system design", "实现", "系统设计")),
            ("overview", ("overview", "putting everything together", "complete workflow", "end-to-end", "概述", "完整流程", "整体流程")),
            ("method", ("method", "approach", "training", "optimization", "方法", "训练", "优化")),
            ("discussion", ("discussion", "limitation", "讨论", "局限")),
            ("background", ("background", "preliminar", "introduction", "背景", "预备", "引言")),
        )
        for section_type, keywords in patterns:
            if any(keyword in lowered for keyword in keywords):
                return section_type
        return "background"

    @classmethod
    def _classify_evidence_type(cls, section: str, text: str) -> str:
        """结合标题和正文识别算法块，容忍 PDF 解析造成的标题错挂。"""
        if cls._looks_like_algorithm_block(text):
            return "algorithm"
        return cls._classify_section_type(section)

    @staticmethod
    def _looks_like_algorithm_block(text: str) -> bool:
        """识别带输入输出、编号步骤或算法图标题的结构化协议正文。"""
        normalized = str(text or "")
        numbered_steps = len(re.findall(r"(?m)^\s*\d{1,3}:\s+", normalized))
        has_contract = bool(re.search(r"(?i)\brequire\s*:|\bensure\s*:", normalized))
        has_algorithm_caption = bool(
            re.search(r"(?i)figure\s+\d+\s*:.*(?:algorithm|protocol|framework|training)", normalized)
        )
        has_step_operations = bool(
            re.search(
                r"(?i)\b(?:jointly compute|locally update|open the|for internal nodes|for leaf nodes|end for)\b",
                normalized,
            )
        )
        return numbered_steps >= 3 and (has_contract or has_algorithm_caption or has_step_operations)

    @classmethod
    def _content_intent_adjustment(
        cls,
        chunk: EvidenceChunk,
        *,
        question_type: str,
        preferred_types: set[str],
    ) -> float:
        """按正文结构提升完整算法/协议块，不依赖论文名或固定章节号。"""
        if question_type != "mechanism" or not cls._looks_like_algorithm_block(chunk.text):
            return 0.0
        numbered_steps = len(re.findall(r"(?m)^\s*\d{1,3}:\s+", chunk.text))
        adjustment = 0.45
        if numbered_steps >= 8:
            adjustment += 0.15
        if re.search(r"(?i)\brequire\s*:.*\bensure\s*:", chunk.text, flags=re.DOTALL):
            adjustment += 0.12
        if re.search(r"(?i)figure\s+\d+\s*:.*(?:protocol|framework|training)", chunk.text):
            adjustment += 0.12
        if preferred_types & {"overview", "framework", "method", "protocol", "algorithm", "implementation"}:
            adjustment += 0.08
        return adjustment

    @staticmethod
    def _section_intent_adjustment(
        *,
        question_type: str,
        section_type: str,
        preferred_types: set[str],
    ) -> float:
        adjustment = 0.22 if section_type in preferred_types else 0.0
        if question_type == "mechanism":
            if section_type in {"method", "framework", "protocol", "algorithm", "implementation", "overview"}:
                adjustment += 0.18
            elif section_type in {"experiment", "result"}:
                adjustment -= 0.15
        elif question_type == "evaluation":
            if section_type in {"experiment", "result", "evaluation"}:
                adjustment += 0.18
        return adjustment

    def plan_retrieval(
        self,
        question: str,
        history: list[dict[str, str]] | None,
        *,
        explicit_paper_ids: list[str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """兼容原入口，并把实际规划委托给独立 QueryPlanningAgent。"""
        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")
        planner = QueryPlanningAgent(
            completion=chat_completion,
            model=model,
            timeout=self.config.request_timeout,
        )
        return planner.plan(question, history, explicit_paper_ids=explicit_paper_ids)

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
        answer_requirements: list[str],
    ) -> str:
        """调用已配置模型，根据证据上下文生成研究回答。"""
        context = self.retriever.build_context(evidence)
        system_prompt = self._load_prompt().replace("{{evidence}}", context)
        if answer_requirements:
            requirements_text = "\n".join(f"- {str(item)[:500]}" for item in answer_requirements if str(item).strip())
            system_prompt = (
                f"{system_prompt}\n\n# 本次回答的核心覆盖目标\n{requirements_text}\n"
                "这些目标用于组织回答；个别非核心实现细节缺失时应明确边界，但不要因此否定已有证据能够支持的整体回答。"
            )
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
