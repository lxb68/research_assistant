"""语义断点续跑的恢复决策与快照复用测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.agents.domainTree_agent import (
    DomainTreeAgent,
    KnowledgeGraphQualityError,
    SourceDocument,
)
from app.services.background_job_handlers import (
    _domain_tree,
    _semantic_auto_resume_decision,
)
from app.services.domain_tree_store import DomainTreeStore


class _FakeJobContext:
    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.progress_updates: list[dict] = []

    def progress(self, value: int, **payload) -> None:
        self.progress_updates.append({"value": value, **payload})

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RuntimeError("任务已取消")

    def record_model_call(self, metric: dict) -> None:
        del metric

    def model_usage_summary(self) -> dict:
        return {"callCount": 0}


class SemanticResumeDecisionTest(unittest.TestCase):
    def test_transient_failures_enable_automatic_resume(self) -> None:
        allowed, message = _semantic_auto_resume_decision(
            {
                "failureReasons": {
                    "timeout": 4,
                    "failure_rate_exceeded": 20,
                }
            }
        )

        self.assertTrue(allowed)
        self.assertIn("可恢复", message)

    def test_authentication_failure_pauses_automatic_resume(self) -> None:
        allowed, message = _semantic_auto_resume_decision(
            {"failureReasons": {"authentication": 8}}
        )

        self.assertFalse(allowed)
        self.assertIn("配置错误", message)

    def test_widespread_truncation_requires_manual_resume(self) -> None:
        allowed, message = _semantic_auto_resume_decision(
            {
                "failureReasons": {
                    "output_truncated": 5,
                    "invalid_json": 2,
                    "failure_rate_exceeded": 780,
                }
            }
        )

        self.assertFalse(allowed)
        self.assertIn("输出截断", message)


class SemanticResumeAgentTest(unittest.TestCase):
    def test_resume_reuses_snapshot_without_regenerating_domain_tree(self) -> None:
        document = SourceDocument(
            record_id="paper-1",
            title="Paper",
            abstract="",
            keywords=[],
            markdown_path=None,
            markdown_dir=None,
            toc_entries=[{"title": "Methods"}],
        )
        graph = {
            "nodes": [],
            "edges": [],
            "entities": [],
            "semanticRelations": [],
            "evidence": [],
            "citations": [],
            "extraction": {
                "qualityStatus": "ready",
                "coverageRatio": 1.0,
                "processedChunkCount": 1,
                "failedChunkCount": 0,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory)
            agent.save_domain_tree_snapshot(
                "project-1",
                [{"label": "1 Security"}],
                documents=[document],
                catalog_text="Paper\nMethods",
                action="rebuild",
                language="English",
            )
            agent._set_graph_status("project-1", "failed")

            with patch.object(agent, "_load_documents", return_value=[document]), patch.object(
                agent,
                "_build_knowledge_graph",
                return_value=graph,
            ) as build_graph, patch.object(agent, "batch_save_tags") as save:
                tags = agent.resume_knowledge_graph_sync(
                    "project-1",
                    model={"provider": "custom"},
                )

        self.assertEqual(tags, [{"label": "1 Security"}])
        build_graph.assert_called_once()
        save.assert_called_once()
        self.assertEqual(save.call_args.kwargs["action"], "rebuild")

    def test_degraded_but_accepted_graph_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "domain_tree.json").write_text(
                json.dumps(
                    {
                        "projectId": "project-1",
                        "graphStatus": "degraded",
                        "domainTree": [{"label": "1 Security"}],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "knowledge_graph.json").write_text(
                json.dumps(
                    {
                        "projectId": "project-1",
                        "entities": [{"id": "entity:1", "name": "Security"}],
                    }
                ),
                encoding="utf-8",
            )

            result = DomainTreeStore().load_raw_result(output_dir, "project-1")

        self.assertEqual(result["knowledgeGraph"]["entities"][0]["name"], "Security")


class SemanticResumeHandlerTest(unittest.TestCase):
    def _agent(self, directory: str) -> Mock:
        result_path = Path(directory) / "domain_tree.json"
        result_path.write_text("{}", encoding="utf-8")
        agent = Mock()
        agent.get_result_path.return_value = result_path
        agent.get_result.return_value = {
            "knowledgeGraph": {
                "extraction": {
                    "qualityStatus": "ready",
                    "coverageRatio": 1.0,
                }
            }
        }
        return agent

    def test_transient_quality_failure_automatically_resumes_cached_chunks(self) -> None:
        extraction = {
            "qualityStatus": "failed",
            "processedChunkCount": 4,
            "failedChunkCount": 6,
            "failureReasons": {"timeout": 2, "failure_rate_exceeded": 4},
        }
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            agent.handle_domain_tree_sync.side_effect = KnowledgeGraphQualityError(extraction)
            agent.resume_knowledge_graph_sync.return_value = [{"label": "1 Security"}]
            context = _FakeJobContext()
            with patch(
                "app.services.background_job_handlers.DomainTreeAgent",
                return_value=agent,
            ), patch(
                "app.services.background_job_handlers.ModelConfigStore.build_model_payload",
                return_value={"provider": "custom"},
            ), patch(
                "app.services.background_job_handlers.settings.semantic_graph_auto_resume_delay_seconds",
                0,
            ):
                result = _domain_tree(
                    context,
                    {"project_id": "project-1", "action": "rebuild"},
                )

        agent.resume_knowledge_graph_sync.assert_called_once()
        self.assertEqual(result["resume"]["mode"], "automatic")
        self.assertTrue(
            any(
                update.get("stage") == "semantic_auto_resume"
                for update in context.progress_updates
            )
        )

    def test_truncation_failure_pauses_for_manual_resume(self) -> None:
        extraction = {
            "qualityStatus": "failed",
            "processedChunkCount": 3,
            "failedChunkCount": 97,
            "failureReasons": {
                "output_truncated": 5,
                "failure_rate_exceeded": 92,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            agent.handle_domain_tree_sync.side_effect = KnowledgeGraphQualityError(extraction)
            context = _FakeJobContext()
            with patch(
                "app.services.background_job_handlers.DomainTreeAgent",
                return_value=agent,
            ), patch(
                "app.services.background_job_handlers.ModelConfigStore.build_model_payload",
                return_value={"provider": "custom"},
            ):
                with self.assertRaises(KnowledgeGraphQualityError):
                    _domain_tree(
                        context,
                        {"project_id": "project-1", "action": "rebuild"},
                    )

        agent.resume_knowledge_graph_sync.assert_not_called()
        self.assertEqual(context.progress_updates[-1]["stage"], "semantic_resume_paused")
        self.assertTrue(
            context.progress_updates[-1]["details"]["manualResumeAvailable"]
        )


if __name__ == "__main__":
    unittest.main()
