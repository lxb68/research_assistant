"""下载、缓存并匹配 CCF 推荐会议和期刊目录。"""

from __future__ import annotations

from html import unescape
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
import re
import sqlite3

from app.core.config import settings


CCF_CATALOG_URL = "https://ccf.atom.im/"


class CcfCatalog:
    """CCF 会议/期刊目录本地缓存。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or settings.ccf_catalog_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def ensure_loaded(self) -> int:
        """如果本地没有 CCF 数据，则从公开目录下载并写入数据库。"""
        count = self.count()
        if count > 0:
            return count

        entries = self._download_entries()
        self.upsert_entries(entries)
        return self.count()

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM ccf_catalog").fetchone()
        return int(row[0] if row else 0)

    def lookup(self, text: str) -> dict:
        """根据标题/venue 文本匹配 CCF 条目。"""
        normalized_text = self._normalize(text)
        if not normalized_text:
            return {"ccfLevel": "", "ccfSource": "", "ccfMatchedName": ""}

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT short_name, full_name, level, item_type FROM ccf_catalog",
            ).fetchall()

        best_match = None
        best_length = 0
        for short_name, full_name, level, item_type in rows:
            candidates = [short_name, full_name]
            for candidate in candidates:
                normalized_candidate = self._normalize(candidate)
                if not normalized_candidate:
                    continue
                if self._contains_normalized_phrase(
                    normalized_text,
                    normalized_candidate,
                ) and len(normalized_candidate) > best_length:
                    best_match = {
                        "ccfLevel": level,
                        "ccfSource": item_type,
                        "ccfMatchedName": candidate,
                    }
                    best_length = len(normalized_candidate)

        return best_match or {"ccfLevel": "", "ccfSource": "", "ccfMatchedName": ""}

    def upsert_entries(self, entries: list[dict]) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.executemany(
                """
                INSERT INTO ccf_catalog (short_name, full_name, level, item_type, field)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(short_name, full_name, item_type) DO UPDATE SET
                    level = excluded.level,
                    field = excluded.field
                """,
                [
                    (
                        entry["short_name"],
                        entry["full_name"],
                        entry["level"],
                        entry["item_type"],
                        entry["field"],
                    )
                    for entry in entries
                ],
            )
            connection.commit()

    def _download_entries(self) -> list[dict]:
        request = Request(CCF_CATALOG_URL, headers={"User-Agent": "research-assistant/0.1"})
        try:
            with urlopen(request, timeout=settings.request_timeout) as response:
                html = response.read().decode("utf-8", errors="ignore")
        except URLError as error:
            raise RuntimeError(f"无法下载 CCF 目录: {error.reason}") from error

        text = self._html_to_text(html)
        entries: list[dict] = []

        for line in text.splitlines():
            parsed = self._parse_catalog_line(line)
            if parsed:
                entries.append(parsed)

        if not entries:
            entries = self._parse_catalog_cells(text)

        if not entries:
            raise RuntimeError("CCF 目录下载成功，但没有解析到有效条目")

        return entries

    def _parse_catalog_cells(self, text: str) -> list[dict]:
        cells = [line.strip() for line in text.splitlines() if line.strip()]
        entries: list[dict] = []
        index = 0

        while index + 5 < len(cells):
            number_cell = cells[index]
            number_match = re.match(r"^(\d+)(?:\s+(.+))?$", number_cell)
            if not number_match:
                index += 1
                continue

            if number_match.group(2):
                short_name = number_match.group(2).strip()
                full_name = cells[index + 1]
                level = cells[index + 2]
                item_type = cells[index + 3]
                field = cells[index + 4]
                step = 5
            else:
                short_name = cells[index + 1]
                full_name = cells[index + 2]
                level = cells[index + 3]
                item_type = cells[index + 4]
                field = cells[index + 5]
                step = 6

            if level in {"A", "B", "C"} and item_type in {"会议", "期刊"}:
                entries.append(
                    {
                        "short_name": short_name,
                        "full_name": full_name,
                        "level": level,
                        "item_type": item_type,
                        "field": field,
                    }
                )
                index += step
                continue

            index += 1

        return entries

    def _parse_catalog_line(self, line: str) -> dict | None:
        clean_line = re.sub(r"\s+", " ", line).strip()
        if not re.match(r"^\d+\s+", clean_line):
            return None

        marker_match = re.search(r"\s([ABC])\s(会议|期刊)\s", clean_line)
        if not marker_match:
            return None

        level = marker_match.group(1)
        item_type = marker_match.group(2)
        left = clean_line[: marker_match.start()].strip()
        field = clean_line[marker_match.end() :].strip()
        name_part = re.sub(r"^\d+\s+", "", left).strip()

        # ccf.atom.im 的文本行格式是“序号 简称 全称 等级 类型 领域”。
        # 简称一般是第一个 token；全称用于更稳妥匹配。
        short_name, full_name = self._split_names(name_part)
        if not short_name and not full_name:
            return None

        return {
            "short_name": short_name,
            "full_name": full_name,
            "level": level,
            "item_type": item_type,
            "field": field,
        }

    def _split_names(self, value: str) -> tuple[str, str]:
        tokens = value.split()
        if not tokens:
            return "", ""

        short_name = tokens[0]
        full_name = value
        return short_name, full_name

    def _html_to_text(self, html: str) -> str:
        without_tags = re.sub(r"<[^>]+>", "\n", html)
        return unescape(without_tags)

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ccf_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    short_name TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    level TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    field TEXT NOT NULL DEFAULT '',
                    UNIQUE(short_name, full_name, item_type)
                )
                """,
            )
            connection.commit()

    def _normalize(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    def _contains_normalized_phrase(self, text: str, phrase: str) -> bool:
        return f" {phrase} " in f" {text} "
