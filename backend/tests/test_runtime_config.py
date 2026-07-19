"""验证前端可见的运行配置不会泄露任何凭据。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.runtime_config import get_public_runtime_config


class PublicRuntimeConfigTest(unittest.TestCase):
    def test_snapshot_only_reports_secret_status(self) -> None:
        secret = "must-never-reach-the-browser"
        with patch("app.services.runtime_config.settings.ieee_api_key", secret), patch(
            "app.services.runtime_config.settings.mineru_api_token",
            secret,
        ), patch(
            "app.services.runtime_config.settings.tencent_translation_secret_id",
            secret,
        ), patch(
            "app.services.runtime_config.settings.tencent_translation_secret_key",
            secret,
        ):
            snapshot = get_public_runtime_config()
            serialized = json.dumps(snapshot, ensure_ascii=False)

        self.assertNotIn(secret, serialized)
        integrations = {item["id"]: item for item in snapshot["integrations"]}
        self.assertTrue(integrations["ieee"]["configured"])
        self.assertTrue(integrations["mineru"]["configured"])
        self.assertTrue(integrations["tencent_translation"]["configured"])


if __name__ == "__main__":
    unittest.main()
