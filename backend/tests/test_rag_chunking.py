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
from app.services.rag_retriever import EvidenceChunk, RAGRetriever


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

    def test_resolves_exact_chunk_reference_from_conversation_source(self) -> None:
        """结构化对话来源应能恢复到同一论文片段。"""
        retriever = RAGRetriever(target_chunk_tokens=12, max_chunk_tokens=20, overlap_tokens=0)
        paper = {
            "id": "paper-1",
            "title": "测试论文",
            "splitChunks": [
                {"content": "第一段证据。", "summary": "第一段", "semanticCategory": "body"},
                {"content": "第二段证据。", "summary": "第二段", "semanticCategory": "body"},
            ],
        }
        chunks = retriever._paper_chunks(paper)
        target_index = chunks[-1].chunk_index

        evidence = retriever.resolve_chunk_references(
            [paper],
            [{"record_id": "paper-1", "chunk_index": target_index}],
        )

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["record_id"], "paper-1")
        self.assertEqual(evidence[0]["chunk_index"], target_index)

    def test_single_paper_can_reach_minimum_evidence_despite_configured_cap(self) -> None:
        """单篇论文上限为 1 时，应按最低证据要求动态扩大有效上限。"""
        retriever = RAGRetriever(max_chunks=6, max_chunks_per_paper=1)
        ranked = [
            EvidenceChunk(
                record_id="paper-1",
                title="测试论文",
                text=text,
                score=score,
                chunk_index=index,
            )
            for index, (text, score) in enumerate(
                [
                    ("安全训练协议使用秘密共享计算梯度。", 0.9),
                    ("树节点分裂通过安全比较协议完成。", 0.8),
                    ("预测阶段组合所有弱学习器。", 0.7),
                ]
            )
        ]

        effective_limit = retriever._effective_max_groups_per_paper(
            ranked,
            minimum_evidence_count=2,
        )
        selected = retriever.evidence_assembler.assemble(
            ranked,
            ranked,
            max_groups_per_paper=effective_limit,
        ).evidence

        self.assertEqual(effective_limit, 2)
        self.assertEqual(len(selected), 2)
        self.assertEqual({item.record_id for item in selected}, {"paper-1"})

    def test_chunk_score_adjuster_can_promote_structured_protocol_block(self) -> None:
        """调用方应能基于正文结构重排，避免只依赖可能错挂的章节标题。"""
        retriever = RAGRetriever(max_chunks=1, max_chunks_per_paper=1)
        paper = {
            "id": "paper-1",
            "title": "Secure Training",
            "splitChunks": [
                {
                    "content": "A secure protocol computes a private score.",
                    "headings": [{"heading": "Protocol", "level": 2, "position": 1}],
                },
                {
                    "content": "1: locally prepare inputs\n2: jointly compute scores\n3: open the result",
                    "headings": [{"heading": "Unrelated Optimization", "level": 2, "position": 2}],
                },
            ],
        }

        evidence = retriever.retrieve(
            "secure protocol",
            [paper],
            chunk_score_adjuster=lambda chunk: 2.0 if "1: locally" in chunk.text else 0.0,
        )

        self.assertEqual(len(evidence), 1)
        self.assertIn("1: locally prepare inputs", evidence[0]["text"])

    def test_retrieval_completes_adjacent_parts_of_selected_structure(self) -> None:
        """命中结构中段时，应在上下文预算内带回同一结构的相邻分片。"""
        retriever = RAGRetriever(
            target_chunk_tokens=8,
            max_chunk_tokens=12,
            overlap_tokens=0,
            max_chunks=4,
            max_chunks_per_paper=1,
        )
        paper = {
            "id": "paper-1",
            "title": "Structured Method",
            "splitChunks": [
                {
                    "content": "1: prepare inputs\n2: initialize shared state",
                    "semanticType": "algorithm",
                    "structureId": "structure-algorithm-1",
                    "structurePartIndex": 1,
                    "structurePartCount": 2,
                },
                {
                    "content": "3: compute unique_target_score\n4: return outputs",
                    "semanticType": "algorithm",
                    "structureId": "structure-algorithm-1",
                    "structurePartIndex": 2,
                    "structurePartCount": 2,
                },
            ],
        }

        evidence = retriever.retrieve("unique_target_score", [paper])

        self.assertGreater(len(evidence), 1)
        self.assertEqual(
            [item["structure_sequence"] for item in evidence],
            list(range(len(evidence))),
        )
        self.assertTrue(evidence[0]["is_structure_start"])
        self.assertTrue(evidence[-1]["is_structure_end"])
        self.assertIsNotNone(evidence[0]["continues_to"])
        self.assertIsNotNone(evidence[-1]["continues_from"])

    def test_candidate_search_is_not_truncated_by_final_evidence_limit(self) -> None:
        """候选搜索必须返回宽候选池，最终数量限制只能由组装阶段执行。"""
        retriever = RAGRetriever(
            target_chunk_tokens=4,
            max_chunk_tokens=8,
            overlap_tokens=0,
            max_chunks=1,
            max_chunks_per_paper=3,
        )
        paper = {
            "id": "paper-1",
            "title": "Candidate Separation",
            "splitChunks": [
                {"content": "target mechanism prepares private inputs"},
                {"content": "target mechanism computes intermediate state"},
                {"content": "target mechanism returns final output"},
            ],
        }

        candidates = retriever.search_candidates("target mechanism", [paper])
        evidence = retriever.retrieve("target mechanism", [paper])

        self.assertGreater(len(candidates.ranked), len(evidence))
        self.assertEqual(len(evidence), 1)
        self.assertNotIn("evidenceCount", candidates.diagnostics)
        self.assertEqual(retriever.last_diagnostics["rankedCandidateCount"], len(candidates.ranked))

    def test_diversity_is_applied_between_structures_not_inside_structure(self) -> None:
        """高度相似的连续分片属于同一逻辑证据时，不能被多样性规则互相去重。"""
        retriever = RAGRetriever(
            target_chunk_tokens=8,
            max_chunk_tokens=12,
            overlap_tokens=0,
            max_chunks=8,
            max_chunks_per_paper=1,
        )
        paper = {
            "id": "paper-1",
            "title": "Repeated Protocol",
            "splitChunks": [
                {
                    "content": "protocol repeats shared state update target step one",
                    "semanticType": "algorithm",
                    "structureId": "structure-protocol-1",
                    "structurePartIndex": 1,
                    "structurePartCount": 2,
                },
                {
                    "content": "protocol repeats shared state update target step two",
                    "semanticType": "algorithm",
                    "structureId": "structure-protocol-1",
                    "structurePartIndex": 2,
                    "structurePartCount": 2,
                },
                {"content": "target unrelated background"},
            ],
        }

        evidence = retriever.retrieve("protocol shared state update target", [paper])

        self.assertGreater(len(evidence), 1)
        self.assertEqual(
            [item["structure_sequence"] for item in evidence],
            list(range(len(evidence))),
        )
        self.assertEqual({item["structure_id"] for item in evidence}, {"structure-protocol-1"})
        self.assertEqual(retriever.last_diagnostics["selectedEvidenceGroupCount"], 1)
        self.assertEqual(retriever.last_diagnostics["incompleteStructureCount"], 0)
        self.assertEqual(retriever.last_diagnostics["droppedByDiversity"], 0)

    def test_structure_window_keeps_hit_when_structure_exceeds_context_capacity(self) -> None:
        """结构长于容量时应选择命中点附近的连续窗口，不能退化为只取结构开头。"""
        retriever = RAGRetriever(max_chunks=4, max_context_chars=10000)
        candidates = [
            EvidenceChunk(
                record_id="paper-1",
                title="Long Structure",
                text=f"part {index}",
                score=1.0 if index == 6 else 0.1,
                chunk_index=index,
                structure_id="structure-algorithm-long",
                structure_sequence=index,
                continues_from=(f"structure-algorithm-long:{index - 1}" if index else None),
                continues_to=(f"structure-algorithm-long:{index + 1}" if index < 7 else None),
            )
            for index in range(8)
        ]

        completed = retriever.evidence_assembler.assemble(
            sorted(candidates, key=lambda item: item.score, reverse=True),
            candidates,
            max_groups_per_paper=1,
        ).evidence

        self.assertEqual([item.structure_sequence for item in completed], [4, 5, 6, 7])
        self.assertIn(candidates[6], completed)

    def test_context_marks_continuous_structure_without_exposing_only_plain_chunks(self) -> None:
        """回答上下文应明确片段属于连续结构，普通片段保持原格式。"""
        retriever = RAGRetriever()
        context = retriever.build_context(
            [
                {
                    "title": "Paper",
                    "text": "step content",
                    "semantic_type": "algorithm",
                    "structure_id": "structure-algorithm-1",
                    "structure_sequence": 1,
                    "continues_from": "structure-algorithm-1:0",
                    "continues_to": "structure-algorithm-1:2",
                }
            ]
        )

        self.assertIn("连续结构：algorithm 第 2 段", context)
        self.assertIn("前后均有连续片段", context)


if __name__ == "__main__":
    unittest.main()
