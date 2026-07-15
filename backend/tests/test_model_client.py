"""验证不同模型协议的请求转换、响应解析和本地无密钥配置。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# 允许从仓库根目录直接执行 unittest discover。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.model_client import chat_completion, discover_models
from app.services.model_config import ModelConfigStore


def response(payload: dict, status_code: int = 200) -> Mock:
    """创建行为接近 requests.Response 的测试替身。"""
    result = Mock(status_code=status_code)
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    return result


class ModelClientTest(unittest.TestCase):
    """覆盖统一聊天适配器支持的四类协议。"""

    messages = [
        {"role": "system", "content": "请简洁回答。"},
        {"role": "user", "content": "你好"},
    ]

    @patch("app.services.model_client.requests.post")
    def test_openai_compatible_chat(self, post: Mock) -> None:
        """OpenAI 兼容服务应使用 Chat Completions 请求结构。"""
        post.return_value = response({"choices": [{"message": {"content": "你好"}}]})
        answer = chat_completion(
            {
                "provider": "deepseek",
                "protocol": "openai_compatible",
                "base_url": "https://api.deepseek.com",
                "api_key": "test-key",
                "model": "deepseek-chat",
            },
            self.messages,
        )
        self.assertEqual(answer, "你好")
        self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    @patch("app.services.model_client.requests.post")
    def test_openai_compatible_json_output(self, post: Mock) -> None:
        """OpenAI 兼容协议应把 JSON Output 参数传给上游。"""
        post.return_value = response({"choices": [{"message": {"content": '{"action":"chat"}'}}]})

        answer = chat_completion(
            {
                "provider": "deepseek",
                "protocol": "openai_compatible",
                "base_url": "https://api.deepseek.com",
                "api_key": "test-key",
                "model": "deepseek-chat",
            },
            self.messages,
            response_format={"type": "json_object"},
        )

        self.assertEqual(answer, '{"action":"chat"}')
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 4096)

    @patch("app.services.model_client.requests.post")
    def test_openai_compatible_retries_without_unsupported_response_format(self, post: Mock) -> None:
        """兼容服务明确拒绝 response_format 时应降级一次。"""
        unsupported = response(
            {"error": {"message": "response_format is unsupported"}},
            status_code=400,
        )
        unsupported.raise_for_status.side_effect = RuntimeError("should not be reached")
        post.side_effect = [
            unsupported,
            response({"choices": [{"message": {"content": '{"action":"chat"}'}}]}),
        ]

        answer = chat_completion(
            {
                "provider": "custom",
                "protocol": "openai_compatible",
                "base_url": "http://model.test/v1",
                "api_key": "",
                "model": "test-model",
            },
            self.messages,
            response_format={"type": "json_object"},
        )

        self.assertEqual(answer, '{"action":"chat"}')
        self.assertEqual(post.call_count, 2)
        self.assertIn("response_format", post.call_args_list[0].kwargs["json"])
        self.assertNotIn("response_format", post.call_args_list[1].kwargs["json"])

    @patch("app.services.model_client.requests.post")
    def test_ollama_chat_without_api_key(self, post: Mock) -> None:
        """Ollama 应调用原生接口、关闭流式输出且不要求密钥。"""
        post.return_value = response({"message": {"role": "assistant", "content": "本地回答"}})
        answer = chat_completion(
            {
                "provider": "ollama",
                "protocol": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "api_key": "",
                "model": "qwen3:8b",
            },
            self.messages,
        )
        self.assertEqual(answer, "本地回答")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/api/chat")
        self.assertFalse(post.call_args.kwargs["json"]["stream"])

    @patch("app.services.model_client.requests.post")
    def test_ollama_json_output(self, post: Mock) -> None:
        """Ollama JSON 模式应映射为 format=json。"""
        post.return_value = response({"message": {"role": "assistant", "content": '{"action":"chat"}'}})

        chat_completion(
            {
                "provider": "ollama",
                "protocol": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "api_key": "",
                "model": "qwen3:8b",
            },
            self.messages,
            response_format={"type": "json_object"},
        )

        self.assertEqual(post.call_args.kwargs["json"]["format"], "json")

    @patch("app.services.model_client.requests.post")
    def test_anthropic_message_conversion(self, post: Mock) -> None:
        """Anthropic 请求应把 system 消息移到顶层字段。"""
        post.return_value = response({"content": [{"type": "text", "text": "Claude 回答"}]})
        answer = chat_completion(
            {
                "provider": "anthropic",
                "protocol": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "test-key",
                "model": "claude-test",
            },
            self.messages,
        )
        body = post.call_args.kwargs["json"]
        self.assertEqual(answer, "Claude 回答")
        self.assertEqual(body["system"], "请简洁回答。")
        self.assertEqual(body["messages"], [{"role": "user", "content": "你好"}])

    @patch("app.services.model_client.requests.post")
    def test_gemini_content_conversion(self, post: Mock) -> None:
        """Gemini 请求应转换角色和 parts，并解析候选文本。"""
        post.return_value = response(
            {"candidates": [{"content": {"parts": [{"text": "Gemini 回答"}]}}]},
        )
        answer = chat_completion(
            {
                "provider": "gemini",
                "protocol": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "test-key",
                "model": "gemini-test",
            },
            self.messages,
        )
        self.assertEqual(answer, "Gemini 回答")
        self.assertTrue(post.call_args.args[0].endswith("/models/gemini-test:generateContent"))

    @patch("app.services.model_client.requests.post")
    def test_gemini_json_output(self, post: Mock) -> None:
        """Gemini JSON 模式应设置 application/json MIME。"""
        post.return_value = response(
            {"candidates": [{"content": {"parts": [{"text": '{"action":"chat"}'}]}}]},
        )

        chat_completion(
            {
                "provider": "gemini",
                "protocol": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "test-key",
                "model": "gemini-test",
            },
            self.messages,
            response_format={"type": "json_object"},
        )

        generation_config = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(generation_config["responseMimeType"], "application/json")

    @patch("app.services.model_client.requests.get")
    def test_discover_ollama_models(self, get: Mock) -> None:
        """Ollama 模型发现应读取 /api/tags 返回的模型名称。"""
        get.return_value = response({"models": [{"name": "qwen3:8b"}, {"name": "gemma3"}]})
        models = discover_models(
            {
                "provider": "ollama",
                "protocol": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "api_key": "",
                "model": "",
            },
        )
        self.assertEqual(models, ["gemma3", "qwen3:8b"])


class ModelConfigStoreTest(unittest.TestCase):
    """验证旧配置迁移与本地模型免密钥规则。"""

    def test_ollama_configured_without_api_key(self) -> None:
        """Ollama 只要地址和模型完整就应视为已配置。"""
        with tempfile.TemporaryDirectory() as directory:
            store = ModelConfigStore(directory)
            public = store.save(
                provider="ollama",
                protocol="ollama",
                model="qwen3:8b",
                base_url="http://127.0.0.1:11434",
                api_key="",
            )
            self.assertTrue(public["configured"])
            self.assertFalse(public["requiresApiKey"])
            saved = json.loads((Path(directory) / "settings" / "model_config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["provider"], "ollama")

    def test_legacy_config_infers_provider(self) -> None:
        """不含 provider 的历史配置应按地址识别供应商。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings" / "model_config.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"model": "deepseek-chat", "baseUrl": "https://api.deepseek.com", "apiKey": "test"}),
                encoding="utf-8",
            )
            runtime = ModelConfigStore(directory).load_runtime()
            self.assertEqual(runtime["provider"], "deepseek")
            self.assertEqual(runtime["protocol"], "openai_compatible")


if __name__ == "__main__":
    unittest.main()
