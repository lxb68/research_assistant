"""验证文献级初筛按查询分面保留互补候选，且无信号时不损失授权语料。"""

from __future__ import annotations

import unittest

from app.services.document_candidate_retriever import DocumentCandidateRetriever


class DocumentCandidateRetrieverTest(unittest.TestCase):
    def test_shortlist_reserves_candidates_for_complementary_facets(self) -> None:
        papers = [
            {"id": f"background-{index}", "title": f"Unrelated Background {index}"}
            for index in range(12)
        ]
        papers.extend(
            [
                {
                    "id": "evaluation",
                    "title": "Protected Tree Evaluation",
                    "abstract": "A method for private model evaluation.",
                },
                {
                    "id": "training",
                    "title": "Training Models on Protected Data",
                    "abstract": "The training procedure operates on encrypted records.",
                },
            ]
        )

        result = DocumentCandidateRetriever().shortlist(
            papers,
            query="model development",
            retrieval_facets=[
                {"query": "protected tree evaluation"},
                {"query": "training protected data"},
            ],
            requirement_specs=[
                {"description": "梳理推理路线"},
                {"description": "梳理训练路线"},
            ],
            limit=4,
        )

        self.assertIn("evaluation", {paper["id"] for paper in result.papers})
        self.assertIn("training", {paper["id"] for paper in result.papers})

    def test_no_metadata_overlap_keeps_authorized_corpus(self) -> None:
        papers = [
            {"id": f"paper-{index}", "title": f"Document {index}"}
            for index in range(5)
        ]

        result = DocumentCandidateRetriever().shortlist(
            papers,
            query="完全不重合的中文问题",
            retrieval_facets=[],
            requirement_specs=[],
            limit=2,
        )

        self.assertEqual(len(result.papers), 5)
        self.assertTrue(result.diagnostics["fallbackToAuthorizedCorpus"])


if __name__ == "__main__":
    unittest.main()
