"""调用文本嵌入接口，并在 SQLite 中缓存向量以供语义检索。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable

import requests


class EmbeddingClient:
    """统一封装百炼、OpenAI 兼容服务和 Ollama 本地嵌入请求。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 60,
        provider: str = "openai_compatible",
        protocol: str = "openai_compatible",
        batch_size: int = 32,
        requires_api_key: bool = True,
    ) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.provider = str(provider or protocol).strip().lower()
        self.protocol = str(protocol or "openai_compatible").strip().lower()
        self.batch_size = max(1, int(batch_size))
        self.requires_api_key = requires_api_key

    @property
    def configured(self) -> bool:
        """判断当前客户端配置是否完整可用。"""
        has_key = bool(self.api_key) or not self.requires_api_key
        return bool(self.base_url and self.model and has_key)

    @property
    def cache_model_key(self) -> str:
        """构造包含后端地址的缓存键，防止同名模型混用向量。"""
        return f"{self.provider}:{self.base_url}:{self.model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        """为一组文本请求并返回稠密向量。"""
        if not texts:
            return []
        if self.protocol == "ollama":
            return self._embed_ollama(texts)
        return self._embed_openai_compatible(texts)

    def _embed_openai_compatible(self, texts: list[str]) -> list[list[float]]:
        """调用百炼或其他 OpenAI 兼容的 `/embeddings` 接口。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = requests.post(
            f"{self.base_url}/embeddings",
            headers=headers,
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError("Embedding API 返回数量与输入不一致")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in ordered]
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise RuntimeError("Embedding API 返回了无效向量")
        return [[float(value) for value in vector] for vector in vectors]

    def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        """调用 Ollama 原生 `/api/embed` 批量嵌入接口。"""
        base = self.base_url[:-4] if self.base_url.lower().endswith("/api") else self.base_url
        response = requests.post(
            f"{base}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        vectors = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError("Ollama Embedding 返回数量与输入不一致")
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise RuntimeError("Ollama Embedding 返回了无效向量")
        return [[float(value) for value in vector] for vector in vectors]


class SQLiteVectorStore:
    """以文本指纹为键缓存稠密向量，避免重复调用 Embedding API。"""

    def __init__(self, path: str | Path) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_vectors (
                    fingerprint TEXT NOT NULL,
                    model TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    PRIMARY KEY (fingerprint, model)
                )
                """
            )
            connection.commit()

    def get_or_create(
        self,
        texts: list[str],
        *,
        client: EmbeddingClient,
        batch_size: int | None = None,
    ) -> list[list[float]]:
        """读取缓存向量，并仅为缺失文本生成新向量。"""
        effective_batch_size = max(1, int(batch_size or client.batch_size))
        model_key = client.cache_model_key
        fingerprints = [self.fingerprint(text) for text in texts]
        cached = self._load(fingerprints, model_key)
        missing_indices = [index for index, fingerprint in enumerate(fingerprints) if fingerprint not in cached]
        for start in range(0, len(missing_indices), effective_batch_size):
            indices = missing_indices[start : start + effective_batch_size]
            vectors = client.embed([texts[index] for index in indices])
            self._save([(fingerprints[index], vector) for index, vector in zip(indices, vectors, strict=True)], model_key)
            cached.update({fingerprints[index]: vector for index, vector in zip(indices, vectors, strict=True)})
        return [cached[fingerprint] for fingerprint in fingerprints]

    def fingerprint(self, text: str) -> str:
        """计算规范化文本的稳定指纹。"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _load(self, fingerprints: list[str], model: str) -> dict[str, list[float]]:
        """从 SQLite 批量读取已有向量。"""
        if not fingerprints:
            return {}
        result: dict[str, list[float]] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(fingerprints), 500):
                batch = fingerprints[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT fingerprint, vector_json FROM rag_vectors WHERE model = ? AND fingerprint IN ({placeholders})",
                    [model, *batch],
                ).fetchall()
                result.update({fingerprint: json.loads(vector_json) for fingerprint, vector_json in rows})
        return result

    def _save(self, entries: Iterable[tuple[str, list[float]]], model: str) -> None:
        """把新生成的向量批量写入 SQLite。"""
        rows = [(fingerprint, model, json.dumps(vector)) for fingerprint, vector in entries]
        if not rows:
            return
        with closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO rag_vectors (fingerprint, model, vector_json) VALUES (?, ?, ?)",
                rows,
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        """创建带行对象支持的 SQLite 连接。"""
        return sqlite3.connect(self.path)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个等长向量的余弦相似度。"""
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def tfidf_cosine_scores(query: str, documents: list[str]) -> list[float]:
    """无需外部模型地计算查询与候选文本的 TF-IDF 余弦相似度。"""
    if not documents:
        return []
    tokenized_documents = [_tfidf_tokenize(document) for document in documents]
    query_tokens = _tfidf_tokenize(query)
    if not query_tokens:
        return [0.0] * len(documents)

    document_frequency: dict[str, int] = {}
    for tokens in tokenized_documents:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    document_count = len(tokenized_documents)

    def vectorize(tokens: list[str]) -> dict[str, float]:
        """把词元列表转换为经过 L2 归一化的稀疏 TF-IDF 向量。"""
        frequencies: dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        total = max(1, len(tokens))
        vector = {
            token: (frequency / total) * (math.log((1 + document_count) / (1 + document_frequency.get(token, 0))) + 1)
            for token, frequency in frequencies.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values()))
        return {token: value / norm for token, value in vector.items()} if norm else {}

    query_vector = vectorize(query_tokens)
    scores: list[float] = []
    for tokens in tokenized_documents:
        document_vector = vectorize(tokens)
        scores.append(sum(value * document_vector.get(token, 0.0) for token, value in query_vector.items()))
    return scores


def _tfidf_tokenize(text: str) -> list[str]:
    """按英文词、中文单字与中文双字词切分 TF-IDF 文本。"""
    lowered = str(text or "").lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9+._-]*", lowered)
    for sequence in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", lowered):
        tokens.extend(sequence)
        if len(sequence) > 1:
            tokens.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


__all__ = ["EmbeddingClient", "SQLiteVectorStore", "cosine_similarity", "tfidf_cosine_scores"]
