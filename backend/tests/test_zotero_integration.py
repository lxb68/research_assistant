"""Zotero 本地连接、数据源状态和增量同步测试。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents import HunterAgent
from app.services.project_repository import ProjectRepository
from app.services.mineru_batch import MinerUBatchOutcome
from app.services.zotero_connector import ZoteroConnector
from app.services.zotero_source_repository import ZoteroSourceRepository
from app.services.zotero_sync import ZoteroSyncService
from app.api.routes.zotero import ZoteroSourceCreateRequest, create_zotero_source
from app.services.project_repository import DEFAULT_PROJECT_ID


def response(*, payload=None, text="", headers=None, status=200):
    value = Mock()
    value.status_code = status
    value.headers = headers or {}
    value.text = text
    value.json.return_value = payload
    return value


class ZoteroConnectorTest(unittest.TestCase):
    def test_only_loopback_api_is_allowed(self) -> None:
        with self.assertRaisesRegex(ValueError, "回环地址"):
            ZoteroConnector(base_url="http://example.com/api")

    @patch("app.services.zotero_connector.requests.get")
    def test_reads_collections_and_decodes_windows_attachment_path(self, get: Mock) -> None:
        with TemporaryDirectory() as temporary_dir:
            pdf_path = Path(temporary_dir) / "paper.pdf"
            pdf_path.write_bytes(b"pdf")
            get.side_effect = [
                response(payload=[{
                    "key": "ABCD1234",
                    "version": 5,
                    "data": {"key": "ABCD1234", "name": "RAG", "parentCollection": False},
                }]),
                response(text=pdf_path.as_uri()),
            ]
            connector = ZoteroConnector()

            collections = connector.list_collections()
            resolved = connector.resolve_attachment_path("F88GZ7BT")

            self.assertEqual(collections[0]["name"], "RAG")
            self.assertEqual(resolved, pdf_path.resolve())
            self.assertIn("/items/F88GZ7BT/file/view/url", get.call_args_list[1].args[0])


class ZoteroSourceRepositoryTest(unittest.TestCase):
    def test_source_and_item_state_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            repository = ZoteroSourceRepository(Path(temporary_dir) / "metadata.sqlite3")
            source = repository.create(
                project_id="project-one",
                api_base_url="http://127.0.0.1:23119/api",
                library_type="users",
                library_id="0",
                collection_keys=["ABCD1234"],
            )
            repository.upsert_item(
                source["id"], "ITEM1234", item_version=7,
                attachment_key="FILE1234", file_hash="hash", paper_id="paper",
                status="ready",
            )

            stored = repository.require(source["id"])
            item = repository.get_item(source["id"], "ITEM1234")

            self.assertEqual(stored["collectionKeys"], ["ABCD1234"])
            self.assertEqual(item["file_hash"], "hash")
            self.assertEqual(repository.mark_missing_except(source["id"], []), 1)


class ZoteroProjectRoutingTest(unittest.TestCase):
    @patch("app.api.routes.zotero._connector")
    def test_selected_collection_creates_and_reuses_same_named_project(self, connector_factory: Mock) -> None:
        with TemporaryDirectory() as temporary_dir:
            db_path = Path(temporary_dir) / "metadata.sqlite3"
            connector = connector_factory.return_value
            connector.base_url = "http://127.0.0.1:23119/api"
            connector.library_type = "users"
            connector.library_id = "0"
            connector.list_collections.return_value = [
                {"key": "COLL1234", "name": "强化学习", "parentCollection": "", "version": 1},
            ]
            payload = ZoteroSourceCreateRequest(
                collection_keys=["COLL1234"],
                create_collection_projects=True,
            )

            with patch("app.api.routes.zotero.settings.hunter_metadata_db", str(db_path)):
                first = create_zotero_source(DEFAULT_PROJECT_ID, payload)
                second = create_zotero_source(DEFAULT_PROJECT_ID, payload)

            self.assertEqual(first["projects"][0]["name"], "强化学习")
            self.assertNotEqual(first["source"]["projectId"], DEFAULT_PROJECT_ID)
            self.assertEqual(second["source"]["id"], first["source"]["id"])
            self.assertEqual(second["source"]["projectId"], first["source"]["projectId"])


class ZoteroSyncServiceTest(unittest.TestCase):
    @patch("app.services.zotero_sync.ZoteroConnector")
    def test_sync_links_pdf_and_skips_unchanged_content(self, connector_type: Mock) -> None:
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            db_path = root / "metadata.sqlite3"
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"same pdf content")
            projects = ProjectRepository(db_path)
            project = projects.create(name="Zotero 项目")
            sources = ZoteroSourceRepository(db_path)
            source = sources.create(
                project_id=project["id"],
                api_base_url="http://127.0.0.1:23119/api",
                library_type="users",
                library_id="0",
                collection_keys=["COLL1234"],
            )
            parent = {
                "key": "PAPER123",
                "version": 3,
                "data": {
                    "key": "PAPER123", "version": 3, "itemType": "journalArticle",
                    "title": "Efficient RAG", "date": "2024", "DOI": "10.1/example",
                    "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
                    "tags": [{"tag": "RAG"}],
                },
            }
            attachment = {
                "key": "F88GZ7BT",
                "version": 2,
                "data": {
                    "key": "F88GZ7BT", "version": 2, "itemType": "attachment",
                    "contentType": "application/pdf", "filename": "paper.pdf",
                },
            }
            connector = connector_type.return_value
            connector.list_collections.return_value = [{"key": "COLL1234", "name": "RAG", "parentCollection": ""}]
            connector.list_top_items.return_value = [parent]
            connector.list_children.return_value = [attachment]
            connector.resolve_attachment_path.return_value = pdf_path

            service = ZoteroSyncService(metadata_db_path=db_path)
            service.agent.index_linked_pdf_paper = Mock(return_value={"id": "paper"})
            service.agent.get_saved_paper = Mock(side_effect=[None, {"id": "existing"}])
            service.projects.add_papers = Mock()

            first = service.sync(source["id"])
            second = service.sync(source["id"])

            self.assertEqual(first["indexed"], 1)
            self.assertEqual(second["unchanged"], 1)
            self.assertNotEqual(first["projectId"], project["id"])
            self.assertEqual(sources.require(source["id"])["projectId"], first["projectId"])
            service.agent.index_linked_pdf_paper.assert_called_once()
            call = service.agent.index_linked_pdf_paper.call_args.kwargs
            self.assertEqual(call["record_id"], "zotero:users:0:PAPER123")
            self.assertEqual(call["pdf_path"], pdf_path)
            self.assertEqual(call["authors"], ["Ada Lovelace"])

    def test_linked_pdf_is_not_copied_and_uses_stable_mineru_output_name(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            pdf_path = root / "zotero-storage" / "F88GZ7BT" / "paper.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"pdf")
            agent = HunterAgent(
                download_dir=root / "managed-papers",
                metadata_db_path=root / "metadata.sqlite3",
            )
            extracted = {
                "text": "searchable text " * 100,
                "metadata": {},
                "parser": "pymupdf",
                "warning": "",
            }
            with patch.object(agent, "_extract_pdf_text", return_value=extracted) as extract, patch.object(
                agent,
                "index_saved_pdf_text",
                side_effect=lambda record_id, **_: agent.get_saved_paper(record_id),
            ):
                paper = agent.index_linked_pdf_paper(
                    pdf_path=pdf_path,
                    record_id="zotero:users:0:PAPER123",
                    source="zotero",
                    external_id="PAPER123",
                    title="Paper",
                )

            self.assertEqual(Path(str(paper["pdfPath"])), pdf_path.resolve())
            self.assertTrue(paper["linkedExternalFile"])
            self.assertFalse((root / "managed-papers" / "paper.pdf").exists())
            self.assertEqual(extract.call_args.kwargs["output_name"], "zotero:users:0:PAPER123")

    @patch("app.services.zotero_sync.MinerUBatchCoordinator")
    @patch("app.services.zotero_sync.ZoteroConnector")
    def test_cloud_sync_submits_batch_and_indexes_preparsed_markdown(
        self,
        connector_type: Mock,
        coordinator_type: Mock,
    ) -> None:
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            db_path = root / "metadata.sqlite3"
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"pdf")
            markdown_path = root / "full.md"
            markdown_path.write_text("# Parsed\n\n正文 " * 300, encoding="utf-8")
            project = ProjectRepository(db_path).create(name="批量项目")
            source = ZoteroSourceRepository(db_path).create(
                project_id=project["id"],
                api_base_url="http://127.0.0.1:23119/api",
                library_type="users",
                library_id="0",
                collection_keys=[],
            )
            parent = {
                "key": "PAPER123", "version": 1,
                "data": {"key": "PAPER123", "version": 1, "itemType": "journalArticle", "title": "Paper"},
            }
            attachment = {
                "key": "F88GZ7BT", "version": 1,
                "data": {
                    "key": "F88GZ7BT", "version": 1, "itemType": "attachment",
                    "contentType": "application/pdf", "filename": "paper.pdf",
                },
            }
            connector = connector_type.return_value
            connector.list_top_items.return_value = [parent]
            connector.list_children.return_value = [attachment]
            connector.resolve_attachment_path.return_value = pdf_path

            service = ZoteroSyncService(metadata_db_path=db_path)
            service.agent.get_saved_paper = Mock(return_value=None)
            service.agent.index_linked_pdf_paper = Mock(return_value={"id": "paper"})
            service.projects.add_papers = Mock()

            def process(inputs, **_kwargs):
                self.assertEqual(len(inputs), 1)
                return MinerUBatchOutcome(results={
                    inputs[0].data_id: {
                        "success": True,
                        "pdfPath": str(pdf_path),
                        "outputDir": str(root),
                        "markdownPath": str(markdown_path),
                    },
                })

            coordinator_type.return_value.process.side_effect = process
            with patch("app.services.zotero_sync.settings.mineru_api_token", "token"):
                result = service.sync(source["id"])

            self.assertEqual(result["indexed"], 1)
            call = service.agent.index_linked_pdf_paper.call_args.kwargs
            self.assertEqual(call["preparsed_result"]["parser"], "mineru")
            self.assertEqual(call["preparsed_result"]["markdownPath"], str(markdown_path))


if __name__ == "__main__":
    unittest.main()
