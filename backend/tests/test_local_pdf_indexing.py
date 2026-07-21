"""验证本地 PDF 文本能够进入全文检索和结构化分块链。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.hunter_agent import HunterAgent
from app.core.config import settings


class LocalPdfIndexingTest(unittest.TestCase):
    """覆盖全文落盘、元数据回写和 RAG 分块。"""

    def test_extracted_pdf_text_is_persisted_and_split(self) -> None:
        """PyMuPDF 已提取的全文不能只保存预览，必须形成可检索 Markdown。"""
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            markdown_root = root / "markdown"
            agent = HunterAgent(
                download_dir=root / "papers",
                metadata_db_path=root / "papers.sqlite3",
            )
            saved = agent._save_paper_to_db(
                {
                    "id": "paper-1",
                    "source": "manual_pdf",
                    "title": "Composite Polynomial Comparison",
                    "pdfPath": str(root / "papers" / "paper.pdf"),
                }
            )
            extracted_text = "\n".join(
                [
                    "1 Introduction",
                    "Homomorphic comparison requires a polynomial approximation.",
                    "2 Composite Polynomial Method",
                    *("The polynomial is composed repeatedly to approach the sign function." for _ in range(40)),
                ]
            )

            with patch.object(settings, "mineru_output_dir", str(markdown_root)):
                indexed = agent.index_saved_pdf_text(
                    str(saved["id"]),
                    extracted_text=extracted_text,
                    parser="pymupdf",
                )

            markdown_path = Path(str(indexed["markdownPath"]))
            self.assertTrue(markdown_path.is_file())
            self.assertIn("Composite Polynomial Method", markdown_path.read_text(encoding="utf-8"))
            self.assertGreater(int(indexed["splitChunkCount"]), 0)
            self.assertTrue(indexed["splitChunks"])
            self.assertEqual(indexed["fullTextIndexedBy"], "pymupdf")

    def test_mineru_markdown_is_used_without_rewriting_structure(self) -> None:
        """MinerU 成功时应直接索引原始 Markdown，不能重新包装为纯文本文件。"""
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            markdown_root = root / "markdown"
            mineru_dir = markdown_root / "mineru-result"
            mineru_dir.mkdir(parents=True)
            markdown_path = mineru_dir / "paper.md"
            markdown_path.write_text(
                "# Paper\n\n## Method\n\n" + "method evidence " * 100 + "\n\n## Experiments\n\n" + "result data " * 100,
                encoding="utf-8",
            )
            agent = HunterAgent(
                download_dir=root / "papers",
                metadata_db_path=root / "papers.sqlite3",
            )
            saved = agent._save_paper_to_db(
                {
                    "id": "paper-mineru",
                    "source": "manual_pdf",
                    "title": "Structured Paper",
                    "pdfPath": str(root / "papers" / "paper.pdf"),
                }
            )

            with patch.object(settings, "mineru_output_dir", str(markdown_root)):
                indexed = agent.index_saved_structured_markdown(
                    str(saved["id"]),
                    markdown_path=markdown_path,
                    output_dir=mineru_dir,
                    parser="mineru",
                    conversion_result={"success": True},
                )

            self.assertEqual(Path(str(indexed["markdownPath"])), markdown_path)
            self.assertEqual(indexed["fullTextIndexedBy"], "mineru")
            self.assertEqual(indexed["pdfParsedBy"], "mineru")
            self.assertEqual(indexed["pdfParseWarning"], "")
            self.assertGreaterEqual(int(indexed["splitSectionCount"]), 3)
            self.assertEqual(markdown_path.read_text(encoding="utf-8").count("##"), 2)

    def test_pdf_extraction_prefers_mineru(self) -> None:
        """只要 MinerU 返回可用 Markdown，就不应进入 PyMuPDF 文本降级。"""
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            markdown_path = root / "paper.md"
            markdown_path.write_text("# Parsed\n\n" + "structured content " * 100, encoding="utf-8")
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            agent = HunterAgent(
                download_dir=root / "papers",
                metadata_db_path=root / "papers.sqlite3",
            )

            with patch(
                "app.agents.hunter_agent.mineru_processing",
                return_value={
                    "success": True,
                    "markdownPath": str(markdown_path),
                    "outputDir": str(root),
                },
            ) as mineru:
                extracted = agent._extract_pdf_text(pdf_path)

            self.assertEqual(extracted["parser"], "mineru")
            self.assertEqual(extracted["markdownPath"], str(markdown_path))
            mineru.assert_called_once_with(pdf_path=str(pdf_path), output_name="paper")

    def test_short_nonempty_mineru_markdown_is_not_downgraded(self) -> None:
        """短文档只产生质量提示，不能被误判为 MinerU 调用失败。"""
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            markdown_path = root / "full.md"
            markdown_path.write_text("# Short paper\n\nValid result.", encoding="utf-8")
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            agent = HunterAgent(download_dir=root / "papers", metadata_db_path=root / "papers.sqlite3")
            with patch(
                "app.agents.hunter_agent.mineru_processing",
                return_value={"success": True, "markdownPath": str(markdown_path), "outputDir": str(root)},
            ):
                extracted = agent._extract_pdf_text(pdf_path)

            self.assertEqual(extracted["parser"], "mineru")
            self.assertEqual(extracted["warning"], "")
            self.assertIn("内容较短", extracted["indexWarning"])

    def test_mineru_reindex_clears_stale_fallback_status(self) -> None:
        """后续 MinerU 成功必须原子清除初次导入留下的降级状态。"""
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            markdown_root = root / "markdown"
            output_dir = markdown_root / "paper"
            output_dir.mkdir(parents=True)
            markdown_path = output_dir / "full.md"
            markdown_path.write_text("# Paper\n\n" + "content " * 200, encoding="utf-8")
            agent = HunterAgent(download_dir=root / "papers", metadata_db_path=root / "papers.sqlite3")
            agent._save_paper_to_db(
                {
                    "id": "paper-stale",
                    "source": "manual_pdf",
                    "title": "Paper",
                    "pdfParsedBy": "pymupdf",
                    "pdfParseWarning": "MinerU 解析失败，已降级使用 PyMuPDF：旧错误",
                }
            )
            with patch.object(settings, "mineru_output_dir", str(markdown_root)):
                indexed = agent.index_saved_structured_markdown(
                    "paper-stale",
                    markdown_path=markdown_path,
                    output_dir=output_dir,
                    parser="mineru",
                )

            self.assertEqual(indexed["pdfParsedBy"], "mineru")
            self.assertEqual(indexed["fullTextIndexedBy"], "mineru")
            self.assertEqual(indexed["pdfParseWarning"], "")
            self.assertEqual(indexed["fullTextIndexWarning"], "")

    def test_repair_mineru_status_supports_dry_run_and_apply(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            markdown_root = root / "markdown"
            output_dir = markdown_root / "paper"
            output_dir.mkdir(parents=True)
            markdown_path = output_dir / "full.md"
            markdown_path.write_text("# Paper", encoding="utf-8")
            agent = HunterAgent(download_dir=root / "papers", metadata_db_path=root / "papers.sqlite3")
            agent._save_paper_to_db(
                {
                    "id": "paper-repair",
                    "source": "manual_pdf",
                    "title": "Paper",
                    "markdownPath": str(markdown_path),
                    "fullTextIndexedBy": "mineru",
                    "pdfParsedBy": "pymupdf",
                    "pdfParseWarning": "MinerU 解析失败，已降级使用 PyMuPDF：旧错误",
                }
            )
            with patch.object(settings, "mineru_output_dir", str(markdown_root)):
                preview = agent.repair_mineru_status_metadata()
                unchanged = agent.get_saved_paper("paper-repair")
                repaired = agent.repair_mineru_status_metadata(apply=True)
                updated = agent.get_saved_paper("paper-repair")

            self.assertEqual(preview["candidateCount"], 1)
            self.assertEqual(unchanged["pdfParsedBy"], "pymupdf")
            self.assertEqual(repaired["repairedCount"], 1)
            self.assertEqual(updated["pdfParsedBy"], "mineru")
            self.assertEqual(updated["pdfParseWarning"], "")

    def test_pdf_extraction_falls_back_to_pymupdf_after_mineru_failure(self) -> None:
        """MinerU 失败后才允许使用 PyMuPDF，并保留明确的降级原因。"""
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            pdf_path = root / "paper.pdf"
            import fitz

            document = fitz.open()
            for _ in range(3):
                page = document.new_page()
                page.insert_textbox(
                    fitz.Rect(72, 72, 520, 760),
                    "\n".join("fallback text with sufficient searchable content" for _ in range(24)),
                )
            document.save(pdf_path)
            document.close()
            agent = HunterAgent(
                download_dir=root / "papers",
                metadata_db_path=root / "papers.sqlite3",
            )

            with patch(
                "app.agents.hunter_agent.mineru_processing",
                side_effect=RuntimeError("云端不可用"),
            ):
                extracted = agent._extract_pdf_text(pdf_path)

            self.assertEqual(extracted["parser"], "pymupdf")
            self.assertIn("MinerU 解析失败", extracted["warning"])
            self.assertIn("fallback text", extracted["text"])


if __name__ == "__main__":
    unittest.main()
