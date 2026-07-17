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


if __name__ == "__main__":
    unittest.main()
