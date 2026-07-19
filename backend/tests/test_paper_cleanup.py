"""验证无 PDF 文献清理的预览、确认范围和项目关联一致性。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.hunter_agent import HunterAgent
from app.services.project_repository import ProjectRepository


class PaperCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "papers.sqlite3"
        self.agent = HunterAgent(download_dir=self.root / "pdfs", metadata_db_path=self.database)
        valid_pdf = self.root / "pdfs" / "valid.pdf"
        valid_pdf.parent.mkdir(parents=True, exist_ok=True)
        valid_pdf.write_bytes(b"%PDF-1.4\n")
        self.agent.repository.save({"id": "missing", "source": "test", "title": "缺少 PDF"})
        self.agent.repository.save({
            "id": "valid",
            "source": "test",
            "title": "已有 PDF",
            "pdfPath": str(valid_pdf),
        })
        self.projects = ProjectRepository(self.database)
        self.project = self.projects.create(name="清理测试", paper_ids=["missing", "valid"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preview_does_not_delete_and_cleanup_is_limited_to_confirmed_ids(self) -> None:
        preview = self.agent.preview_records_without_local_pdf()

        self.assertEqual(preview["candidateCount"], 1)
        self.assertEqual([record["id"] for record in preview["candidateRecords"]], ["missing"])
        self.assertIsNotNone(self.agent.get_saved_paper("missing"))

        cleanup = self.agent.cleanup_records_without_local_pdf(["missing", "valid"])
        removed_ids = [record["id"] for record in cleanup["removedRecords"]]
        removed_references = self.projects.remove_paper_references(removed_ids)

        self.assertEqual(removed_ids, ["missing"])
        self.assertEqual(removed_references, 2)
        self.assertIsNone(self.agent.get_saved_paper("missing"))
        self.assertIsNotNone(self.agent.get_saved_paper("valid"))
        self.assertEqual(self.projects.list_paper_ids(self.project["id"]), ["valid"])

    def test_empty_confirmation_scope_never_deletes_all_records(self) -> None:
        result = self.agent.cleanup_records_without_local_pdf([])

        self.assertEqual(result["removedCount"], 0)
        self.assertIsNotNone(self.agent.get_saved_paper("missing"))


if __name__ == "__main__":
    unittest.main()
