"""验证 MinerU 批量并发、幂等复用和批次恢复。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.document_parse_repository import DocumentParseRepository
from app.services.mineru_batch import (
    MinerUBatchCoordinator,
    MinerUBatchError,
    MinerUBatchInput,
)


class FakeBatchClient:
    def __init__(self, *, fail_data_id: str = "") -> None:
        self.create_calls = 0
        self.poll_calls = 0
        self.fail_data_id = fail_data_id
        self.active_uploads = 0
        self.max_active_uploads = 0
        self.lock = threading.Lock()
        self.inputs: list[MinerUBatchInput] = []

    def create_batch(self, inputs: list[MinerUBatchInput]):
        self.create_calls += 1
        self.inputs = list(inputs)
        return "batch-1", [f"upload://{item.data_id}" for item in inputs]

    def upload(self, upload_url: str, _pdf_path: Path) -> None:
        data_id = upload_url.removeprefix("upload://")
        if data_id == self.fail_data_id:
            raise MinerUBatchError("文件格式无效", retryable=False)
        with self.lock:
            self.active_uploads += 1
            self.max_active_uploads = max(self.max_active_uploads, self.active_uploads)
        time.sleep(0.03)
        with self.lock:
            self.active_uploads -= 1

    def get_results(self, _batch_id: str):
        self.poll_calls += 1
        return [
            {
                "data_id": item.data_id,
                "file_name": f"zotero_{item.attachment_key}.pdf",
                "state": "done",
                "full_zip_url": f"result://{item.data_id}",
            }
            for item in self.inputs
            if item.data_id != self.fail_data_id
        ]


def input_for(root: Path, index: int) -> MinerUBatchInput:
    pdf_path = root / f"paper-{index}.pdf"
    pdf_path.write_bytes(f"pdf-{index}".encode())
    return MinerUBatchInput(
        source_id="source-1",
        source_item_key=f"ITEM{index:04d}",
        attachment_key=f"FILE{index:04d}",
        file_hash=f"hash-{index}",
        pdf_path=pdf_path,
        output_name=f"paper-{index}",
        data_id=f"data-{index}",
        parse_key=f"parse-{index}",
    )


class MinerUBatchCoordinatorTest(unittest.TestCase):
    def test_uploads_in_parallel_and_reuses_completed_results(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = [input_for(root, index) for index in range(4)]
            repository = DocumentParseRepository(root / "metadata.sqlite3")
            client = FakeBatchClient()
            materialized = []

            def materializer(*, pdf_path: Path, output_name: str, result_url: str):
                output_dir = root / "markdown" / output_name
                output_dir.mkdir(parents=True, exist_ok=True)
                markdown = output_dir / "full.md"
                markdown.write_text(f"# {result_url}", encoding="utf-8")
                materialized.append(result_url)
                return {
                    "success": True,
                    "pdfPath": str(pdf_path),
                    "outputDir": str(output_dir),
                    "markdownPath": str(markdown),
                }

            coordinator = MinerUBatchCoordinator(
                repository=repository,
                client=client,
                materializer=materializer,
            )
            with patch("app.services.mineru_batch.settings.mineru_upload_concurrency", 2):
                first = coordinator.process(inputs)
                second = coordinator.process(inputs)

            self.assertEqual(len(first.results), 4)
            self.assertFalse(first.errors)
            self.assertEqual(len(second.results), 4)
            self.assertEqual(client.create_calls, 1)
            self.assertEqual(len(materialized), 4)
            self.assertEqual(client.max_active_uploads, 2)

    def test_one_upload_failure_does_not_block_other_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = [input_for(root, index) for index in range(3)]
            client = FakeBatchClient(fail_data_id="data-1")

            def materializer(*, pdf_path: Path, output_name: str, result_url: str):
                output_dir = root / output_name
                output_dir.mkdir()
                markdown = output_dir / "full.md"
                markdown.write_text(result_url, encoding="utf-8")
                return {"pdfPath": str(pdf_path), "outputDir": str(output_dir), "markdownPath": str(markdown)}

            coordinator = MinerUBatchCoordinator(
                repository=DocumentParseRepository(root / "metadata.sqlite3"),
                client=client,
                materializer=materializer,
            )
            outcome = coordinator.process(inputs)

            self.assertEqual(set(outcome.results), {"data-0", "data-2"})
            self.assertEqual(set(outcome.errors), {"data-1"})

    def test_resumes_remote_batch_without_requesting_new_upload_urls(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = input_for(root, 0)
            repository = DocumentParseRepository(root / "metadata.sqlite3")
            repository.create_or_get(
                source_id=item.source_id,
                source_item_key=item.source_item_key,
                attachment_key=item.attachment_key,
                file_hash=item.file_hash,
                parse_key=item.parse_key,
                data_id=item.data_id,
            )
            repository.update(
                item.parse_key,
                status="running",
                provider_batch_id="batch-old",
                provider_data_id="original-data-id",
            )
            client = FakeBatchClient()
            client.inputs = [item]
            client.get_results = lambda _batch_id: [{
                "data_id": "original-data-id",
                "state": "done",
                "full_zip_url": "result://old",
            }]

            def materializer(*, pdf_path: Path, output_name: str, result_url: str):
                output_dir = root / output_name
                output_dir.mkdir()
                markdown = output_dir / "full.md"
                markdown.write_text(result_url, encoding="utf-8")
                return {"pdfPath": str(pdf_path), "outputDir": str(output_dir), "markdownPath": str(markdown)}

            outcome = MinerUBatchCoordinator(
                repository=repository,
                client=client,
                materializer=materializer,
            ).process([item])

            self.assertEqual(client.create_calls, 0)
            self.assertEqual(set(outcome.results), {item.data_id})
            self.assertEqual(repository.get_by_parse_key(item.parse_key)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
