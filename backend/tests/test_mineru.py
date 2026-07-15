"""验证 MinerU 云 API 优先与显式本地 CLI 回退策略。"""

from __future__ import annotations

from io import BytesIO
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import zipfile

import requests

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.mineru import _run_mineru_cloud_api, mineru_processing


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
