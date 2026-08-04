"""Domain-tree model failures must be explicit unless fallback is enabled."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.domainTree_agent import (
    DomainTreeAgent,
    DomainTreeModelGenerationError,
    KnowledgeGraphQualityError,
    SourceDocument,
)
from app.services.model_client import ModelCallResult, ModelUsage


class DomainTreeFallbackTest(unittest.TestCase):
    def _document(self) -> SourceDocument:
        return SourceDocument(
            record_id="paper-1",
            title="Secure Multi-Party Computation",
            abstract="Privacy preserving distributed computation.",
            keywords=["privacy", "cryptography"],
            markdown_path=None,
            markdown_dir=None,
            toc_entries=[{"title": "Threat Model"}, {"title": "Protocol Design"}],
        )

    def test_model_failure_fails_when_fallback_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory)
            with patch.object(agent, "_call_llm", side_effect=RuntimeError("upstream unavailable")):
                with self.assertRaises(DomainTreeModelGenerationError) as raised:
                    agent._generate_domain_tree(
                        prompt="generate",
                        documents=[self._document()],
                        catalog_text="Secure Multi-Party Computation\nThreat Model\nProtocol Design",
                        language="English",
                        model={"allow_heuristic_fallback": False},
                    )

            self.assertEqual(raised.exception.reason, "model_call_failed")
            self.assertFalse(agent._generation_metadata["degraded"])

    def test_invalid_json_fails_when_fallback_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory)
            with patch.object(agent, "_call_llm", return_value="not-json"):
                with self.assertRaises(DomainTreeModelGenerationError) as raised:
                    agent._generate_domain_tree(
                        prompt="generate",
                        documents=[self._document()],
                        catalog_text="Secure Multi-Party Computation\nThreat Model\nProtocol Design",
                        language="English",
                        model={"allow_heuristic_fallback": False},
                    )

            self.assertEqual(raised.exception.reason, "invalid_model_output")

    def test_model_failure_is_marked_when_fallback_is_enabled(self) -> None:
        progress: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory)
            with patch.object(agent, "_call_llm", side_effect=RuntimeError("upstream unavailable")):
                tags = agent._generate_domain_tree(
                    prompt="generate",
                    documents=[self._document()],
                    catalog_text="Secure Multi-Party Computation\nThreat Model\nProtocol Design",
                    language="English",
                    model={"allow_heuristic_fallback": True},
                    progress_callback=progress.append,
                )

            self.assertTrue(tags)
            self.assertTrue(agent._generation_metadata["degraded"])
            self.assertEqual(agent._generation_metadata["generationMode"], "heuristic")
            self.assertTrue(agent._generation_metadata["warnings"])
            self.assertTrue(progress[-1]["degraded"])

    def test_rejects_failed_knowledge_graph_quality(self) -> None:
        with self.assertRaises(KnowledgeGraphQualityError) as raised:
            DomainTreeAgent._validate_knowledge_graph_quality(
                {
                    "qualityStatus": "failed",
                    "coverageRatio": 0.105,
                    "failedChunkCount": 794,
                }
            )

        self.assertEqual(raised.exception.extraction["failedChunkCount"], 794)

    def test_allows_degraded_knowledge_graph_quality(self) -> None:
        DomainTreeAgent._validate_knowledge_graph_quality(
            {"qualityStatus": "degraded", "coverageRatio": 0.8}
        )

    def test_accepts_json_object_domain_tree_contract_and_legacy_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory)
            wrapped = agent.extract_json_from_llm_output(
                '{"domainTree":[{"label":"1 Cryptography"}]}'
            )
            legacy = agent.extract_json_from_llm_output(
                '[{"label":"1 Cryptography"}]'
            )

        self.assertEqual(wrapped, [{"label": "1 Cryptography"}])
        self.assertEqual(legacy, [{"label": "1 Cryptography"}])

    def test_reports_truncated_domain_tree_output_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory)
            runtime = {
                "provider": "custom",
                "protocol": "openai_compatible",
                "base_url": "https://model.test/v1",
                "model": "test-model",
            }
            result = ModelCallResult(
                content='{"domainTree":[{"label":"1 Cryptography"}',
                usage=ModelUsage(completion_tokens=16384),
                finish_reason="length",
            )
            with patch.object(agent, "_resolve_model_runtime", return_value=runtime), patch(
                "app.agents.domainTree_agent.chat_completion_result",
                return_value=result,
            ) as chat:
                with self.assertRaises(DomainTreeModelGenerationError) as raised:
                    agent._call_llm(
                        "generate",
                        language="English",
                        model=None,
                        max_output_tokens=8192,
                        request_timeout_seconds=60,
                    )

        self.assertEqual(raised.exception.reason, "model_output_truncated")
        self.assertIn("Token 上限", str(raised.exception))
        self.assertEqual(chat.call_args.kwargs["max_output_tokens"], 8192)
        self.assertEqual(chat.call_args.kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
