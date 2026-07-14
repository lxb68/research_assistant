"""在安全边界内编排检索、问答和领域树等研究任务。"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from app.agents.domainTree_agent import DomainTreeAgent
from app.agents.error_recovery_agent import ErrorRecoveryAgent, RecoveryExhaustedError
from app.agents.hunter_agent import HunterAgent
from app.agents.research_chat_agent import ResearchChatAgent
from app.core.config import settings
from app.services.model_config import ModelConfigStore
from app.services.run_logger import RunLogger


class OrchestratorAgent:
    """受限编排器：只允许调用已注册的研究工具。"""

    ALLOWED_ACTIONS = {"auto", "chat", "search", "domain_tree"}

    def __init__(self, *, log_callback: Callable[[str], None] | None = None) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.ui_log_callback = log_callback
        self.run_logger = RunLogger(settings.agent_run_log_dir)
        self.log_callback = self._child_log
        self.recovery = ErrorRecoveryAgent(
            max_cycles=settings.error_recovery_max_cycles,
            base_delay_seconds=settings.error_recovery_base_delay_seconds,
            log_callback=self.log_callback,
        )

    async def run(self, task: str, *, action: str = "auto", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行当前代理的主要业务流程并返回结构化结果。"""
        normalized_task = str(task).strip()
        if not normalized_task:
            raise ValueError("编排任务不能为空")
        normalized_action = str(action).strip().lower() or "auto"
        if normalized_action not in self.ALLOWED_ACTIONS:
            raise ValueError(f"不支持的编排动作：{normalized_action}")
        args = arguments or {}
        self.run_logger.log(
            "OrchestratorAgent",
            "研究任务开始",
            event="run_start",
            data={"task": normalized_task, "requestedAction": normalized_action, "arguments": args},
        )
        selected = self._select_action(normalized_task) if normalized_action == "auto" else normalized_action
        self._log(f"编排器已选择 {selected} Agent")

        if selected == "search":
            result, recovery_trace = await self.recovery.execute(
                "HunterAgent 论文搜索",
                lambda: asyncio.to_thread(
                    HunterAgent(log_callback=self.log_callback).run,
                    str(args.get("keyword") or normalized_task),
                    sources=list(args.get("sources") or ["arxiv", "crossref", "open_access"]),
                    limit_per_source=max(1, min(int(args.get("limit_per_source") or 5), 50)),
                    download_pdf=bool(args.get("download_pdf", True)),
                ),
            )
            return {
                "agent": "hunter",
                "action": selected,
                "result": result,
                "recoveryTrace": recovery_trace,
                "runLog": self.run_logger.public_info(),
            }

        if selected == "domain_tree":
            model = ModelConfigStore().build_model_payload()
            if not model:
                raise ValueError("请先配置模型参数")
            project_id = str(args.get("project_id") or "workspace-domain-tree").strip()
            domain_agent = DomainTreeAgent()
            _, recovery_trace = await self.recovery.execute(
                "DomainTreeAgent 领域树生成",
                lambda: domain_agent.handle_domain_tree(
                    project_id,
                    action=str(args.get("domain_action") or "rebuild"),
                    model=model,
                    language=str(args.get("language") or "中文"),
                ),
            )
            result = domain_agent.get_result(project_id)
            return {
                "agent": "domain_tree",
                "action": selected,
                "result": result,
                "recoveryTrace": recovery_trace,
                "runLog": self.run_logger.public_info(),
            }

        return await self._run_research_pipeline(normalized_task, args)

    async def _run_research_pipeline(self, task: str, args: dict[str, Any]) -> dict[str, Any]:
        """依次评估本地证据、补充论文并生成研究回答。"""
        history = list(args.get("history") or [])
        paper_ids = list(args.get("paper_ids") or []) or None
        allow_search = bool(args.get("allow_external_search", True)) and not paper_ids
        trace: list[dict[str, Any]] = []
        research_agent = ResearchChatAgent(log_callback=self.log_callback)

        self._log("正在判断本地知识库证据是否充分")
        evidence, diagnostics = await asyncio.to_thread(
            research_agent.retrieve_evidence,
            task,
            history=history,
            paper_ids=paper_ids,
        )
        sufficient, reasons = self._assess_evidence(diagnostics)
        self.run_logger.log(
            "RAGRetriever",
            "本地证据充分度评估完成",
            event="evidence_assessment",
            data={"diagnostics": diagnostics, "sufficient": sufficient, "reasons": reasons},
        )
        trace.append(
            {
                "step": "local_retrieval",
                "agent": "research_chat",
                "status": "sufficient" if sufficient else "insufficient",
                "diagnostics": diagnostics,
                "reasons": reasons,
            }
        )

        search_result: dict[str, Any] | None = None
        search_error = ""
        if not sufficient and allow_search:
            self._log("本地证据不足，正在调用 HunterAgent 搜索补充论文")
            hunter_agent = HunterAgent(log_callback=self.log_callback)
            requested_search_keyword = str(args.get("search_keyword") or task)
            search_keyword = await asyncio.to_thread(hunter_agent.translate_search_query, requested_search_keyword)
            self.run_logger.log(
                "OrchestratorAgent",
                "已通过后端翻译服务生成英文论文检索词",
                event="search_query",
                data={"originalQuestion": requested_search_keyword, "searchKeyword": search_keyword},
            )
            try:
                search_result, recovery_trace = await self.recovery.execute(
                    "HunterAgent 证据补充搜索",
                    lambda: asyncio.to_thread(
                        hunter_agent.run,
                        search_keyword,
                        sources=list(args.get("sources") or ["arxiv", "crossref", "open_access"]),
                        limit_per_source=max(
                            1,
                            min(
                                int(args.get("limit_per_source") or settings.orchestrator_search_limit_per_source),
                                20,
                            ),
                        ),
                        download_pdf=True,
                    ),
                )
                trace.append(
                    {
                        "step": "evidence_search",
                        "agent": "hunter",
                        "status": "completed",
                        "savedCount": search_result.get("savedCount", 0),
                        "errors": search_result.get("errors", []),
                        "recoveryTrace": recovery_trace,
                    }
                )
                if search_result.get("errors"):
                    self.run_logger.log(
                        "HunterAgent",
                        "论文搜索部分失败；HunterAgent 返回结果但包含数据源错误",
                        event="partial_failure",
                        data={
                            "keyword": search_result.get("keyword"),
                            "searchKeyword": search_result.get("searchKeyword"),
                            "savedCount": search_result.get("savedCount"),
                            "targetCount": search_result.get("targetCount"),
                            "errors": search_result.get("errors"),
                        },
                    )
            except RecoveryExhaustedError as error:
                search_error = str(error)
                trace.append({
                    "step": "evidence_search",
                    "agent": "hunter",
                    "status": "failed",
                    "category": error.decision.category,
                    "message": search_error,
                    "recoveryTrace": error.trace,
                })

            self._log("正在使用补充后的知识库重新评估证据")
            evidence, diagnostics = await asyncio.to_thread(
                research_agent.retrieve_evidence,
                task,
                history=history,
            )
            sufficient, reasons = self._assess_evidence(diagnostics)
            self.run_logger.log(
                "RAGRetriever",
                "补充搜索后的证据充分度评估完成",
                event="evidence_assessment",
                data={"diagnostics": diagnostics, "sufficient": sufficient, "reasons": reasons},
            )
            trace.append(
                {
                    "step": "retrieval_after_search",
                    "agent": "research_chat",
                    "status": "sufficient" if sufficient else "insufficient",
                    "diagnostics": diagnostics,
                    "reasons": reasons,
                }
            )

        if not sufficient:
            required_materials = self._required_materials(task, diagnostics, search_result, search_error)
            self._log("仍缺少足够证据，需要用户补充 PDF 材料")
            return {
                "agent": "orchestrator",
                "action": "request_materials",
                "result": {
                    "status": "needs_materials",
                    "message": "当前知识库和自动检索结果不足以可靠回答该问题，请补充相关 PDF 后重试。",
                    "requiredMaterials": required_materials,
                    "retrievalDiagnostics": diagnostics,
                    "evidencePreview": self._evidence_preview(evidence),
                    "trace": trace,
                    "runLog": self.run_logger.public_info(),
                },
            }

        self._log("证据充分，正在交给 ResearchChatAgent 生成最终回答")
        try:
            result, recovery_trace = await self.recovery.execute(
                "ResearchChatAgent 回答生成",
                lambda: asyncio.to_thread(
                    research_agent.run,
                    task,
                    history=history,
                    paper_ids=paper_ids,
                ),
            )
        except RecoveryExhaustedError as error:
            trace.append({
                "step": "answer",
                "agent": "research_chat",
                "status": "failed",
                "category": error.decision.category,
                "recoveryTrace": error.trace,
            })
            return {
                "agent": "orchestrator",
                "action": "request_user_action",
                "result": {
                    "status": "needs_user_action",
                    "message": str(error),
                    "requiredAction": error.decision.action,
                    "trace": trace,
                    "runLog": self.run_logger.public_info(),
                },
            }
        trace.append({
            "step": "answer",
            "agent": "research_chat",
            "status": "completed",
            "recoveryTrace": recovery_trace,
        })
        return {
            "agent": "orchestrator",
            "action": "chat",
            "result": {**result, "trace": trace, "runLog": self.run_logger.public_info()},
        }

    def _assess_evidence(self, diagnostics: dict[str, Any]) -> tuple[bool, list[str]]:
        """根据检索诊断信息判断当前证据是否充分。"""
        reasons: list[str] = []
        evidence_count = int(diagnostics.get("evidenceCount") or 0)
        distinct_papers = int(diagnostics.get("distinctPaperCount") or 0)
        coverage = float(diagnostics.get("queryCoverage") or 0)
        if evidence_count < settings.orchestrator_min_evidence:
            reasons.append(f"相关证据片段仅 {evidence_count} 条")
        if distinct_papers < min(2, settings.orchestrator_min_evidence):
            reasons.append(f"相关证据仅覆盖 {distinct_papers} 篇文献")
        if coverage < settings.orchestrator_min_query_coverage:
            reasons.append(f"问题关键词覆盖率仅 {coverage:.0%}")
        return not reasons, reasons

    def _required_materials(
        self,
        task: str,
        diagnostics: dict[str, Any],
        search_result: dict[str, Any] | None,
        search_error: str,
    ) -> list[dict[str, str]]:
        """根据证据缺口生成需要用户补充的材料清单。"""
        materials = [
            {
                "type": "core_papers",
                "description": f"与“{task[:120]}”直接相关的核心论文全文 PDF，建议至少 2–3 篇。",
            },
            {
                "type": "methods_or_results",
                "description": "包含研究方法、实验设置、数据集与结果分析的论文 PDF，而不只是摘要或元数据。",
            },
        ]
        if search_error or (search_result and search_result.get("errors")):
            materials.append(
                {
                    "type": "unavailable_sources",
                    "description": "自动检索或下载未成功的目标论文 PDF；可在“浏览数据集”中手动导入。",
                }
            )
        if int(diagnostics.get("paperCount") or 0) == 0:
            materials.insert(
                0,
                {"type": "knowledge_base", "description": "当前知识库没有可检索论文，请先导入并完成 PDF 解析。"},
            )
        return materials

    def _evidence_preview(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """整理可安全返回前端的证据摘要。"""
        return [
            {
                "recordId": item.get("record_id", ""),
                "title": item.get("title", ""),
                "section": item.get("section", ""),
                "score": round(float(item.get("score") or 0), 4),
            }
            for item in evidence[:5]
        ]

    def _select_action(self, task: str) -> str:
        """根据任务文本选择研究问答、搜索或领域树动作。"""
        lowered = task.lower()
        if any(token in lowered for token in ("下载论文", "搜索论文", "检索论文", "search papers", "find papers")):
            return "search"
        if any(
            token in lowered
            for token in ("生成领域树", "重建领域树", "更新领域树", "知识图谱", "domain tree", "knowledge graph")
        ):
            return "domain_tree"
        return "chat"

    def _log(self, message: str) -> None:
        """把运行消息转发给已配置的日志回调。"""
        self.run_logger.log("OrchestratorAgent", message)
        if self.ui_log_callback:
            self.ui_log_callback(message)

    def _child_log(self, message: str) -> None:
        """记录子代理日志并同步给界面回调。"""
        component = "HunterAgent" if message.startswith("[") or "数据源" in message or "论文" in message else "Agent"
        self.run_logger.log(component, message)
        if self.ui_log_callback:
            self.ui_log_callback(message)


__all__ = ["OrchestratorAgent"]
