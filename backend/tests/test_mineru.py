"""验证 MinerU 云 API 优先与显式本地 CLI 回退策略。"""

from __future__ import annotations

from io import BytesIO
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import zipfile

import requests

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.mineru import (
    _output_directory_lock,
    _replace_path_with_retry,
    _run_mineru_cloud_api,
    _select_primary_markdown,
    mineru_processing,
)


def response(payload: dict | None = None, *, status_code: int = 200, content: bytes = b"") -> Mock:
    """创建简化的 requests.Response 测试替身。"""
    result = Mock(status_code=status_code, content=content, text="")
    if payload is not None:
        result.json.return_value = payload
    return result


def result_zip() -> bytes:
    """创建包含 Markdown 和图片的 MinerU 结果压缩包。"""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("document/full.md", "# Parsed")
        archive.writestr("document/images/figure.png", b"image")
    return buffer.getvalue()


class MinerUCloudTest(unittest.TestCase):
    """覆盖云端签名上传、轮询和结果解压。"""

    @patch("app.services.mineru.requests.put")
    @patch("app.services.mineru.subprocess.run")
    @patch("app.services.mineru.shutil.which")
    @patch("app.services.mineru.requests.get")
    @patch("app.services.mineru.requests.post")
    def test_cloud_api_uploads_polls_and_extracts_zip(
        self,
        post: Mock,
        get: Mock,
        which: Mock,
        run: Mock,
        put: Mock,
    ) -> None:
        post.return_value = response(
            {
                "code": 0,
                "data": {"batch_id": "batch-1", "file_urls": ["https://upload.example/file"]},
            },
        )
        put.return_value = response(status_code=200)
        get.side_effect = [
            response(
                {
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "file_name": "paper.pdf",
                                "state": "done",
                                "full_zip_url": "https://download.example/result.zip",
                            },
                        ],
                    },
                },
            ),
            requests.exceptions.SSLError("TLS EOF"),
        ]
        which.return_value = "curl.exe"

        def download_with_curl(args, **_kwargs):
            output_path = Path(args[args.index("--output") + 1])
            output_path.write_bytes(result_zip())
            return Mock(returncode=0, stderr="", stdout="")

        run.side_effect = download_with_curl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "paper.pdf"
            output_dir = root / "output"
            pdf_path.write_bytes(b"%PDF-test")
            output_dir.mkdir()

            with (
                patch("app.services.mineru.settings.mineru_api_base", "https://mineru.net/api/v4"),
                patch("app.services.mineru.settings.mineru_model_version", "vlm"),
                patch("app.services.mineru.settings.mineru_request_timeout_seconds", 10),
                patch("app.services.mineru.settings.mineru_cloud_timeout_seconds", 60),
                patch("app.services.mineru.settings.mineru_poll_interval_seconds", 0),
            ):
                _run_mineru_cloud_api(pdf_path, output_dir, "secret-token")

            self.assertEqual((output_dir / "document" / "full.md").read_text(), "# Parsed")
            self.assertTrue((output_dir / "document" / "images" / "figure.png").exists())
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret-token")
            self.assertEqual(post.call_args.kwargs["json"]["model_version"], "vlm")
            self.assertNotIn("Content-Type", put.call_args.kwargs)
            run.assert_called_once()

    @patch("app.services.mineru._run_local_mineru_cli")
    @patch("app.services.mineru._run_mineru_cloud_api")
    def test_local_cli_is_not_used_unless_explicitly_enabled(self, cloud: Mock, local: Mock) -> None:
        cloud.side_effect = RuntimeError("cloud unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            stale_output = root / "output" / "paper"
            stale_output.mkdir(parents=True)
            (stale_output / "stale.md").write_text("old result", encoding="utf-8")

            with (
                patch("app.services.mineru.settings.mineru_output_dir", str(root / "output")),
                patch("app.services.mineru.settings.mineru_api_token", "secret-token"),
                patch("app.services.mineru.settings.mineru_enable_local_cli_fallback", False),
            ):
                with self.assertRaisesRegex(RuntimeError, "cloud unavailable"):
                    mineru_processing(pdf_path=str(pdf_path), output_name="paper")

        local.assert_not_called()

    @patch("app.services.mineru._run_local_mineru_cli")
    @patch("app.services.mineru._run_mineru_cloud_api")
    def test_explicit_local_cli_fallback_runs_after_cloud_failure(self, cloud: Mock, local: Mock) -> None:
        cloud.side_effect = RuntimeError("cloud unavailable")

        def write_markdown(_pdf_path: Path, output_dir: Path) -> None:
            (output_dir / "fallback.md").write_text("fallback", encoding="utf-8")

        local.side_effect = write_markdown

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            with (
                patch("app.services.mineru.settings.mineru_output_dir", str(root / "output")),
                patch("app.services.mineru.settings.mineru_api_token", "secret-token"),
                patch("app.services.mineru.settings.mineru_enable_local_cli_fallback", True),
            ):
                result = mineru_processing(pdf_path=str(pdf_path), output_name="paper")

        self.assertTrue(result["success"])
        local.assert_called_once()

    @patch("app.services.mineru._run_mineru_cloud_api")
    def test_processing_replaces_stale_output_after_validation(self, cloud: Mock) -> None:
        """本次结果必须在隔离目录完成，不能混入最终目录的旧 Markdown。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            output_dir = root / "output" / "paper"
            output_dir.mkdir(parents=True)
            (output_dir / "stale.md").write_text("stale", encoding="utf-8")

            def write_result(_pdf_path: Path, processing_dir: Path, _token: str) -> None:
                (processing_dir / "full.md").write_text("# current", encoding="utf-8")

            cloud.side_effect = write_result
            with (
                patch("app.services.mineru.settings.mineru_output_dir", str(root / "output")),
                patch("app.services.mineru.settings.mineru_api_token", "secret-token"),
                patch("app.services.mineru.settings.mineru_enable_local_cli_fallback", False),
            ):
                result = mineru_processing(pdf_path=str(pdf_path), output_name="paper")

            self.assertEqual(Path(result["markdownPath"]).read_text(encoding="utf-8"), "# current")
            self.assertFalse((output_dir / "stale.md").exists())
            self.assertFalse(list((root / "output").glob(".paper.processing-*")))

    @patch("app.services.mineru._run_mineru_cloud_api")
    def test_failed_processing_preserves_previous_output(self, cloud: Mock) -> None:
        """新任务失败时必须保留上一份有效结果。"""
        cloud.side_effect = RuntimeError("cloud failed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            output_dir = root / "output" / "paper"
            output_dir.mkdir(parents=True)
            previous = output_dir / "full.md"
            previous.write_text("# previous", encoding="utf-8")
            with (
                patch("app.services.mineru.settings.mineru_output_dir", str(root / "output")),
                patch("app.services.mineru.settings.mineru_api_token", "secret-token"),
                patch("app.services.mineru.settings.mineru_enable_local_cli_fallback", False),
            ):
                with self.assertRaisesRegex(RuntimeError, "cloud failed"):
                    mineru_processing(pdf_path=str(pdf_path), output_name="paper")

            self.assertEqual(previous.read_text(encoding="utf-8"), "# previous")

    @patch("app.services.mineru.time.sleep")
    def test_directory_replace_retries_transient_windows_file_lock(self, sleep: Mock) -> None:
        """Windows 临时占用不应立即触发 PyMuPDF 降级。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "processing"
            target = root / "published"
            source.mkdir()
            attempts = 0
            real_replace = os.replace

            def flaky_replace(current: Path, destination: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(13, "文件暂时被占用", str(current))
                real_replace(current, destination)

            with patch("app.services.mineru.os.replace", side_effect=flaky_replace):
                _replace_path_with_retry(source, target)

            self.assertTrue(target.is_dir())
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)

    @patch("app.services.mineru.time.sleep")
    @patch("app.services.mineru._run_mineru_cloud_api")
    def test_publish_failure_reuses_only_matching_pdf_output(self, cloud: Mock, _sleep: Mock) -> None:
        """发布持续受阻时，可安全复用字节一致 PDF 的历史 MinerU 结果。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-same-content")
            output_dir = root / "output" / "paper"
            output_dir.mkdir(parents=True)
            (output_dir / "full.md").write_text("# previous", encoding="utf-8")
            (output_dir / pdf_path.name).write_bytes(pdf_path.read_bytes())

            def write_result(_pdf_path: Path, processing_dir: Path, _token: str) -> None:
                (processing_dir / "full.md").write_text("# current", encoding="utf-8")

            cloud.side_effect = write_result
            real_replace = os.replace

            def block_new_publish(source: Path, target: Path) -> None:
                if source.name.startswith(".paper.processing-") and target == output_dir:
                    raise PermissionError(13, "目录被占用", str(source))
                real_replace(source, target)

            with (
                patch("app.services.mineru.settings.mineru_output_dir", str(root / "output")),
                patch("app.services.mineru.settings.mineru_api_token", "secret-token"),
                patch("app.services.mineru.settings.mineru_enable_local_cli_fallback", False),
                patch("app.services.mineru.os.replace", side_effect=block_new_publish),
            ):
                result = mineru_processing(pdf_path=str(pdf_path), output_name="paper")

            self.assertTrue(result["reusedExisting"])
            self.assertIn("Windows 文件占用", result["publishWarning"])
            self.assertEqual(Path(result["markdownPath"]).read_text(encoding="utf-8"), "# previous")

    def test_output_directory_lock_serializes_same_paper(self) -> None:
        """同一文献目录的发布区必须互斥。"""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "paper"
            barrier = threading.Barrier(3)
            state_guard = threading.Lock()
            active = 0
            maximum_active = 0

            def worker() -> None:
                nonlocal active, maximum_active
                barrier.wait()
                with _output_directory_lock(output_dir):
                    with state_guard:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    time.sleep(0.02)
                    with state_guard:
                        active -= 1

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

            self.assertEqual(maximum_active, 1)
            self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_primary_markdown_prefers_full_then_largest_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "short.md").write_text("x", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            full = nested / "full.md"
            full.write_text("full", encoding="utf-8")
            other_full = root / "full.md"
            other_full.write_text("larger full content", encoding="utf-8")

            self.assertEqual(_select_primary_markdown(root), other_full)

    @patch("app.api.routes.mineru.mineru_processing")
    @patch("app.api.routes.mineru.HunterAgent")
    def test_endpoint_uses_record_id_and_keyword_pdf_path(self, agent_type: Mock, processing: Mock) -> None:
        """前端 record_id 必须贯穿到论文定位、MinerU 输出名和结构化索引。"""
        from app.api.routes.mineru import _process_mineru_sync
        from app.services.mineru import MinerURequest

        agent = agent_type.return_value
        agent.get_saved_paper.return_value = {"id": "paper-1"}
        agent.find_local_pdf_for_paper.return_value = Path("C:/papers/paper.pdf")
        processing.return_value = {
            "success": True,
            "markdownPath": "C:/markdown/paper/full.md",
            "outputDir": "C:/markdown/paper",
        }
        agent.index_saved_structured_markdown.return_value = {"id": "paper-1", "splitChunkCount": 4}

        result = _process_mineru_sync(
            MinerURequest(record_id="paper-1", output_name="paper-output", mineru_token="token"),
        )

        agent.get_saved_paper.assert_called_once_with("paper-1")
        processing.assert_called_once_with(
            project_id="paper-1",
            file_name=None,
            pdf_path=str(Path("C:/papers/paper.pdf")),
            output_name="paper-output",
            mineru_token="token",
        )
        agent.index_saved_structured_markdown.assert_called_once()
        self.assertEqual(result["paper"]["splitChunkCount"], 4)


@unittest.skipIf(sys.version_info < (3, 10), "FastAPI app requires the project's Python 3.10+ runtime")
class MinerUEndpointConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """验证 MinerU 接口不会在事件循环线程执行同步工作。"""

    async def test_endpoint_offloads_work_to_worker_thread(self) -> None:
        from app import main

        event_loop_thread = threading.get_ident()

        def fake_processing(_request):
            return {"status": "ok", "workerThread": threading.get_ident()}

        request = main.MinerURequest(pdf_path="paper.pdf")
        with patch.object(main, "_process_mineru_sync", side_effect=fake_processing):
            result = await main.process_mineru(request)

        self.assertNotEqual(result["workerThread"], event_loop_thread)


if __name__ == "__main__":
    unittest.main()
