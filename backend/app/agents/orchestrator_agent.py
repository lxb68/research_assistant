"""在安全边界内编排检索、问答和领域树等研究任务。"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable

from app.agents.domainTree_agent import DomainTreeAgent
from app.agents.evidence_evaluator import EvidenceEvaluator
from app.agents.error_recovery_agent import ErrorRecoveryAgent, RecoveryExhaustedError
from app.agents.hunter_agent import HunterAgent
from app.agents.research_chat_agent import ResearchChatAgent
from app.agents.tool_loop_agent import ObservationReducer, ToolLoopAgent
from app.core.config import settings
from app.services.conversation_context import ConversationContextProjector
from app.services.model_client import chat_completion
from app.services.model_config import ModelConfigStore, SYSTEM_SECURITY_CONSTRAINT
from app.services.run_logger import RunLogger
from app.services.task_control import TaskCancelled, raise_if_task_cancelled
from app.services.retrieval_contracts import requires_semantic_validation
from app.services.retrieval_refiner import RetrievalRefiner
from app.tools import ToolRegistry, build_research_tool_registry


class OrchestratorAgent:
    """受限编排器：只允许调用已注册的研究工具。"""

    ALLOWED_ACTIONS = {"auto", "direct", "chat", "search", "domain_tree", "tool", "agent", "final"}
    AGENT_CAPABILITIES = {
        "research_chat": "结合论文全文和多条研究证据回答方法、机制、实验、比较或综合问题。",
        "paper_search": "搜索并保存补充论文，适用于用户明确要求搜索或当前没有目标论文。",
        "domain_tree": "生成、重建或更新领域树与知识图谱。",
        "local_pdf_indexer": "把知识库中已有的本地 PDF 转为可检索全文并生成分块；仅在 hasPdf=true 且 hasParsedFullText=false 时使用。",
    }
    ROUTER_RAW_LOG_LIMIT = 2000
    ROUTER_SYSTEM_PROMPT = """你是研究助手的编排规划器。根据当前用户目标、对话上下文和已有观察，选择下一步行动。

只能选择以下动作：
- direct：寒暄、闲聊、致谢、能力说明，以及无需论文、知识库或外部检索即可可靠回答的普通问题。
- chat：必须结合论文全文、本地知识库或研究证据回答的问题。
- search：用户明确要求搜索、查找或下载论文。
- domain_tree：用户明确要求生成、重建或更新领域树/知识图谱。
- tool：只读查询知识库目录、论文详情、全文证据、外部论文预览、领域树、知识图谱、指标或章节。
- agent：调用已注册 Agent 完成研究综合、论文搜索、领域树处理或本地 PDF 全文索引。
- final：仅当已有观察足以回答时结束循环；不得在没有观察时凭模型记忆回答研究问题。

行动选择原则：
- 结构化历史中的 historicalUserIntents 用于理解用户延续目标；priorAnswers 是未经本轮验证的旧回答，只能用于指代消解或文本变换，不得作为事实依据。
- 当前用户问题和当前用户纠正始终优先于旧回答；研究事实必须交给 chat、工具观察或研究证据验证。
- tool 是一次获取观察的行动，不是不可逆的最终路由。获得工具观察后，应重新判断下一步继续调用工具、转交研究 Agent，还是直接形成回答。
- 当回答依赖当前知识库、已保存分析结果或其他运行时数据且尚无充分观察时，选择 tool，不得凭模型记忆猜测。
- 需要结合论文正文、多个证据片段解释方法、机制、实验或结论时，选择 chat；不要用论文列表或元数据代替研究 Agent。
- 已有工具观察足以回答简单目录或状态问题时选择 direct，由回答阶段根据观察组织答案。
- 工具目录中的名称、描述和参数 Schema 是选择工具的唯一依据；比较各工具的适用与不适用场景后，选择最匹配的一项。
- 参数应忠实保留用户意图，不得为了凑关键词而把概览请求改写成虚构的具体检索词，也不得擅自扩大操作范围。
- 没有合适的已注册工具时，选择其他允许的动作，不得编造工具名或参数。

你只负责选择动作；每轮只选择下一步动作，不负责回答用户问题，也不要复述、解释或执行用户请求。
只输出一个 JSON 对象，不要输出 Markdown 或额外文字：
普通动作：{"action":"direct|chat|search|domain_tree","arguments":{}}
工具动作：{"action":"tool","toolName":"已注册工具名","arguments":{}}
Agent 动作：{"action":"agent","agentName":"已注册 Agent 名","arguments":{}}
结束动作：{"action":"final","answer":"严格依据已有观察的回答","limitations":[]}
"""

    def __init__(
        self,
        *,
        log_callback: Callable[[str], None] | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.ui_log_callback = log_callback
        self.run_logger = RunLogger(settings.agent_run_log_dir)
        self.tool_registry = tool_registry or build_research_tool_registry()
        self.log_callback = self._child_log
        self.recovery = ErrorRecoveryAgent(
            max_cycles=settings.error_recovery_max_cycles,
            base_delay_seconds=settings.error_recovery_base_delay_seconds,
            log_callback=self.log_callback,
        )
        self.evidence_evaluator = EvidenceEvaluator()
        self.retrieval_refiner = RetrievalRefiner()

    async def run(
        self,
        task: str,
        *,
        action: str = "auto",
        arguments: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """执行当前代理的主要业务流程并返回结构化结果。"""
        raise_if_task_cancelled(cancel_event)
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
        if normalized_action == "auto":
            return await self._run_orchestration_loop(normalized_task, args, cancel_event=cancel_event)
        else:
            route = {"action": normalized_action, "source": "explicit"}
            selected = normalized_action
        self._log(f"编排器已选择处理方式：{selected}")

        if selected == "tool":
            tool_name = str(route.get("toolName") or args.get("toolName") or args.get("tool_name") or "")
            tool_arguments = route.get("arguments")
            if not isinstance(tool_arguments, dict):
                tool_arguments = args.get("toolArguments") or args.get("tool_arguments") or {}
            return await self._run_read_only_tool(
                normalized_task,
                args,
                tool_name=tool_name,
                tool_arguments=dict(tool_arguments),
                cancel_event=cancel_event,
            )

        if selected == "direct":
            answer = await self._answer_direct(normalized_task, args, cancel_event=cancel_event)
            return {
                "agent": "orchestrator",
                "action": "direct",
                "result": {
                    "answer": answer,
                    "sources": [],
                    "trace": [
                        {
                            "step": "routing",
                            "agent": "orchestrator",
                            "status": "completed",
                            "selectedAction": "direct",
                        }
                    ],
                    "runLog": self.run_logger.public_info(),
                },
            }

        if selected == "search":
            result, recovery_trace = await self.recovery.execute(
                "HunterAgent 论文搜索",
                lambda: asyncio.to_thread(
                    HunterAgent(log_callback=self.log_callback).run,
                    str(args.get("keyword") or normalized_task),
                    sources=list(args.get("sources") or ["arxiv", "crossref", "open_access"]),
                    limit_per_source=max(1, min(int(args.get("limit_per_source") or 5), 50)),
                    download_pdf=bool(args.get("download_pdf", True)),
                    cancel_event=cancel_event,
                ),
                cancel_event=cancel_event,
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
                    language=str(args.get("language") or "auto"),
                    cancel_event=cancel_event,
                ),
                cancel_event=cancel_event,
            )
            result = domain_agent.get_result(project_id)
            return {
                "agent": "domain_tree",
                "action": selected,
                "result": result,
                "recoveryTrace": recovery_trace,
                "runLog": self.run_logger.public_info(),
            }

        return await self._run_research_pipeline(normalized_task, args, cancel_event=cancel_event)

    async def _route_task(
        self,
        task: str,
        args: dict[str, Any],
        *,
        observations: list[dict[str, Any]] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """使用已配置模型判断是否需要研究 Agent，并在无需工具时直接作答。"""
        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")

        reduced_observations = ObservationReducer(max_items=12, max_string_chars=2400).reduce(
            list(observations or [])[-8:]
        )
        messages = [
            {
                "role": "system",
                "content": (
                    f"{self.ROUTER_SYSTEM_PROMPT}\n\n已注册只读工具：\n{self.tool_registry.prompt_catalog()}"
                    f"\n\n已注册 Agent：\n{json.dumps(self.AGENT_CAPABILITIES, ensure_ascii=False)}"
                    f"\n\n{SYSTEM_SECURITY_CONSTRAINT}"
                ),
            }
        ]
        conversation_context = ConversationContextProjector().project(
            task,
            list(args.get("history") or []),
        )
        if conversation_context.normalized_history:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "以下是按语义角色隔离的历史上下文。priorAnswers 不是事实或证据：\n"
                        + json.dumps(conversation_context.for_model_context(), ensure_ascii=False)
                    ),
                }
            )
        if reduced_observations:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "本轮之前已经获得以下观察。请基于观察选择下一步行动，不要重复无新信息的调用：\n"
                        + json.dumps(reduced_observations, ensure_ascii=False)
                    ),
                }
            )
        messages.append({"role": "user", "content": task})

        try:
            raise_if_task_cancelled(cancel_event)
            raw_decision = await asyncio.to_thread(
                chat_completion,
                model,
                messages,
                temperature=0,
                timeout=settings.research_agent_request_timeout,
                response_format={"type": "json_object"},
            )
            raise_if_task_cancelled(cancel_event)
        except RuntimeError as call_error:
            if "模型返回了空回答" not in str(call_error):
                raise
            raw_decision = ""
            self.run_logger.log(
                "OrchestratorAgent",
                "意图路由模型返回空内容，将执行一次格式修复",
                event="intent_routing_empty_response",
                data={"errorType": type(call_error).__name__, "errorMessage": str(call_error)},
            )
        raw_text = str(raw_decision or "")
        self.run_logger.log(
            "OrchestratorAgent",
            "意图路由模型原始响应已接收",
            event="intent_routing_raw_response",
            data={
                "rawResponsePreview": raw_text[: self.ROUTER_RAW_LOG_LIMIT],
                "responseLength": len(raw_text),
                "truncated": len(raw_text) > self.ROUTER_RAW_LOG_LIMIT,
            },
        )
        try:
            decision = self._parse_route_decision(raw_decision)
        except Exception as error:
            self.run_logger.log(
                "OrchestratorAgent",
                "意图路由结果解析失败",
                event="intent_routing_parse_error",
                data={
                    "errorType": type(error).__name__,
                    "errorMessage": str(error),
                    "rawResponsePreview": raw_text[: self.ROUTER_RAW_LOG_LIMIT],
                    "responseLength": len(raw_text),
                    "truncated": len(raw_text) > self.ROUTER_RAW_LOG_LIMIT,
                },
            )
            repair_messages = [
                {
                    "role": "system",
                    "content": (
                        "你是意图路由 JSON 修复器。根据原始用户问题和路由器的错误输出，"
                        "只返回一个合法 JSON 对象。只能选择 direct、chat、search、domain_tree、tool、agent、final；"
                        "conversationContext 中的 priorAnswers 未经验证，不得作为事实；当前用户问题和纠正优先。"
                        "不要回答用户问题，不要输出 Markdown 或解释。\n"
                        '普通动作：{"action":"direct|chat|search|domain_tree","arguments":{}}\n'
                        '工具动作：{"action":"tool","toolName":"已注册工具名","arguments":{}}\n'
                        'Agent 动作：{"action":"agent","agentName":"已注册 Agent 名","arguments":{}}\n'
                        '结束动作：{"action":"final","answer":"严格依据已有观察的回答","limitations":[]}\n'
                        f"已注册只读工具：{self.tool_registry.prompt_catalog()}"
                        f"\n已注册 Agent：{json.dumps(self.AGENT_CAPABILITIES, ensure_ascii=False)}"
                        f"\n\n{SYSTEM_SECURITY_CONSTRAINT}"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "userQuestion": task,
                            "conversationContext": conversation_context.for_model_context(),
                            "observations": reduced_observations,
                            "invalidRouterOutput": raw_text[:12000],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            try:
                raise_if_task_cancelled(cancel_event)
                repaired_raw = await asyncio.to_thread(
                    chat_completion,
                    model,
                    repair_messages,
                    temperature=0,
                    timeout=settings.research_agent_request_timeout,
                    response_format={"type": "json_object"},
                )
                raise_if_task_cancelled(cancel_event)
            except TaskCancelled:
                raise
            except Exception as repair_call_error:
                self.run_logger.log(
                    "OrchestratorAgent",
                    "意图路由格式修复调用失败",
                    event="intent_routing_repair_error",
                    data={
                        "errorType": type(repair_call_error).__name__,
                        "errorMessage": str(repair_call_error),
                    },
                )
                raise
            repaired_text = str(repaired_raw or "")
            self.run_logger.log(
                "OrchestratorAgent",
                "意图路由格式修复响应已接收",
                event="intent_routing_repair_raw_response",
                data={
                    "rawResponsePreview": repaired_text[: self.ROUTER_RAW_LOG_LIMIT],
                    "responseLength": len(repaired_text),
                    "truncated": len(repaired_text) > self.ROUTER_RAW_LOG_LIMIT,
                },
            )
            try:
                decision = self._parse_route_decision(repaired_raw)
            except Exception as repair_error:
                self.run_logger.log(
                    "OrchestratorAgent",
                    "意图路由格式修复失败",
                    event="intent_routing_repair_error",
                    data={
                        "errorType": type(repair_error).__name__,
                        "errorMessage": str(repair_error),
                        "rawResponsePreview": repaired_text[: self.ROUTER_RAW_LOG_LIMIT],
                        "responseLength": len(repaired_text),
                        "truncated": len(repaired_text) > self.ROUTER_RAW_LOG_LIMIT,
                    },
                )
                raise
        self.run_logger.log(
            "OrchestratorAgent",
            "意图路由完成",
            event="intent_routing",
            data={"selectedAction": decision["action"], "toolName": decision.get("toolName", "")},
        )
        return decision

    async def _run_orchestration_loop(
        self,
        task: str,
        args: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """在工具与研究 Agent 之间执行有界的行动—观察—再规划循环。"""
        observations: list[dict[str, Any]] = []
        seen_tool_actions: set[str] = set()
        seen_agent_actions: set[str] = set()
        max_rounds = max(2, min(int(settings.orchestrator_max_action_rounds), 8))

        for round_index in range(1, max_rounds + 1):
            raise_if_task_cancelled(cancel_event)
            decision = await self._route_task(
                task,
                args,
                observations=observations,
                cancel_event=cancel_event,
            )
            selected = str(decision["action"])
            self._log(f"编排循环第 {round_index} 轮选择：{selected}")
            self.run_logger.log(
                "OrchestratorAgent",
                "编排行动已选择",
                event="orchestration_action",
                data={
                    "round": round_index,
                    "selectedAction": selected,
                    "toolName": decision.get("toolName", ""),
                    "observationCount": len(observations),
                },
            )

            decision_arguments = decision.get("arguments")
            merged_args = {**args, **(decision_arguments if isinstance(decision_arguments, dict) else {})}

            if selected == "tool":
                tool_name = str(decision.get("toolName") or "")
                tool_arguments = dict(decision.get("arguments") or {})
                signature = json.dumps(
                    {"toolName": tool_name, "arguments": tool_arguments},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if signature in seen_tool_actions:
                    observation = {
                        "round": round_index,
                        "kind": "tool",
                        "target": tool_name,
                        "ok": False,
                        "errorType": "RepeatedAction",
                        "error": "相同工具和参数已经执行，必须根据现有观察选择其他行动。",
                    }
                else:
                    seen_tool_actions.add(signature)
                    observation = await self._execute_tool_step(
                        round_index,
                        tool_name,
                        tool_arguments,
                        cancel_event=cancel_event,
                    )
                observations.append(observation)
                continue

            if selected == "agent":
                agent_name = str(decision.get("agentName") or "")
                agent_arguments = dict(decision.get("arguments") or {})
                signature = json.dumps(
                    {"agentName": agent_name, "arguments": agent_arguments},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if signature in seen_agent_actions:
                    observations.append(
                        {
                            "round": round_index,
                            "kind": "agent",
                            "target": agent_name,
                            "ok": False,
                            "errorType": "RepeatedAction",
                            "error": "相同 Agent 和参数已经执行，必须根据现有观察选择其他行动。",
                        }
                    )
                    continue
                seen_agent_actions.add(signature)
                if agent_name == "research_chat":
                    return await self._run_research_pipeline(task, merged_args, cancel_event=cancel_event)
                if agent_name == "paper_search":
                    return await self.run(
                        task,
                        action="search",
                        arguments=merged_args,
                        cancel_event=cancel_event,
                    )
                if agent_name == "domain_tree":
                    return await self.run(
                        task,
                        action="domain_tree",
                        arguments=merged_args,
                        cancel_event=cancel_event,
                    )
                observation = await self._execute_local_pdf_index_step(
                    round_index,
                    agent_arguments,
                    cancel_event=cancel_event,
                )
                observations.append(observation)
                continue

            if selected == "chat":
                return await self._run_research_pipeline(task, merged_args, cancel_event=cancel_event)

            if selected == "direct":
                answer = (
                    await self._answer_from_observations(task, observations, cancel_event=cancel_event)
                    if observations
                    else await self._answer_direct(task, args, cancel_event=cancel_event)
                )
                return self._direct_loop_result(answer, observations)

            if selected == "final":
                if not observations:
                    answer = await self._answer_direct(task, args, cancel_event=cancel_event)
                else:
                    answer = str(decision.get("answer") or "").strip()
                    limitations = [
                        str(item).strip()
                        for item in decision.get("limitations") or []
                        if str(item).strip()
                    ]
                    if limitations:
                        answer += "\n\n信息边界：\n" + "\n".join(f"- {item}" for item in limitations)
                return self._direct_loop_result(answer, observations)

            if selected in {"search", "domain_tree"}:
                return await self.run(
                    task,
                    action=selected,
                    arguments=merged_args,
                    cancel_event=cancel_event,
                )

        answer = await self._answer_from_observations(
            task,
            observations,
            cancel_event=cancel_event,
            reached_limit=True,
        )
        return self._direct_loop_result(answer, observations, stop_reason="max_rounds")

    async def _execute_tool_step(
        self,
        round_index: int,
        tool_name: str,
        tool_arguments: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """执行单次只读工具调用，并转换为统一观察。"""
        raise_if_task_cancelled(cancel_event)
        try:
            result = await asyncio.to_thread(self.tool_registry.execute, tool_name, tool_arguments)
            raise_if_task_cancelled(cancel_event)
            observation = {
                "round": round_index,
                "kind": "tool",
                "target": tool_name,
                "arguments": tool_arguments,
                "ok": True,
                "result": result,
            }
        except TaskCancelled:
            raise
        except Exception as error:
            observation = {
                "round": round_index,
                "kind": "tool",
                "target": tool_name,
                "arguments": tool_arguments,
                "ok": False,
                "errorType": type(error).__name__,
                "error": str(error)[:1000],
            }
        self.run_logger.log(
            "OrchestratorAgent",
            "编排行动观察已记录",
            event="orchestration_observation",
            data={
                "round": round_index,
                "kind": "tool",
                "target": tool_name,
                "ok": observation["ok"],
            },
        )
        return observation

    async def _execute_local_pdf_index_step(
        self,
        round_index: int,
        arguments: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """执行本地 PDF 全文索引 Agent，并把结果记录为可继续规划的观察。"""
        record_id = str(arguments.get("record_id") or arguments.get("recordId") or "").strip()
        try:
            if not record_id:
                raise ValueError("local_pdf_indexer 缺少 record_id")
            indexed = await asyncio.to_thread(
                HunterAgent(log_callback=self.log_callback).index_saved_pdf_text,
                record_id,
                cancel_event=cancel_event,
            )
            raise_if_task_cancelled(cancel_event)
            observation = {
                "round": round_index,
                "kind": "agent",
                "target": "local_pdf_indexer",
                "arguments": {"record_id": record_id},
                "ok": True,
                "result": {
                    "recordId": indexed.get("id", record_id),
                    "title": indexed.get("title", ""),
                    "hasParsedFullText": bool(indexed.get("markdownPath")),
                    "splitChunkCount": indexed.get("splitChunkCount", 0),
                    "fullTextIndexedBy": indexed.get("fullTextIndexedBy", ""),
                },
            }
        except TaskCancelled:
            raise
        except Exception as error:
            observation = {
                "round": round_index,
                "kind": "agent",
                "target": "local_pdf_indexer",
                "arguments": {"record_id": record_id},
                "ok": False,
                "errorType": type(error).__name__,
                "error": str(error)[:1000],
            }
        self.run_logger.log(
            "OrchestratorAgent",
            "PDF 全文索引观察已记录",
            event="orchestration_observation",
            data={
                "round": round_index,
                "kind": "agent",
                "target": "local_pdf_indexer",
                "ok": observation["ok"],
            },
        )
        return observation

    async def _answer_from_observations(
        self,
        task: str,
        observations: list[dict[str, Any]],
        *,
        cancel_event: threading.Event | None = None,
        reached_limit: bool = False,
    ) -> str:
        """只依据编排循环的观察结果形成最终回答。"""
        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")
        reduced = ObservationReducer(max_items=16, max_string_chars=4000).reduce(observations)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是研究编排循环的回答器。只能使用提供的工具观察回答，不得补充模型记忆或猜测。"
                    "如果观察不足，明确说明已有信息、缺口和下一步所需材料。"
                    "不要声称 hasParsedFullText=false 等于没有 PDF。"
                    + ("编排循环已达到轮数上限，必须给出有边界的结果。" if reached_limit else "")
                    + f"\n\n{SYSTEM_SECURITY_CONSTRAINT}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"question": task, "observations": reduced}, ensure_ascii=False),
            },
        ]
        raise_if_task_cancelled(cancel_event)
        answer = await asyncio.to_thread(
            chat_completion,
            model,
            messages,
            temperature=0.1,
            timeout=settings.research_agent_request_timeout,
        )
        raise_if_task_cancelled(cancel_event)
        return answer

    def _direct_loop_result(
        self,
        answer: str,
        observations: list[dict[str, Any]],
        *,
        stop_reason: str = "completed",
    ) -> dict[str, Any]:
        """构造编排循环基于观察形成的终态结果。"""
        sources = self._tool_sources_from_orchestration_observations(observations)
        return {
            "agent": "orchestrator",
            "action": "direct" if not observations else "tool",
            "result": {
                "answer": answer,
                "sources": sources,
                "retrievedSources": sources,
                "tool": self._first_tool_descriptor(observations),
                "toolResult": self._first_tool_result(observations),
                "orchestrationLoop": {
                    "rounds": len(observations),
                    "stopReason": stop_reason,
                    "observations": ObservationReducer().reduce(observations),
                },
                "trace": [
                    {
                        "step": "orchestration_action",
                        "agent": "orchestrator",
                        "status": "completed" if item.get("ok") else "failed",
                        "target": item.get("target", ""),
                    }
                    for item in observations
                ],
                "runLog": self.run_logger.public_info(),
            },
        }

    @staticmethod
    def _first_tool_descriptor(observations: list[dict[str, Any]]) -> dict[str, Any]:
        """保留旧版工具响应字段，便于现有前端与调用方平滑迁移。"""
        for item in observations:
            if item.get("kind") == "tool":
                return {"name": item.get("target", ""), "arguments": item.get("arguments", {})}
        return {}

    @staticmethod
    def _first_tool_result(observations: list[dict[str, Any]]) -> dict[str, Any]:
        """返回编排循环中第一条工具观察结果。"""
        for item in observations:
            if item.get("kind") == "tool":
                return item.get("result", {}) if item.get("ok") else {
                    "errorType": item.get("errorType", ""),
                    "error": item.get("error", ""),
                }
        return {}

    def _tool_sources_from_orchestration_observations(
        self,
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """适配统一编排观察到现有来源汇总协议。"""
        legacy = [
            {
                "toolName": item.get("target", ""),
                "ok": item.get("ok", False),
                "result": item.get("result", {}),
            }
            for item in observations
            if item.get("kind") == "tool"
        ]
        return self._tool_sources_from_observations(legacy)

    async def _run_read_only_tool(
        self,
        task: str,
        args: dict[str, Any],
        *,
        tool_name: str,
        tool_arguments: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """进入有界行动—观察循环，直到工具信息足以形成回答。"""
        raise_if_task_cancelled(cancel_event)
        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")

        def loop_event(component: str, message: str, event: str, data: dict[str, Any]) -> None:
            self.run_logger.log(component, message, event=event, data=data)
            if self.ui_log_callback:
                self.ui_log_callback(message)

        loop = ToolLoopAgent(
            self.tool_registry,
            model=model,
            completion=chat_completion,
            max_steps=4,
            timeout=settings.research_agent_request_timeout,
            event_callback=loop_event,
        )
        outcome = await loop.run(
            task,
            history=self._clean_history(list(args.get("history") or [])),
            initial_tool_name=tool_name,
            initial_arguments=tool_arguments,
            cancel_event=cancel_event,
        )
        observations = list(outcome.pop("_observations", []))
        first_observation = observations[0] if observations else {}
        first_result = (
            first_observation.get("result")
            if first_observation.get("ok") and isinstance(first_observation.get("result"), dict)
            else {
                "errorType": first_observation.get("errorType", ""),
                "error": first_observation.get("error", ""),
            }
        )
        sources = self._tool_sources_from_observations(observations)
        loop_trace = [
            {
                "step": "tool_execution",
                "agent": "tool_loop",
                "status": "completed" if observation.get("ok") else "failed",
                "toolName": observation.get("toolName", ""),
            }
            for observation in observations
        ]
        return {
            "agent": "orchestrator",
            "action": "tool",
            "result": {
                "answer": outcome["answer"],
                "limitations": outcome.get("limitations", []),
                "sources": sources,
                "tool": {"name": tool_name, "arguments": tool_arguments},
                "toolResult": first_result,
                "toolTrace": outcome.get("toolTrace", []),
                "toolLoop": {
                    "steps": outcome.get("steps", len(observations)),
                    "stopReason": outcome.get("stopReason", "completed"),
                },
                "trace": [
                    {"step": "routing", "agent": "orchestrator", "status": "completed", "selectedAction": "tool"},
                    *loop_trace,
                ],
                "runLog": self.run_logger.public_info(),
            },
        }

    @staticmethod
    def _tool_sources(tool_name: str, result: dict[str, Any]) -> list[dict[str, Any]]:
        """把文献类工具结果映射为前端可复用的来源结构。"""
        if tool_name == "search_knowledge_base":
            items = result.get("results") or []
        elif tool_name == "get_knowledge_base_paper":
            items = [result.get("paper")] if isinstance(result.get("paper"), dict) else []
        elif tool_name == "list_knowledge_base_papers":
            items = result.get("items") or []
        else:
            return []
        sources: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            sources.append(
                {
                    "index": index,
                    "record_id": item.get("recordId") or item.get("record_id") or "",
                    "title": item.get("title", ""),
                    "section": item.get("section", ""),
                    "chunk_index": item.get("chunkIndex") or item.get("chunk_index") or 0,
                    "excerpt": item.get("excerpt") or item.get("abstract") or "",
                    "source": item.get("source", ""),
                    "url": item.get("url", ""),
                }
            )
        return sources

    def _tool_sources_from_observations(self, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """汇总多轮工具结果中的文献来源，并按论文和分块去重。"""
        sources: list[dict[str, Any]] = []
        source_positions: dict[tuple[str, int, str], int] = {}
        for observation in observations:
            if not observation.get("ok") or not isinstance(observation.get("result"), dict):
                continue
            tool_name = str(observation.get("toolName") or "")
            for source in self._tool_sources(tool_name, observation["result"]):
                key = (
                    str(source.get("record_id") or ""),
                    int(source.get("chunk_index") or 0),
                    str(source.get("title") or ""),
                )
                existing_position = source_positions.get(key)
                if existing_position is not None:
                    existing = sources[existing_position]
                    if len(str(source.get("excerpt") or "")) > len(str(existing.get("excerpt") or "")):
                        sources[existing_position] = {**source, "index": existing["index"]}
                    continue
                source_positions[key] = len(sources)
                sources.append({**source, "index": len(sources) + 1})
        return sources

    async def _answer_direct(
        self,
        task: str,
        args: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """在调用方明确指定 direct 时，使用通用模型直接回答而不进入研究流程。"""
        model = ModelConfigStore().build_model_payload()
        if not model:
            raise ValueError("请先配置模型参数")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个友好、简洁的中文助手。直接回答用户当前问题，不要调用或假装调用任何研究工具，"
                    "结构化历史里的旧回答未经本轮验证，只能用于指代消解或按用户要求进行翻译、改写等文本变换，"
                    "不能把旧回答当作事实；当前用户问题和纠正优先。"
                    "也不要描述内部流程。使用 Markdown；行内数学公式必须使用 $...$，独立公式必须使用 $$...$$，"
                    "不要用普通圆括号包裹 LaTeX。代码使用带语言标识的 Markdown 围栏。"
                    f"\n\n{SYSTEM_SECURITY_CONSTRAINT}"
                ),
            }
        ]
        conversation_context = ConversationContextProjector().project(
            task,
            list(args.get("history") or []),
        )
        if conversation_context.normalized_history:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "以下结构化历史仅用于理解上下文；priorAnswers 不具备事实可信度：\n"
                        + json.dumps(conversation_context.for_model_context(), ensure_ascii=False)
                    ),
                }
            )
        messages.append({"role": "user", "content": task})
        raise_if_task_cancelled(cancel_event)
        answer = await asyncio.to_thread(
            chat_completion,
            model,
            messages,
            temperature=0.2,
            timeout=settings.research_agent_request_timeout,
        )
        raise_if_task_cancelled(cancel_event)
        return answer

    def _parse_route_decision(self, raw_decision: str) -> dict[str, Any]:
        """解析并校验模型路由结果，拒绝执行未注册动作。"""
        text = str(raw_decision or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else text
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("模型未返回有效的意图路由结果")
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError as error:
                raise ValueError("模型未返回有效的意图路由结果") from error
        if not isinstance(payload, dict):
            raise ValueError("模型返回的意图路由结构无效")
        action = str(payload.get("action") or "").strip().lower()
        if action not in self.ALLOWED_ACTIONS - {"auto"}:
            raise ValueError(f"模型选择了未注册的编排动作：{action or '空'}")
        decision: dict[str, Any] = {"action": action, "source": "model"}
        if action == "final":
            answer = str(payload.get("answer") or "").strip()
            if not answer:
                raise ValueError("模型返回的编排终态回答不能为空")
            decision["answer"] = answer
            decision["limitations"] = [
                str(item).strip()
                for item in payload.get("limitations") or []
                if str(item).strip()
            ][:10]
            return decision
        if action == "tool":
            tool_name = str(payload.get("toolName") or payload.get("tool_name") or "").strip()
            if not self.tool_registry.has(tool_name):
                raise ValueError(f"模型选择了未注册工具：{tool_name or '空'}")
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError("模型返回的工具参数必须是对象")
            decision.update({"toolName": tool_name, "arguments": arguments})
        elif action == "agent":
            agent_name = str(payload.get("agentName") or payload.get("agent_name") or "").strip()
            if agent_name not in self.AGENT_CAPABILITIES:
                raise ValueError(f"模型选择了未注册 Agent：{agent_name or '空'}")
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError("模型返回的 Agent 参数必须是对象")
            decision.update({"agentName": agent_name, "arguments": arguments})
        elif isinstance(payload.get("arguments"), dict):
            decision["arguments"] = dict(payload["arguments"])
        return decision

    def _clean_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """过滤前端生成的失败占位消息，避免其污染后续路由和回答。"""
        return ConversationContextProjector.normalize_history(history)

    async def _run_research_pipeline(
        self,
        task: str,
        args: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """依次评估本地证据、补充论文并生成研究回答。"""
        history = self._clean_history(list(args.get("history") or []))
        paper_ids = list(args.get("paper_ids") or []) or None
        trace: list[dict[str, Any]] = []
        research_agent = ResearchChatAgent(log_callback=self.log_callback)

        try:
            raise_if_task_cancelled(cancel_event)
            plan, raw_plan = await asyncio.to_thread(
                research_agent.plan_retrieval,
                task,
                history,
                explicit_paper_ids=paper_ids,
            )
            raise_if_task_cancelled(cancel_event)
            self.run_logger.log(
                "OrchestratorAgent",
                "上下文查询规划模型原始响应已接收",
                event="query_planning_raw_response",
                data={
                    "rawResponsePreview": raw_plan[: self.ROUTER_RAW_LOG_LIMIT],
                    "responseLength": len(raw_plan),
                    "truncated": len(raw_plan) > self.ROUTER_RAW_LOG_LIMIT,
                },
            )
        except TaskCancelled:
            raise
        except Exception as error:
            raw_plan = str(getattr(error, "raw_response", "") or "")
            self.run_logger.log(
                "OrchestratorAgent",
                "上下文查询规划失败",
                event="query_planning_error",
                data={
                    "errorType": type(error).__name__,
                    "errorMessage": str(error),
                    "rawResponsePreview": raw_plan[: self.ROUTER_RAW_LOG_LIMIT],
                    "responseLength": len(raw_plan),
                    "truncated": len(raw_plan) > self.ROUTER_RAW_LOG_LIMIT,
                },
            )
            has_grounded_history = any(message.get("sources") for message in history if message.get("role") == "assistant")
            if has_grounded_history:
                return {
                    "agent": "orchestrator",
                    "action": "request_user_action",
                    "result": {
                        "status": "needs_user_action",
                        "message": "暂时无法可靠解析这条追问所指的论文或片段，请补充论文标题、章节或引用内容后重试。",
                        "requiredAction": "clarify_research_reference",
                        "trace": [{"step": "query_planning", "status": "failed", "error": str(error)}],
                        "runLog": self.run_logger.public_info(),
                    },
                }
            plan = {
                "standaloneQuestion": task,
                "questionType": "simple_fact",
                "complexity": "simple",
                "targetPaperIds": list(paper_ids or []),
                "targetChunks": [],
                "retrievalFacets": [
                    {"id": "facet-1", "goal": task, "query": task, "preferredSectionTypes": []},
                ],
                "answerRequirements": [],
                "requiresIterativeRetrieval": False,
                "targetEvidenceCount": settings.orchestrator_min_evidence,
                "needsClarification": False,
                "clarificationQuestion": "",
                "planningFallback": True,
            }

        self.run_logger.log(
            "OrchestratorAgent",
            "上下文查询规划完成",
            event="query_planning",
            data=plan,
        )
        trace.append({"step": "query_planning", "agent": "query_planning", "status": "completed", **plan})
        if plan.get("needsClarification"):
            self._log("当前研究追问存在无法唯一确定的指代，需要用户澄清")
            return {
                "agent": "orchestrator",
                "action": "request_user_action",
                "result": {
                    "status": "needs_user_action",
                    "message": str(plan.get("clarificationQuestion") or "请明确你指的是哪篇文献或哪个片段。"),
                    "requiredAction": "clarify_research_reference",
                    "trace": trace,
                    "runLog": self.run_logger.public_info(),
                },
            }

        paper_ids = list(plan.get("targetPaperIds") or paper_ids or []) or None
        target_chunks = list(plan.get("targetChunks") or [])
        retrieval_facets = list(plan.get("retrievalFacets") or [])
        question_type = str(plan.get("questionType") or "simple_fact")
        target_evidence_count = int(plan.get("targetEvidenceCount") or settings.orchestrator_min_evidence)
        allow_search = bool(args.get("allow_external_search", True)) and not paper_ids and not target_chunks
        retrieval_query = str(plan.get("standaloneQuestion") or task).strip()

        self._log("正在判断本地知识库证据是否充分")
        raise_if_task_cancelled(cancel_event)
        evidence, diagnostics = await asyncio.to_thread(
            research_agent.retrieve_evidence,
            task,
            history=history,
            paper_ids=paper_ids,
            retrieval_query=retrieval_query,
            target_chunks=target_chunks,
            retrieval_facets=retrieval_facets,
            question_type=question_type,
            target_evidence_count=target_evidence_count,
        )
        raise_if_task_cancelled(cancel_event)
        evaluation = await self._evaluate_retrieved_evidence(
            diagnostics,
            evidence=evidence,
            plan=plan,
            required_paper_ids=paper_ids,
            required_chunk_refs=target_chunks,
        )
        sufficient = bool(evaluation["sufficient"])
        reasons = list(evaluation["reasons"])
        self.run_logger.log(
            "RAGRetriever",
            "本地证据充分度评估完成",
            event="evidence_assessment",
            data={
                "diagnostics": diagnostics,
                "sufficient": sufficient,
                "reasons": reasons,
                "evidencePreview": self._evidence_preview(evidence),
                "semanticEvaluation": {
                    "facetAssessments": evaluation.get("facetAssessments", []),
                    "requirementAssessments": evaluation.get("requirementAssessments", []),
                },
            },
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

        max_retrieval_rounds = max(1, min(settings.orchestrator_max_retrieval_rounds, 3))
        refinement_facets = self.retrieval_refiner.refine(plan, evaluation) if not sufficient else []
        if not sufficient and max_retrieval_rounds > 1 and refinement_facets:
                self._log("首轮证据存在覆盖缺口，正在执行一次补偿检索")
                raise_if_task_cancelled(cancel_event)
                evidence, diagnostics = await asyncio.to_thread(
                    research_agent.retrieve_evidence,
                    task,
                    history=history,
                    paper_ids=paper_ids,
                    retrieval_query=retrieval_query,
                    target_chunks=target_chunks,
                    retrieval_facets=refinement_facets,
                    question_type=question_type,
                    target_evidence_count=target_evidence_count,
                    existing_evidence=evidence,
                )
                raise_if_task_cancelled(cancel_event)
                evaluation = await self._evaluate_retrieved_evidence(
                    diagnostics,
                    evidence=evidence,
                    plan=plan,
                    required_paper_ids=paper_ids,
                    required_chunk_refs=target_chunks,
                )
                sufficient = bool(evaluation["sufficient"])
                reasons = list(evaluation["reasons"])
                self.run_logger.log(
                    "RAGRetriever",
                    "补偿检索后的证据充分度评估完成",
                    event="evidence_refinement_assessment",
                    data={
                        "refinementFacets": refinement_facets,
                        "diagnostics": diagnostics,
                        "sufficient": sufficient,
                        "reasons": reasons,
                        "evidencePreview": self._evidence_preview(evidence),
                    },
                )
                trace.append(
                    {
                        "step": "retrieval_refinement",
                        "agent": "query_planning",
                        "status": "sufficient" if sufficient else "insufficient",
                        "facets": refinement_facets,
                        "diagnostics": diagnostics,
                        "reasons": reasons,
                    }
                )

        search_result: dict[str, Any] | None = None
        search_error = ""
        if not sufficient and allow_search:
            self._log("本地证据不足，正在调用 HunterAgent 搜索补充论文")
            hunter_agent = HunterAgent(log_callback=self.log_callback)
            requested_search_keyword = str(args.get("search_keyword") or retrieval_query)
            raise_if_task_cancelled(cancel_event)
            search_keyword = await asyncio.to_thread(hunter_agent.translate_search_query, requested_search_keyword)
            raise_if_task_cancelled(cancel_event)
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
                        cancel_event=cancel_event,
                    ),
                    cancel_event=cancel_event,
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
            raise_if_task_cancelled(cancel_event)
            evidence, diagnostics = await asyncio.to_thread(
                research_agent.retrieve_evidence,
                task,
                history=history,
                retrieval_query=retrieval_query,
                retrieval_facets=retrieval_facets,
                question_type=question_type,
                target_evidence_count=target_evidence_count,
                existing_evidence=evidence,
            )
            raise_if_task_cancelled(cancel_event)
            evaluation = await self._evaluate_retrieved_evidence(
                diagnostics,
                evidence=evidence,
                plan=plan,
            )
            sufficient = bool(evaluation["sufficient"])
            reasons = list(evaluation["reasons"])
            self.run_logger.log(
                "RAGRetriever",
                "补充搜索后的证据充分度评估完成",
                event="evidence_assessment",
                data={
                    "diagnostics": diagnostics,
                    "sufficient": sufficient,
                    "reasons": reasons,
                    "evidencePreview": self._evidence_preview(evidence),
                },
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

        full_text_available = bool(diagnostics.get("fullTextAvailable"))
        if not sufficient and full_text_available and paper_ids:
            self._log("本地全文已存在但部分深层细节仍未完全覆盖，将基于现有证据生成有边界的回答")
            trace.append(
                {
                    "step": "evidence_fallback",
                    "agent": "orchestrator",
                    "status": "best_effort",
                    "reason": "full_text_available",
                    "missingFacetIds": evaluation.get("missingFacetIds", []),
                }
            )
        elif not sufficient:
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

        self._log(
            "证据充分，正在交给 ResearchChatAgent 生成最终回答"
            if sufficient
            else "将基于已验证证据生成有边界的回答"
        )
        evidence_indices = {
            f"{str(item.get('record_id') or '')}:{int(item.get('chunk_index') or 0)}": index
            for index, item in enumerate(evidence, start=1)
        }
        requirement_specs = {
            str(item.get("id") or ""): item
            for item in plan.get("requirementSpecs") or []
            if isinstance(item, dict)
        }
        requirement_claims = [
            {
                **item,
                "description": str(requirement_specs.get(str(item.get("id") or ""), {}).get("description") or ""),
                "citationIndices": [
                    evidence_indices[ref]
                    for ref in item.get("supportingRefs") or []
                    if ref in evidence_indices
                ],
            }
            for item in evaluation.get("requirementAssessments") or []
            if isinstance(item, dict)
        ]
        retrieval_state = {
            "fullTextAvailable": bool(diagnostics.get("fullTextAvailable")),
            "evidenceSufficient": sufficient,
            "evidenceCount": len(evidence),
            "candidateCount": int(diagnostics.get("candidateCount") or 0),
            "missingFacetIds": list(evaluation.get("missingFacetIds") or []),
            "missingRequirementIds": list(evaluation.get("missingRequirementIds") or []),
            "sectionMetadataDegraded": bool(diagnostics.get("sectionMetadataDegraded")),
            "requirementClaims": requirement_claims,
            "requiredCitationGroups": [
                item["citationIndices"]
                for item in requirement_claims
                if item.get("status") == "supported" and item.get("citationIndices")
            ],
        }
        try:
            result, recovery_trace = await self.recovery.execute(
                "ResearchChatAgent 回答生成",
                lambda: asyncio.to_thread(
                    research_agent.run,
                    task,
                    history=history,
                    paper_ids=paper_ids,
                    retrieval_query=retrieval_query,
                    evidence=evidence,
                    answer_requirements=list(plan.get("coreRequirements") or plan.get("answerRequirements") or []),
                    retrieval_state=retrieval_state,
                ),
                cancel_event=cancel_event,
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

    def _assess_evidence(
        self,
        diagnostics: dict[str, Any],
        *,
        required_paper_ids: list[str] | None = None,
        required_chunk_refs: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, list[str]]:
        """保留原有二元接口，内部统一使用 EvidenceEvaluator。"""
        evaluation = self._evaluate_evidence(
            diagnostics,
            required_paper_ids=required_paper_ids,
            required_chunk_refs=required_chunk_refs,
        )
        return bool(evaluation["sufficient"]), list(evaluation["reasons"])

    def _evaluate_evidence(
        self,
        diagnostics: dict[str, Any],
        *,
        plan: dict[str, Any] | None = None,
        required_paper_ids: list[str] | None = None,
        required_chunk_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """返回包含 facet 缺口的完整证据评估结果。"""
        return self.evidence_evaluator.evaluate(
            diagnostics,
            plan=plan,
            required_paper_ids=required_paper_ids,
            required_chunk_refs=required_chunk_refs,
        )

    async def _evaluate_retrieved_evidence(
        self,
        diagnostics: dict[str, Any],
        *,
        evidence: list[dict[str, Any]],
        plan: dict[str, Any],
        required_paper_ids: list[str] | None = None,
        required_chunk_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """先做确定性检查，再按回答契约执行逐项语义证据验证。"""
        evaluation = self._evaluate_evidence(
            diagnostics,
            plan=plan,
            required_paper_ids=required_paper_ids,
            required_chunk_refs=required_chunk_refs,
        )
        if not requires_semantic_validation(plan) or not evidence:
            return evaluation
        model = ModelConfigStore().build_model_payload()
        if not model:
            return {**evaluation, "semanticValidated": False, "semanticValidationError": "模型未配置"}
        try:
            semantic, raw_response = await asyncio.to_thread(
                self.evidence_evaluator.evaluate_semantic,
                evidence,
                plan,
                completion=chat_completion,
                model=model,
                timeout=settings.research_agent_request_timeout,
            )
            self.run_logger.log(
                "EvidenceEvaluator",
                "语义证据覆盖验证完成",
                event="semantic_evidence_assessment",
                data={
                    "rawResponsePreview": raw_response[: self.ROUTER_RAW_LOG_LIMIT],
                    "responseLength": len(raw_response),
                    "truncated": len(raw_response) > self.ROUTER_RAW_LOG_LIMIT,
                    "facetAssessments": semantic.get("facetAssessments", []),
                    "requirementAssessments": semantic.get("requirementAssessments", []),
                    "answerable": semantic.get("answerable", False),
                },
            )
        except Exception as error:
            raw_response = str(getattr(error, "raw_response", "") or "")
            self.run_logger.log(
                "EvidenceEvaluator",
                "语义证据覆盖验证失败，保留确定性评估结果",
                event="semantic_evidence_assessment_error",
                data={
                    "errorType": type(error).__name__,
                    "errorMessage": str(error),
                    "rawResponsePreview": raw_response[: self.ROUTER_RAW_LOG_LIMIT],
                    "responseLength": len(raw_response),
                    "truncated": len(raw_response) > self.ROUTER_RAW_LOG_LIMIT,
                },
            )
            fallback_refinements = []
            candidates = [
                *list(plan.get("retrievalFacets") or []),
                *[
                    {
                        "id": f"requirement-{item.get('id')}",
                        "goal": item.get("description"),
                        "query": item.get("description"),
                        "preferredSectionTypes": item.get("preferredSectionTypes") or [],
                    }
                    for item in plan.get("requirementSpecs") or []
                    if isinstance(item, dict)
                ],
            ]
            for item in candidates:
                if not isinstance(item, dict) or not str(item.get("query") or "").strip():
                    continue
                fallback_refinements.append(dict(item))
            return {
                **evaluation,
                "sufficient": False,
                "semanticValidated": False,
                "semanticValidationError": str(error),
                "refinementFacets": fallback_refinements,
            }

        reasons = list(evaluation.get("reasons") or [])
        if not semantic.get("answerable"):
            partial_count = sum(
                item.get("status") == "partial"
                for item in [*semantic.get("facetAssessments", []), *semantic.get("requirementAssessments", [])]
            )
            unsupported_count = sum(
                item.get("status") == "unsupported"
                for item in [*semantic.get("facetAssessments", []), *semantic.get("requirementAssessments", [])]
            )
            reasons.append(f"语义验证仍有 {partial_count} 项部分支持、{unsupported_count} 项缺少直接证据")
        else:
            # 关键词覆盖率用于触发补偿检索，但不应否决逐 facet 的直接证据验证。
            # 证据数量、指定论文/片段缺失等确定性错误仍然保留为阻塞原因。
            reasons = [reason for reason in reasons if not str(reason).startswith("问题关键词覆盖率仅")]
        return {
            **evaluation,
            **semantic,
            "sufficient": not reasons and bool(semantic.get("answerable")),
            "reasons": reasons,
        }

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
