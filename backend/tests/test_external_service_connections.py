"""验证设置页外部服务探测不会泄露凭据或创建解析任务。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api.routes import settings as settings_routes
from app.schemas.api import ExternalServiceConnectionTestRequest
from app.services import mineru
from app.services.tencent_translation import translate_tencent_cloud


class ExternalServiceConnectionTest(unittest.TestCase):
    def test_tencent_translation_probe_validates_real_response(self) -> None:
        response = Mock()
        response.read.return_value = json.dumps({"Response": {"TargetText": "connection test"}}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch("app.services.tencent_translation.urlopen", return_value=response) as open_request:
            translated = translate_tencent_cloud(
                "连接测试",
                secret_id="draft-id",
                secret_key="draft-key",
                region="ap-guangzhou",
            )

        self.assertEqual(translated, "connection test")
        request = open_request.call_args.args[0]
        self.assertTrue(request.get_header("Authorization").startswith("TC3-HMAC-SHA256 Credential=draft-id/"))
        self.assertNotIn("draft-key", repr(request.headers))

    def test_mineru_probe_uses_read_only_query_and_accepts_missing_task(self) -> None:
        response = Mock(status_code=200, text='{"code":-1,"msg":"batch not found"}')
        response.json.return_value = {"code": -1, "msg": "batch not found"}

        with patch("app.services.mineru.requests.get", return_value=response) as get:
            mineru.test_mineru_connection(
                token="draft-token",
                api_base="https://mineru.example/api/v4/",
                timeout=10,
            )

        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith(f"/extract-results/batch/{mineru.MINERU_PROBE_BATCH_ID}"))
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer draft-token"})

    def test_mineru_probe_rejects_authentication_failures(self) -> None:
        cases = [
            (401, {"code": -1, "msg": "unauthorized"}),
            (200, {"code": -1, "msg": "invalid token"}),
        ]
        for status_code, payload in cases:
            with self.subTest(status_code=status_code, payload=payload):
                response = Mock(status_code=status_code, text=json.dumps(payload))
                response.json.return_value = payload
                with patch("app.services.mineru.requests.get", return_value=response):
                    with self.assertRaisesRegex(RuntimeError, "鉴权失败"):
                        mineru.test_mineru_connection(
                            token="invalid-token",
                            api_base="https://mineru.example/api/v4",
                            timeout=10,
                        )

    def test_settings_probe_uses_form_overrides_without_persisting(self) -> None:
        payload = ExternalServiceConnectionTestRequest(
            service="tencent_translation",
            secret_id="form-id",
            secret_key="form-key",
            region="ap-shanghai",
        )
        with patch.object(settings_routes, "translate_tencent_cloud", return_value="connection test") as translate:
            result = settings_routes.test_external_service_connection(payload)

        self.assertTrue(result["available"])
        translate.assert_called_once_with(
            "连接测试",
            secret_id="form-id",
            secret_key="form-key",
            region="ap-shanghai",
            timeout=min(settings_routes.settings.request_timeout, 20),
        )

    def test_settings_probe_reports_missing_tencent_credentials(self) -> None:
        payload = ExternalServiceConnectionTestRequest(service="tencent_translation")
        with (
            patch.object(settings_routes.settings, "tencent_translation_secret_id", ""),
            patch.object(settings_routes.settings, "tencent_translation_secret_key", ""),
            self.assertRaises(HTTPException) as error,
        ):
            settings_routes.test_external_service_connection(payload)

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("SecretId", str(error.exception.detail))


if __name__ == "__main__":
    unittest.main()
