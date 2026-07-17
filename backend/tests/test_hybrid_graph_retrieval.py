"""验证“图谱导航 + 原文回查”混合检索边界。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.hybrid_graph_retriever import HybridGraphRetriever
from app.services.rag_retriever import RAGRetriever


class HybridGraphRetrieverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.graph_root = Path(self.temp_dir.name)
        self.project_id = "test-project"
        project_dir = self.graph_root / self.project_id
        project_dir.mkdir(parents=True)
        (project_dir / "domain_tree.json").write_text(
            json.dumps({"projectId": self.project_id, "graphStatus": "ready"}),
            encoding="utf-8",
        )
        (project_dir / "knowledge_graph.json").write_text(
            json.dumps(
                {
                    "entities": [
                        {"id": "entity:squirrel", "name": "Squirrel", "type": "system"},
                        {"id": "entity:gbdt", "name": "GBDT", "type": "algorithm"},
                    ],
                    "semanticRelations": [
                        {
                            "id": "relation:train",
                            "source": "entity:squirrel",
                            "target": "entity:gbdt",
                            "predicate": "trains",
                            "relationType": "mechanism",
                            "confidence": 0.9,
                            "evidenceIds": ["evidence:train"],
                        }
                    ],
                    "evidence": [
                        {
                            "id": "evidence:train",
                            "documentId": "paper-1",
                            "section": "Secure GBDT Training",
                            "chunkIndex": 17,
                            "lineStart": 42,
                            "quote": "The parties securely aggregate gradients before selecting a split.",
                            "kind": "relation",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.paper = {
            "id": "paper-1",
            "title": "Squirrel",
            "splitChunks": [
                {
                    "content": (
                        "The parties securely aggregate gradients before selecting a split. "
                        "They then update the private sample partition."
                    ),
                    "headings": [{"heading": "Secure GBDT Training", "level": 1}],
                    "summary": "Private tree training",
                }
            ],
        }
        self.rag = RAGRetriever(
            target_chunk_tokens=200,
            max_chunk_tokens=300,
            overlap_tokens=0,
            max_chunks=6,
        )
        self.hybrid = HybridGraphRetriever(
            graph_root=self.graph_root,
            project_id=self.project_id,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_relation_navigation_must_resolve_to_original_text(self) -> None:
        evidence, diagnostics = self.hybrid.retrieve(
            "How does Squirrel train GBDT?",
            papers=[self.paper],
            retriever=self.rag,
            question_type="mechanism",
        )

        self.assertEqual(len(evidence), 1)
        self.assertTrue(evidence[0]["graph_backed"])
        self.assertIn("relation:train", evidence[0]["graph_relation_ids"])
        self.assertIn("original_text", evidence[0]["retrieval_channels"])
        self.assertEqual(diagnostics["verifiedEvidenceCount"], 1)
        self.assertEqual(diagnostics["unresolvedEvidenceCount"], 0)

    def test_unresolvable_graph_quote_is_not_returned(self) -> None:
        paper = {**self.paper, "splitChunks": [{"content": "Unrelated original text."}]}
        evidence, diagnostics = self.hybrid.retrieve(
            "How does Squirrel train GBDT?",
            papers=[paper],
            retriever=self.rag,
            question_type="mechanism",
        )

        self.assertEqual(evidence, [])
        self.assertEqual(diagnostics["verifiedEvidenceCount"], 0)
        self.assertEqual(diagnostics["unresolvedEvidenceCount"], 1)

    def test_non_relational_question_skips_graph(self) -> None:
        evidence, diagnostics = self.hybrid.retrieve(
            "What is Squirrel?",
            papers=[self.paper],
            retriever=self.rag,
            question_type="simple_fact",
        )

        self.assertEqual(evidence, [])
        self.assertFalse(diagnostics["attempted"])
        self.assertEqual(diagnostics["skipReason"], "question_type_not_relational")

    def test_missing_graph_degrades_without_affecting_text_retrieval(self) -> None:
        missing = HybridGraphRetriever(
            graph_root=self.graph_root,
            project_id="missing-project",
        )
        evidence, diagnostics = missing.retrieve(
            "How does Squirrel train GBDT?",
            papers=[self.paper],
            retriever=self.rag,
            question_type="mechanism",
        )

        self.assertEqual(evidence, [])
        self.assertTrue(diagnostics["attempted"])
        self.assertEqual(diagnostics["skipReason"], "graph_not_found")

    def test_fusion_preserves_both_channels_and_graph_provenance(self) -> None:
        text_item = {
            "record_id": "paper-1",
            "chunk_index": 0,
            "text": "original",
            "score": 0.4,
        }
        graph_item = {
            **text_item,
            "score": 1.2,
            "graph_backed": True,
            "graph_relation_ids": ["relation:train"],
            "retrieval_channels": ["graph_navigation", "original_text"],
        }

        fused = self.hybrid.merge_evidence([text_item], [graph_item], limit=3)

        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["score"], 1.2)
        self.assertTrue(fused[0]["graph_backed"])
        self.assertIn("text_rag", fused[0]["retrieval_channels"])
        self.assertIn("graph_navigation", fused[0]["retrieval_channels"])
        self.assertEqual(fused[0]["graph_relation_ids"], ["relation:train"])

    def test_fusion_limit_counts_structure_as_one_logical_evidence(self) -> None:
        """混合融合的数量限制不能拆散同一结构的连续分块。"""
        text_items = [
            {
                "record_id": "paper-1", "chunk_index": 30, "text": "steps 1-5", "score": 1.0,
                "structure_id": "structure-algorithm-1", "structure_sequence": 0,
                "continues_to": "structure-algorithm-1:1",
            },
            {
                "record_id": "paper-1", "chunk_index": 31, "text": "steps 6-10", "score": 0.9,
                "structure_id": "structure-algorithm-1", "structure_sequence": 1,
                "continues_from": "structure-algorithm-1:0",
            },
            {
                "record_id": "paper-1", "chunk_index": 40, "text": "unrelated", "score": 0.8,
            },
        ]

        fused = self.hybrid.merge_evidence(text_items, [], limit=1)

        self.assertEqual([item["chunk_index"] for item in fused], [30, 31])


if __name__ == "__main__":
    unittest.main()
