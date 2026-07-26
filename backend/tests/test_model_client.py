"""验证不同模型协议的请求转换、响应解析和本地无密钥配置。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# 允许从仓库根目录直接执行 unittest discover。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.model_client import (
    ModelCallError,
    ModelUsage,
    chat_completion,
    chat_completion_result,
    discover_models,
)
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
    def test_openai_detailed_result_preserves_usage(self, post: Mock) -> None:
        """详细接口必须保留上游 usage，纯文本兼容接口不受影响。"""
        result_response = response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 20},
                },
            }
        )
        result_response.headers = {"x-request-id": "request-1"}
        post.return_value = result_response

        result = chat_completion_result(
            {
                "provider": "deepseek",
                "protocol": "openai_compatible",
                "base_url": "https://api.deepseek.com",
                "api_key": "test-key",
                "model": "deepseek-chat",
            },
            self.messages,
        )

        self.assertEqual(result.content, "ok")
        self.assertEqual(result.usage.total_tokens, 150)
        self.assertEqual(result.usage.cached_tokens, 20)
        self.assertEqual(result.request_id, "request-1")
        self.assertEqual(result.finish_reason, "length")

    @patch("app.services.model_client.requests.post")
    def test_empty_openai_answer_preserves_diagnostics_and_is_retryable(self, post: Mock) -> None:
        """偶发空回答必须保留停止原因和用量，并交给配置化重试器处理。"""
        empty = response(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "reasoning_content": "internal reasoning",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 4096,
                    "total_tokens": 5096,
                    "completion_tokens_details": {"reasoning_tokens": 4096},
                },
            }
        )
        empty.headers = {"x-request-id": "empty-request"}
        post.return_value = empty

        with self.assertRaises(ModelCallError) as raised:
            chat_completion_result(
                {
                    "provider": "deepseek",
                    "protocol": "openai_compatible",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                self.messages,
                response_format={"type": "json_object"},
                max_output_tokens=4096,
            )

        error = raised.exception
        self.assertEqual(error.category, "empty_response")
        self.assertTrue(error.retryable)
        self.assertTrue(error.request_accepted)
        self.assertEqual(error.request_id, "empty-request")
        self.assertEqual(error.finish_reason, "stop")
        self.assertEqual(error.usage, ModelUsage(1000, 4096, 5096, None, 4096))
        self.assertIn("reasoning_tokens=4096", str(error))
        self.assertIn("max_output_tokens=4096", str(error))

    @patch("app.services.model_client.requests.post")
    def test_empty_answer_at_token_limit_is_not_retried_blindly(self, post: Mock) -> None:
        """输出预算耗尽属于配置问题，应明确提示而不是用相同预算重复请求。"""
        post.return_value = response(
            {
                "choices": [
                    {
                        "message": {"content": ""},
                        "finish_reason": "length",
                    }
                ],
                "usage": {
                    "completion_tokens": 2048,
                    "completion_tokens_details": {"reasoning_tokens": 2048},
                },
            }
        )

        with self.assertRaises(ModelCallError) as raised:
            chat_completion_result(
                {
                    "provider": "deepseek",
                    "protocol": "openai_compatible",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                self.messages,
                max_output_tokens=2048,
            )

        error = raised.exception
        self.assertEqual(error.category, "output_truncated")
        self.assertFalse(error.retryable)
        self.assertIn("关闭该调用场景的思考模式", str(error))
        self.assertIn("提高输出 Token 配置", str(error))

    @patch("app.services.model_client.requests.post")
    def test_http_error_is_structured(self, post: Mock) -> None:
        rejected = response({"error": {"message": "quota exhausted"}}, status_code=402)
        rejected.headers = {}
        post.return_value = rejected

        with self.assertRaises(ModelCallError) as raised:
            chat_completion_result(
                {
                    "provider": "custom",
                    "protocol": "openai_compatible",
                    "base_url": "https://model.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                self.messages,
            )

        self.assertEqual(raised.exception.category, "quota_exhausted")
        self.assertEqual(raised.exception.http_status, 402)
        self.assertFalse(raised.exception.retryable)

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
            max_output_tokens=8192,
        )

        self.assertEqual(answer, '{"action":"chat"}')
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 8192)

    @patch("app.services.model_client.requests.post")
    def test_deepseek_can_disable_thinking_for_structured_extraction(self, post: Mock) -> None:
        """DeepSeek 场景级开关应映射到官方 thinking 请求字段。"""
        post.return_value = response({"choices": [{"message": {"content": '{"entities":[]}'}}]})

        chat_completion_result(
            {
                "provider": "deepseek",
                "protocol": "openai_compatible",
                "base_url": "https://api.deepseek.com",
                "api_key": "test-key",
                "model": "deepseek-v4-pro",
            },
            self.messages,
            response_format={"type": "json_object"},
            thinking=False,
        )

        self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "disabled"})

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
            max_output_tokens=8192,
        )

        self.assertEqual(answer, '{"action":"chat"}')
        self.assertEqual(post.call_count, 2)
        self.assertIn("response_format", post.call_args_list[0].kwargs["json"])
        self.assertNotIn("response_format", post.call_args_list[1].kwargs["json"])
        self.assertEqual(post.call_args_list[1].kwargs["json"]["max_tokens"], 8192)

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
            max_output_tokens=8192,
        )

        self.assertEqual(post.call_args.kwargs["json"]["format"], "json")
        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], 8192)

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
                "max_output_tokens": 8192,
            },
            self.messages,
        )
        body = post.call_args.kwargs["json"]
        self.assertEqual(answer, "Claude 回答")
        self.assertEqual(body["system"], "请简洁回答。")
        self.assertEqual(body["messages"], [{"role": "user", "content": "你好"}])
        self.assertEqual(body["max_tokens"], 8192)

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
            max_output_tokens=8192,
        )

        generation_config = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(generation_config["responseMimeType"], "application/json")
        self.assertEqual(generation_config["maxOutputTokens"], 8192)

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
            self.assertFalse(runtime["allow_heuristic_fallback"])

    def test_heuristic_fallback_setting_is_persisted_and_public(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ModelConfigStore(directory)
            public = store.save(
                provider="ollama",
                protocol="ollama",
                model="qwen3:8b",
                base_url="http://127.0.0.1:11434",
                api_key="",
                allow_heuristic_fallback=True,
            )

            self.assertTrue(public["allowHeuristicFallback"])
            self.assertTrue(store.load_runtime()["allow_heuristic_fallback"])
            saved = json.loads(store.config_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["allowHeuristicFallback"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI 仅在 Windows 上可用")
    def test_api_key_is_protected_with_windows_dpapi(self) -> None:
        """新保存的云模型密钥不能再以明文出现在配置文件中。"""
        with tempfile.TemporaryDirectory() as directory:
            store = ModelConfigStore(directory)
            store.save(
                provider="openai",
                protocol="openai_compatible",
                model="gpt-test",
                base_url="https://api.openai.com/v1",
                api_key="test-secret-value",
            )

            saved_text = store.config_path.read_text(encoding="utf-8")
            saved = json.loads(saved_text)
            self.assertNotIn("test-secret-value", saved_text)
            self.assertTrue(saved["apiKeyProtected"].startswith("dpapi:v1:"))
            self.assertEqual(store.load_runtime()["api_key"], "test-secret-value")

    def test_environment_api_key_is_not_copied_into_saved_config(self) -> None:
        """环境变量密钥可用于校验，但保存普通字段时不应复制到文件。"""
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.services.model_config.settings.llm_translation_api_key",
            "environment-secret",
        ), patch(
            "app.services.model_config.settings.llm_translation_base_url",
            "https://api.openai.com/v1",
        ), patch(
            "app.services.model_config.settings.llm_translation_model",
            "gpt-env",
        ):
            store = ModelConfigStore(directory)
            public = store.save(
                provider="openai",
                protocol="openai_compatible",
                model="gpt-updated",
                base_url="https://api.openai.com/v1",
                api_key="",
            )

            saved = json.loads(store.config_path.read_text(encoding="utf-8"))
            self.assertTrue(public["configured"])
            self.assertNotIn("apiKey", saved)
            self.assertNotIn("apiKeyProtected", saved)


if __name__ == "__main__":
    unittest.main()
