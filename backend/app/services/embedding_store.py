"""调用文本嵌入接口，并在 SQLite 中缓存向量以供语义检索。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable

import requests


class EmbeddingClient:
    """封装兼容 OpenAI 协议的文本嵌入请求。"""
    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: int = 60) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        """判断当前客户端配置是否完整可用。"""
        return bool(self.base_url and self.api_key and self.model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """为一组文本请求并返回稠密向量。"""
        if not texts:
            return []
        response = requests.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
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
        batch_size: int = 32,
    ) -> list[list[float]]:
        """读取缓存向量，并仅为缺失文本生成新向量。"""
        fingerprints = [self.fingerprint(text) for text in texts]
        cached = self._load(fingerprints, client.model)
        missing_indices = [index for index, fingerprint in enumerate(fingerprints) if fingerprint not in cached]
        for start in range(0, len(missing_indices), batch_size):
            indices = missing_indices[start : start + batch_size]
            vectors = client.embed([texts[index] for index in indices])
            self._save([(fingerprints[index], vector) for index, vector in zip(indices, vectors, strict=True)], client.model)
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


__all__ = ["EmbeddingClient", "SQLiteVectorStore", "cosine_similarity"]
