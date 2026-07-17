"""验证语义单元与连续结构切块契约。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.rag_chunking import BaseMarkdownBlock, MarkdownRAGChunker
from app.services.split import split_markdown_document


class SemanticStructureSplitTest(unittest.TestCase):
    """结构识别不能依赖论文、章节号或领域关键词。"""

    def test_long_algorithm_keeps_one_structure_and_explicit_continuity(self) -> None:
        steps = "\n\n".join(
            f"{index}: jointly process the private input for stage {index}."
            for index in range(1, 15)
        )
        markdown = (
            "# Method\n\nBefore the procedure.\n\n"
            '<div class="mineru-algorithm" style="white-space: pre-wrap">\n'
            f"{steps}\n"
            "</div>\n\nAfter the procedure."
        )

        result = split_markdown_document(
            markdown,
            min_split_length=80,
            max_split_length=220,
        )
        algorithm_chunks = [
            chunk for chunk in result["chunks"] if chunk.get("semanticType") == "algorithm"
        ]

        self.assertGreater(len(algorithm_chunks), 1)
        self.assertEqual(len({chunk["structureId"] for chunk in algorithm_chunks}), 1)
        self.assertTrue(algorithm_chunks[0]["isStructureStart"])
        self.assertIsNone(algorithm_chunks[0]["continuesFrom"])
        self.assertIsNotNone(algorithm_chunks[0]["continuesTo"])
        self.assertTrue(algorithm_chunks[-1]["isStructureEnd"])
        self.assertIsNotNone(algorithm_chunks[-1]["continuesFrom"])
        self.assertIsNone(algorithm_chunks[-1]["continuesTo"])
        self.assertIn("Before the procedure", "\n".join(chunk["content"] for chunk in result["chunks"]))
        self.assertIn("After the procedure", "\n".join(chunk["content"] for chunk in result["chunks"]))

    def test_markdown_table_is_an_atomic_semantic_structure(self) -> None:
        markdown = """# Results

Introductory text.

| Metric | Model A | Model B |
|:---|---:|---:|
| Accuracy | 0.91 | 0.93 |
| Recall | 0.88 | 0.90 |

Interpretation text.
"""
        result = split_markdown_document(
            markdown,
            min_split_length=40,
            max_split_length=500,
        )
        tables = [chunk for chunk in result["chunks"] if chunk.get("semanticType") == "table"]

        self.assertEqual(len(tables), 1)
        self.assertTrue(tables[0]["structureId"].startswith("structure-table-"))
        self.assertTrue(tables[0]["isStructureStart"])
        self.assertTrue(tables[0]["isStructureEnd"])

    def test_display_equation_is_separated_from_surrounding_prose(self) -> None:
        markdown = """# Analysis

The objective is defined below.

$$
L(\\theta)=\\sum_i (y_i-f_\\theta(x_i))^2
$$

The next paragraph explains the variables.
"""
        result = split_markdown_document(
            markdown,
            min_split_length=40,
            max_split_length=500,
        )
        equations = [
            chunk for chunk in result["chunks"] if chunk.get("semanticType") == "equation"
        ]

        self.assertEqual(len(equations), 1)
        self.assertIn("L(\\theta)", equations[0]["content"])
        self.assertNotIn("explains the variables", equations[0]["content"])
        self.assertTrue(equations[0]["structureId"].startswith("structure-equation-"))


class SemanticStructureRAGTest(unittest.TestCase):
    """RAG Token 重组应保留结构边界并避免在结构内部复制重叠文本。"""

    def test_rag_chunker_preserves_structure_identity_across_token_splits(self) -> None:
        blocks = [
            BaseMarkdownBlock(
                content="\n".join(f"{index}: compute value {index}" for index in range(1, 7)),
                index=0,
                headings=[{"heading": "Method", "level": 1, "position": 1}],
                semantic_type="algorithm",
                structure_id="structure-algorithm-test",
            )
        ]
        chunker = MarkdownRAGChunker(target_tokens=12, max_tokens=18, overlap_tokens=4)

        chunks = chunker.build(blocks)

        self.assertGreater(len(chunks), 1)
        self.assertEqual({chunk.structure_id for chunk in chunks}, {"structure-algorithm-test"})
        self.assertEqual([chunk.structure_sequence for chunk in chunks], list(range(len(chunks))))
        self.assertTrue(all(chunk.overlap_token_count == 0 for chunk in chunks))
        self.assertTrue(chunks[0].is_structure_start)
        self.assertTrue(chunks[-1].is_structure_end)
        self.assertIsNotNone(chunks[0].continues_to)
        self.assertIsNotNone(chunks[-1].continues_from)

    def test_legacy_blocks_without_structure_metadata_keep_original_behavior(self) -> None:
        chunker = MarkdownRAGChunker(target_tokens=8, max_tokens=14, overlap_tokens=4)
        chunks = chunker.build(
            [
                BaseMarkdownBlock(
                    content="第一段普通文本。\n\n第二段普通文本。\n\n第三段普通文本。",
                    index=0,
                )
            ]
        )

        self.assertTrue(chunks)
        self.assertTrue(all(chunk.semantic_type == "prose" for chunk in chunks))
        self.assertTrue(all(not chunk.structure_id for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
