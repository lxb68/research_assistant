"""验证 RAG 对 MinerU 基础块的结构化复用与 Token 分块。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 允许从仓库根目录直接执行 unittest discover。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.rag_chunking import BaseMarkdownBlock, MarkdownRAGChunker
from app.services.rag_retriever import RAGRetriever


class MarkdownRAGChunkerTest(unittest.TestCase):
    """覆盖标题层次、语义边界、Token 上限和重叠策略。"""

    def test_reuses_heading_hierarchy_and_keeps_token_limit(self) -> None:
        """同一标题路径的基础块应归组，最终块不得超过 Token 上限。"""
        chunker = MarkdownRAGChunker(target_tokens=12, max_tokens=18, overlap_tokens=4)
        outline = [
            {"title": "方法", "level": 1, "position": 1},
            {"title": "数据处理", "level": 2, "position": 3},
            {"title": "实验", "level": 1, "position": 20},
        ]
        blocks = [
            BaseMarkdownBlock(
                content="第一段描述数据清洗。\n\n第二段描述特征编码。",
                index=0,
                headings=[{"heading": "数据处理", "level": 2, "position": 3}],
                summary="数据处理流程",
            ),
            BaseMarkdownBlock(
                content="第三段描述缺失值处理。\n\n第四段描述格式统一。",
                index=1,
                headings=[{"heading": "数据处理", "level": 2, "position": 3}],
                summary="缺失值与格式",
            ),
        ]
        chunks = chunker.build(blocks, outline=outline)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.section == "方法 > 数据处理" for chunk in chunks))
        self.assertTrue(all(chunk.token_count <= 18 for chunk in chunks))
        self.assertEqual(
            {index for chunk in chunks for index in chunk.base_chunk_indices},
            {0, 1},
        )
        self.assertTrue(all(set(chunk.base_chunk_indices) <= {0, 1} for chunk in chunks))
        self.assertTrue(any(chunk.overlap_token_count > 0 for chunk in chunks[1:]))

    def test_overlap_does_not_cross_heading_branch(self) -> None:
        """不同标题分支之间不得复制上一章节的重叠文本。"""
        chunker = MarkdownRAGChunker(target_tokens=8, max_tokens=14, overlap_tokens=4)
        blocks = [
            BaseMarkdownBlock(
                content="方法章节第一段。\n\n方法章节第二段。",
                index=0,
                headings=[{"heading": "方法", "level": 1, "position": 1}],
            ),
            BaseMarkdownBlock(
                content="实验章节第一段。\n\n实验章节第二段。",
                index=1,
                headings=[{"heading": "实验", "level": 1, "position": 10}],
            ),
        ]
        chunks = chunker.build(
            blocks,
            outline=[
                {"title": "方法", "level": 1, "position": 1},
                {"title": "实验", "level": 1, "position": 10},
            ],
        )
        first_experiment = next(chunk for chunk in chunks if chunk.section == "实验")
        self.assertEqual(first_experiment.overlap_token_count, 0)
        self.assertNotIn("方法章节", first_experiment.text)

    def test_oversized_english_token_is_hard_split(self) -> None:
        """超长 URL 或标识符也不能突破 Token 硬上限。"""
        chunker = MarkdownRAGChunker(target_tokens=8, max_tokens=10, overlap_tokens=0)
        block = BaseMarkdownBlock(content="a" * 160, index=0)
        chunks = chunker.build([block])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.token_count <= 10 for chunk in chunks))


class RAGRetrieverChunkIntegrationTest(unittest.TestCase):
    """验证检索器优先使用基础块并准备向量化文本。"""

    def test_prepares_chunks_from_saved_split_chunks(self) -> None:
        """基础块标题、摘要和索引应保留到最终 RAG 候选中。"""
        retriever = RAGRetriever(
            target_chunk_tokens=12,
            max_chunk_tokens=20,
            overlap_tokens=4,
        )
        paper = {
            "id": "paper-1",
            "title": "测试论文",
            "splitOutline": [
                {"title": "方法", "level": 1, "position": 1},
                {"title": "训练", "level": 2, "position": 2},
            ],
            "splitChunks": [
                {
                    "content": "训练采用批量优化。\n\n学习率根据验证结果调整。",
                    "summary": "训练过程",
                    "semanticCategory": "body",
                    "headings": [{"heading": "训练", "level": 2, "position": 2}],
                }
            ],
        }
        chunks = retriever._paper_chunks(paper)

        self.assertTrue(chunks)
        self.assertEqual(chunks[0].section, "方法 > 训练")
        self.assertEqual(chunks[0].base_chunk_indices, [0])
        self.assertIn("训练过程", retriever._searchable_text(chunks[0]))
        self.assertLessEqual(chunks[0].token_count, 20)


if __name__ == "__main__":
    unittest.main()
