"""验证百炼、本地 Embedding 与 TF-IDF 的三级降级链。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

# 允许从仓库根目录直接执行 unittest discover。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.embedding_store import EmbeddingClient, SQLiteVectorStore, tfidf_cosine_scores
from app.services.rag_retriever import RAGRetriever


def response(payload: dict) -> Mock:
    """创建最小 requests.Response 测试替身。"""
    result = Mock()
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    return result


class EmbeddingClientTest(unittest.TestCase):
    """覆盖百炼兼容协议和 Ollama 原生协议。"""

    @patch("app.services.embedding_store.requests.post")
    def test_bailian_openai_compatible_embedding(self, post: Mock) -> None:
        """百炼应调用 OpenAI 兼容 `/embeddings` 并按索引恢复顺序。"""
        post.return_value = response(
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }
        )
        client = EmbeddingClient(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model="text-embedding-v4",
            provider="bailian",
            batch_size=10,
        )
        vectors = client.embed(["文本一", "文本二"])
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertTrue(post.call_args.args[0].endswith("/embeddings"))
        self.assertEqual(client.batch_size, 10)

    @patch("app.services.embedding_store.requests.post")
    def test_ollama_local_embedding_without_api_key(self, post: Mock) -> None:
        """Ollama 本地嵌入应使用 `/api/embed` 且不要求 API Key。"""
        post.return_value = response({"embeddings": [[0.2, 0.8]]})
        client = EmbeddingClient(
            base_url="http://127.0.0.1:11434",
            api_key="",
            model="nomic-embed-text",
            provider="local_ollama",
            protocol="ollama",
            requires_api_key=False,
        )
        self.assertTrue(client.configured)
        self.assertEqual(client.embed(["本地文本"]), [[0.2, 0.8]])
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/api/embed")

    def test_tfidf_scores_rank_matching_document_first(self) -> None:
        """TF-IDF 兜底应把包含查询概念的文档排在前面。"""
        scores = tfidf_cosine_scores(
            "同态加密 亲缘检测",
            ["本文研究同态加密条件下的亲缘检测", "本文介绍图像分类和卷积网络"],
        )
        self.assertGreater(scores[0], scores[1])

    def test_vector_store_respects_provider_batch_size(self) -> None:
        """向量缓存应遵守百炼单批最多 10 行的客户端限制。"""
        client = EmbeddingClient(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model="text-embedding-v4",
            provider="bailian",
            batch_size=10,
        )

        def embed(texts: list[str]) -> list[list[float]]:
            """为每个输入生成固定二维测试向量。"""
            return [[1.0, 0.0] for _ in texts]

        client.embed = Mock(side_effect=embed)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteVectorStore(Path(directory) / "vectors.sqlite3")
            vectors = store.get_or_create([f"文本-{index}" for index in range(21)], client=client)
        self.assertEqual(len(vectors), 21)
        self.assertEqual(client.embed.call_count, 3)
        self.assertTrue(all(len(call.args[0]) <= 10 for call in client.embed.call_args_list))


class EmbeddingFallbackIntegrationTest(unittest.TestCase):
    """验证检索器会按百炼、本地、TF-IDF 顺序降级。"""

    def setUp(self) -> None:
        """准备包含两个基础分块的测试论文。"""
        self.paper = {
            "id": "paper-1",
            "title": "测试论文",
            "splitChunks": [
                {
                    "content": "同态加密可用于隐私保护计算。",
                    "headings": [{"heading": "方法", "level": 1, "position": 1}],
                },
                {
                    "content": "图像分类使用卷积神经网络。",
                    "headings": [{"heading": "实验", "level": 1, "position": 10}],
                },
            ],
        }

    def test_cloud_failure_switches_to_local_embedding(self) -> None:
        """百炼请求失败后应继续尝试本地嵌入，而不是直接终止向量检索。"""
        cloud = EmbeddingClient(
            base_url="https://example.com/v1",
            api_key="key",
            model="text-embedding-v4",
            provider="bailian",
        )
        local = EmbeddingClient(
            base_url="http://127.0.0.1:11434",
            api_key="",
            model="local-model",
            provider="local_ollama",
            protocol="ollama",
            requires_api_key=False,
        )
        cloud.embed = Mock(side_effect=requests.Timeout("超时"))
        local.embed = Mock(return_value=[[1.0, 0.0]])
        vector_store = Mock()

        def get_or_create(texts: list[str], *, client: EmbeddingClient) -> list[list[float]]:
            """模拟百炼缓存阶段失败、本地缓存阶段成功。"""
            if client.provider == "bailian":
                raise requests.Timeout("超时")
            return [[1.0, 0.0] for _ in texts]

        vector_store.get_or_create.side_effect = get_or_create
        retriever = RAGRetriever(
            max_chunks=1,
            embedding_clients=[cloud, local],
            vector_store=vector_store,
        )
        evidence = retriever.retrieve("同态加密", [self.paper])
        self.assertTrue(evidence)
        self.assertEqual(retriever.last_retrieval_mode, "hybrid_local_ollama")
        self.assertEqual(retriever.last_diagnostics["embeddingBackend"], "local_ollama")
        self.assertIn("bailian:Timeout", retriever.last_diagnostics["embeddingFailures"])

    def test_no_embedding_backend_uses_tfidf(self) -> None:
        """没有配置任何嵌入服务时应启用 TF-IDF 与 BM25 混合检索。"""
        retriever = RAGRetriever(max_chunks=1, embedding_clients=[])
        evidence = retriever.retrieve("同态加密", [self.paper])
        self.assertTrue(evidence)
        self.assertEqual(retriever.last_retrieval_mode, "hybrid_tfidf")
        self.assertEqual(retriever.last_diagnostics["embeddingBackend"], "tfidf")


if __name__ == "__main__":
    unittest.main()
