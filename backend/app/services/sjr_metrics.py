"""加载并匹配本地 SJR 期刊指标数据。"""

from __future__ import annotations

from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
import csv
import io
import re
import sqlite3

from app.core.config import settings


SJR_DOWNLOAD_URL = "https://www.scimagojr.com/journalrank.php?out=xls"


class SjrMetrics:
    """SJR 免费期刊指标本地缓存。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or settings.sjr_catalog_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def lookup(self, venue: str) -> dict:
        normalized = self._normalize(venue)
        if not normalized:
            return {"sjr": None, "impactFactor": None, "metricSource": ""}

        if self.count() == 0:
            try:
                self.refresh()
            except Exception:
                return {"sjr": None, "impactFactor": None, "metricSource": ""}

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute("SELECT title, sjr FROM sjr_journals").fetchall()

        best = None
        best_length = 0
        for title, sjr in rows:
            normalized_title = self._normalize(title)
            if normalized_title and normalized_title in normalized and len(normalized_title) > best_length:
                best = {"sjr": sjr, "impactFactor": sjr, "metricSource": "SJR"}
                best_length = len(normalized_title)

        return best or {"sjr": None, "impactFactor": None, "metricSource": ""}

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM sjr_journals").fetchone()
        return int(row[0] if row else 0)

    def refresh(self) -> int:
        request = Request(SJR_DOWNLOAD_URL, headers={"User-Agent": "research-assistant/0.1"})
        try:
            with urlopen(request, timeout=settings.request_timeout) as response:
                raw = response.read().decode("utf-8", errors="ignore")
        except URLError as error:
            raise RuntimeError(f"无法下载 SJR 数据: {error.reason}") from error

        reader = csv.DictReader(io.StringIO(raw), delimiter=";")
        rows = []
        for item in reader:
            title = (item.get("Title") or item.get("title") or "").strip()
            sjr_value = (item.get("SJR") or item.get("sjr") or "").replace(",", ".").strip()
            if not title or not sjr_value:
                continue
            try:
                sjr = float(sjr_value)
            except ValueError:
                continue
            rows.append((title, sjr))

        if not rows:
            raise RuntimeError("SJR 数据下载成功，但没有解析到有效条目")

        with sqlite3.connect(self.db_path) as connection:
            connection.executemany(
                """
                INSERT INTO sjr_journals (title, sjr)
                VALUES (?, ?)
                ON CONFLICT(title) DO UPDATE SET sjr = excluded.sjr
                """,
                rows,
            )
            connection.commit()

        return self.count()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sjr_journals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL UNIQUE,
                    sjr REAL NOT NULL
                )
                """,
            )
            connection.commit()

    def _normalize(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
