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
    SourceDocument,
)


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


if __name__ == "__main__":
    unittest.main()
