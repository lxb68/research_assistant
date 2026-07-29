"""文献地图的独立 SQLite 持久化边界。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.services.literature_map.models import (
    EvidenceReference,
    LiteratureRelation,
    MapClaim,
    PaperCard,
    PaperCardDraft,
    PaperExtractionResult,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LiteratureMapRepository:
    """只负责事务和查询，不执行模型抽取或关系推断。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_cards (
                    paper_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    year TEXT NOT NULL DEFAULT '',
                    document_version TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    source_language TEXT NOT NULL DEFAULT '',
                    facets_json TEXT NOT NULL DEFAULT '{}',
                    canonical_facets_json TEXT NOT NULL DEFAULT '{}',
                    metadata_warnings_json TEXT NOT NULL DEFAULT '[]',
                    metadata_provenance_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_claims (
                    id TEXT PRIMARY KEY,
                    paper_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    qualifiers_json TEXT NOT NULL DEFAULT '{}',
                    attribution_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    support_status TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    raw_kind TEXT NOT NULL DEFAULT '',
                    canonical_kind TEXT NOT NULL DEFAULT '',
                    normalizer_version TEXT NOT NULL DEFAULT '',
                    normalization_confidence REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (paper_id) REFERENCES paper_cards(paper_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_paper_claims_paper
                    ON paper_claims(paper_id, support_status);

                CREATE TABLE IF NOT EXISTS literature_relations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL DEFAULT '',
                    source_paper_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    qualifiers_json TEXT NOT NULL DEFAULT '{}',
                    evidence_refs_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    extractor_version TEXT NOT NULL DEFAULT '',
                    raw_relation_type TEXT NOT NULL DEFAULT '',
                    canonical_relation_type TEXT NOT NULL DEFAULT '',
                    normalizer_version TEXT NOT NULL DEFAULT '',
                    normalization_confidence REAL NOT NULL DEFAULT 0,
                    resolution_method TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (source_paper_id) REFERENCES paper_cards(paper_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_literature_relations_source
                    ON literature_relations(project_id, source_paper_id);

                CREATE INDEX IF NOT EXISTS idx_literature_relations_target
                    ON literature_relations(project_id, target_type, target_id);

                CREATE TABLE IF NOT EXISTS literature_map_builds (
                    paper_id TEXT PRIMARY KEY,
                    document_version TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS literature_map_extractions (
                    paper_id TEXT NOT NULL,
                    document_version TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    raw_response TEXT NOT NULL DEFAULT '',
                    draft_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (
                        paper_id, document_version, extractor_version, schema_version
                    )
                );
                """
            )
            self._ensure_column(
                connection,
                "paper_cards",
                "canonical_facets_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                connection,
                "paper_cards",
                "metadata_warnings_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                connection,
                "paper_cards",
                "metadata_provenance_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            for column, definition in (
                ("raw_kind", "TEXT NOT NULL DEFAULT ''"),
                ("canonical_kind", "TEXT NOT NULL DEFAULT ''"),
                ("normalizer_version", "TEXT NOT NULL DEFAULT ''"),
                ("normalization_confidence", "REAL NOT NULL DEFAULT 0"),
            ):
                self._ensure_column(connection, "paper_claims", column, definition)
            for column, definition in (
                ("raw_relation_type", "TEXT NOT NULL DEFAULT ''"),
                ("canonical_relation_type", "TEXT NOT NULL DEFAULT ''"),
                ("normalizer_version", "TEXT NOT NULL DEFAULT ''"),
                ("normalization_confidence", "REAL NOT NULL DEFAULT 0"),
                ("resolution_method", "TEXT NOT NULL DEFAULT ''"),
            ):
                self._ensure_column(
                    connection,
                    "literature_relations",
                    column,
                    definition,
                )
            connection.execute(
                """
                UPDATE paper_claims
                SET raw_kind = CASE WHEN raw_kind = '' THEN kind ELSE raw_kind END,
                    canonical_kind = CASE
                        WHEN canonical_kind = '' THEN kind ELSE canonical_kind END
                """
            )
            connection.execute(
                """
                UPDATE literature_relations
                SET raw_relation_type = CASE
                        WHEN raw_relation_type = '' THEN relation_type
                        ELSE raw_relation_type END,
                    canonical_relation_type = CASE
                        WHEN canonical_relation_type = '' THEN relation_type
                        ELSE canonical_relation_type END
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def needs_rebuild(
        self,
        paper_id: str,
        *,
        document_version: str,
        extractor_version: str,
        schema_version: int,
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT document_version, extractor_version, schema_version, status
                FROM paper_cards WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()
        return not row or any(
            (
                row["document_version"] != document_version,
                row["extractor_version"] != extractor_version,
                int(row["schema_version"]) != int(schema_version),
                row["status"] != "ready",
            )
        )

    def mark_build_started(
        self,
        paper_id: str,
        *,
        document_version: str,
        extractor_version: str,
    ) -> None:
        started_at = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO literature_map_builds (
                    paper_id, document_version, extractor_version, status,
                    diagnostics_json, error_message, started_at, completed_at
                ) VALUES (?, ?, ?, 'running', '{}', '', ?, '')
                ON CONFLICT(paper_id) DO UPDATE SET
                    document_version=excluded.document_version,
                    extractor_version=excluded.extractor_version,
                    status='running',
                    diagnostics_json='{}',
                    error_message='',
                    started_at=excluded.started_at,
                    completed_at=''
                """,
                (paper_id, document_version, extractor_version, started_at),
            )

    def mark_build_finished(
        self,
        paper_id: str,
        *,
        status: str,
        diagnostics: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE literature_map_builds
                SET status = ?, diagnostics_json = ?, error_message = ?, completed_at = ?
                WHERE paper_id = ?
                """,
                (
                    status,
                    _json(diagnostics or {}),
                    str(error_message or "")[:4000],
                    _now(),
                    paper_id,
                ),
            )

    def save_extraction(
        self,
        paper_id: str,
        *,
        document_version: str,
        extractor_version: str,
        schema_version: int,
        result: PaperExtractionResult,
        status: str = "validated",
    ) -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO literature_map_extractions (
                    paper_id, document_version, extractor_version, schema_version,
                    raw_response, draft_json, diagnostics_json, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    paper_id, document_version, extractor_version, schema_version
                ) DO UPDATE SET
                    raw_response=excluded.raw_response,
                    draft_json=excluded.draft_json,
                    diagnostics_json=excluded.diagnostics_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    paper_id,
                    document_version,
                    extractor_version,
                    schema_version,
                    result.raw_response,
                    _json(result.draft.to_dict()),
                    _json(result.diagnostics),
                    status,
                    timestamp,
                    timestamp,
                ),
            )

    def get_extraction(
        self,
        paper_id: str,
        *,
        document_version: str,
        extractor_version: str,
        schema_version: int,
    ) -> PaperExtractionResult | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT raw_response, draft_json, diagnostics_json
                FROM literature_map_extractions
                WHERE paper_id = ? AND document_version = ?
                  AND extractor_version = ? AND schema_version = ?
                """,
                (
                    paper_id,
                    document_version,
                    extractor_version,
                    schema_version,
                ),
            ).fetchone()
        if not row:
            return None
        return PaperExtractionResult(
            draft=PaperCardDraft.from_dict(json.loads(row["draft_json"] or "{}")),
            diagnostics=json.loads(row["diagnostics_json"] or "{}"),
            raw_response=str(row["raw_response"] or ""),
        )

    def mark_extraction_committed(
        self,
        paper_id: str,
        *,
        document_version: str,
        extractor_version: str,
        schema_version: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE literature_map_extractions
                SET status = 'committed', updated_at = ?
                WHERE paper_id = ? AND document_version = ?
                  AND extractor_version = ? AND schema_version = ?
                """,
                (
                    _now(),
                    paper_id,
                    document_version,
                    extractor_version,
                    schema_version,
                ),
            )

    def get_build_statuses(self, paper_ids: list[str]) -> dict[str, dict[str, Any]]:
        normalized = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in paper_ids
                if str(value or "").strip()
            )
        )
        if not normalized:
            return {}
        placeholders = ", ".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT paper_id, status, error_message, diagnostics_json
                FROM literature_map_builds
                WHERE paper_id IN ({placeholders})
                """,
                normalized,
            ).fetchall()
        return {
            str(row["paper_id"]): {
                "status": str(row["status"]),
                "error": str(row["error_message"] or ""),
                "diagnostics": json.loads(row["diagnostics_json"] or "{}"),
            }
            for row in rows
        }

    def save_card(
        self,
        card: PaperCard,
        *,
        relations: list[LiteratureRelation] | None = None,
        relation_scope_project_id: str = "",
    ) -> None:
        """原子替换单篇卡片、声明和本轮产生的关系，避免残留旧版本数据。"""

        timestamp = _now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM paper_cards WHERE paper_id = ?",
                (card.paper_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else timestamp
            connection.execute(
                """
                INSERT INTO paper_cards (
                    paper_id, title, year, document_version, extractor_version,
                    schema_version, summary, source_language, facets_json,
                    canonical_facets_json, metadata_warnings_json,
                    metadata_provenance_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    title=excluded.title,
                    year=excluded.year,
                    document_version=excluded.document_version,
                    extractor_version=excluded.extractor_version,
                    schema_version=excluded.schema_version,
                    summary=excluded.summary,
                    source_language=excluded.source_language,
                    facets_json=excluded.facets_json,
                    canonical_facets_json=excluded.canonical_facets_json,
                    metadata_warnings_json=excluded.metadata_warnings_json,
                    metadata_provenance_json=excluded.metadata_provenance_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    card.paper_id,
                    card.title,
                    card.year,
                    card.document_version,
                    card.extractor_version,
                    card.schema_version,
                    card.summary,
                    card.source_language,
                    _json(card.facets),
                    _json(card.canonical_facets),
                    _json(card.metadata_warnings),
                    _json(card.metadata_provenance),
                    card.status,
                    created_at,
                    timestamp,
                ),
            )
            connection.execute("DELETE FROM paper_claims WHERE paper_id = ?", (card.paper_id,))
            connection.executemany(
                """
                INSERT INTO paper_claims (
                    id, paper_id, kind, subject, predicate, object,
                    qualifiers_json, attribution_type, confidence, support_status,
                    evidence_refs_json, raw_kind, canonical_kind,
                    normalizer_version, normalization_confidence,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        claim.id,
                        card.paper_id,
                        claim.kind,
                        claim.subject,
                        claim.predicate,
                        claim.object,
                        _json(claim.qualifiers),
                        claim.attribution_type,
                        claim.confidence,
                        claim.support_status,
                        _json([item.to_dict() for item in claim.evidence_refs]),
                        claim.raw_kind or claim.kind,
                        claim.canonical_kind or claim.kind,
                        claim.normalizer_version,
                        claim.normalization_confidence,
                        timestamp,
                        timestamp,
                    )
                    for claim in card.claims
                ],
            )
            if relations is not None:
                connection.execute(
                    """
                    DELETE FROM literature_relations
                    WHERE project_id = ? AND source_paper_id = ?
                    """,
                    (relation_scope_project_id, card.paper_id),
                )
                connection.executemany(
                    """
                    INSERT INTO literature_relations (
                        id, project_id, source_paper_id, relation_type, target_id,
                        target_type, qualifiers_json, evidence_refs_json, confidence,
                        status, extractor_version, created_at, updated_at
                        , raw_relation_type, canonical_relation_type,
                        normalizer_version, normalization_confidence,
                        resolution_method
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            relation.id,
                            relation.project_id,
                            relation.source_paper_id,
                            relation.relation_type,
                            relation.target_id,
                            relation.target_type,
                            _json(relation.qualifiers),
                            _json(
                                [item.to_dict() for item in relation.evidence_refs]
                            ),
                            relation.confidence,
                            relation.status,
                            relation.extractor_version,
                            timestamp,
                            timestamp,
                            relation.raw_relation_type or relation.relation_type,
                            relation.canonical_relation_type or relation.relation_type,
                            relation.normalizer_version,
                            relation.normalization_confidence,
                            relation.resolution_method,
                        )
                        for relation in relations
                    ],
                )

    def get_card(self, paper_id: str) -> PaperCard | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_cards WHERE paper_id = ?",
                (paper_id,),
            ).fetchone()
            if not row:
                return None
            claim_rows = connection.execute(
                "SELECT * FROM paper_claims WHERE paper_id = ? ORDER BY id",
                (paper_id,),
            ).fetchall()
        return self._row_to_card(row, claim_rows)

    def list_cards(self, *, limit: int = 100, status: str = "ready") -> list[PaperCard]:
        safe_limit = max(1, min(int(limit), 1000))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_cards
                WHERE (? = '' OR status = ?)
                ORDER BY year, title
                LIMIT ?
                """,
                (status, status, safe_limit),
            ).fetchall()
            result: list[PaperCard] = []
            for row in rows:
                claims = connection.execute(
                    "SELECT * FROM paper_claims WHERE paper_id = ? ORDER BY id",
                    (row["paper_id"],),
                ).fetchall()
                result.append(self._row_to_card(row, claims))
        return result

    def list_cards_by_ids(self, paper_ids: list[str]) -> list[PaperCard]:
        normalized = list(
            dict.fromkeys(str(value or "").strip() for value in paper_ids if str(value or "").strip())
        )
        if not normalized:
            return []
        placeholders = ", ".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM paper_cards WHERE paper_id IN ({placeholders})",
                normalized,
            ).fetchall()
            cards = {
                str(row["paper_id"]): self._row_to_card(
                    row,
                    connection.execute(
                        "SELECT * FROM paper_claims WHERE paper_id = ? ORDER BY id",
                        (row["paper_id"],),
                    ).fetchall(),
                )
                for row in rows
            }
        return [cards[paper_id] for paper_id in normalized if paper_id in cards]

    def list_relations(
        self,
        *,
        project_id: str = "",
        source_paper_id: str = "",
        target_id: str = "",
        status: str = "",
        limit: int = 500,
    ) -> list[LiteratureRelation]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("source_paper_id", source_paper_id),
            ("target_id", target_id),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit), 5000)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM literature_relations{where} ORDER BY source_paper_id, id LIMIT ?",
                values,
            ).fetchall()
        return [self._row_to_relation(row) for row in rows]

    def list_relations_for_sources(
        self,
        source_paper_ids: list[str],
        *,
        project_id: str = "",
        limit: int = 5000,
    ) -> list[LiteratureRelation]:
        normalized = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in source_paper_ids
                if str(value or "").strip()
            )
        )
        if not normalized:
            return []
        placeholders = ", ".join("?" for _ in normalized)
        safe_limit = max(1, min(int(limit), 20000))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM literature_relations
                WHERE project_id = ?
                  AND source_paper_id IN ({placeholders})
                ORDER BY source_paper_id, id
                LIMIT ?
                """,
                [project_id, *normalized, safe_limit],
            ).fetchall()
        return [self._row_to_relation(row) for row in rows]

    def rewrite_relations(
        self,
        source_paper_ids: list[str],
        relations: list[LiteratureRelation],
        *,
        project_id: str = "",
    ) -> None:
        """原子重写指定来源的关系，用于无模型的解析和规范化迁移。"""

        normalized = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in source_paper_ids
                if str(value or "").strip()
            )
        )
        if not normalized:
            return
        placeholders = ", ".join("?" for _ in normalized)
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                f"""
                DELETE FROM literature_relations
                WHERE project_id = ? AND source_paper_id IN ({placeholders})
                """,
                [project_id, *normalized],
            )
            connection.executemany(
                """
                INSERT INTO literature_relations (
                    id, project_id, source_paper_id, relation_type, target_id,
                    target_type, qualifiers_json, evidence_refs_json, confidence,
                    status, extractor_version, created_at, updated_at,
                    raw_relation_type, canonical_relation_type,
                    normalizer_version, normalization_confidence,
                    resolution_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        relation.id,
                        relation.project_id,
                        relation.source_paper_id,
                        relation.relation_type,
                        relation.target_id,
                        relation.target_type,
                        _json(relation.qualifiers),
                        _json([item.to_dict() for item in relation.evidence_refs]),
                        relation.confidence,
                        relation.status,
                        relation.extractor_version,
                        timestamp,
                        timestamp,
                        relation.raw_relation_type or relation.relation_type,
                        relation.canonical_relation_type or relation.relation_type,
                        relation.normalizer_version,
                        relation.normalization_confidence,
                        relation.resolution_method,
                    )
                    for relation in relations
                ],
            )

    @staticmethod
    def _row_to_card(
        row: sqlite3.Row,
        claim_rows: list[sqlite3.Row],
    ) -> PaperCard:
        return PaperCard(
            paper_id=str(row["paper_id"]),
            title=str(row["title"]),
            year=str(row["year"]),
            document_version=str(row["document_version"]),
            extractor_version=str(row["extractor_version"]),
            schema_version=int(row["schema_version"]),
            summary=str(row["summary"]),
            source_language=str(row["source_language"]),
            facets=json.loads(row["facets_json"] or "{}"),
            claims=[
                MapClaim(
                    id=str(claim["id"]),
                    paper_id=str(claim["paper_id"]),
                    kind=str(claim["kind"]),
                    subject=str(claim["subject"]),
                    predicate=str(claim["predicate"]),
                    object=str(claim["object"]),
                    qualifiers=json.loads(claim["qualifiers_json"] or "{}"),
                    attribution_type=str(claim["attribution_type"]),
                    confidence=float(claim["confidence"]),
                    support_status=str(claim["support_status"]),
                    evidence_refs=[
                        EvidenceReference.from_dict(item)
                        for item in json.loads(claim["evidence_refs_json"] or "[]")
                        if isinstance(item, dict)
                    ],
                    raw_kind=str(claim["raw_kind"] or claim["kind"]),
                    canonical_kind=str(
                        claim["canonical_kind"] or claim["kind"]
                    ),
                    normalizer_version=str(claim["normalizer_version"] or ""),
                    normalization_confidence=float(
                        claim["normalization_confidence"] or 0
                    ),
                )
                for claim in claim_rows
            ],
            status=str(row["status"]),
            canonical_facets=json.loads(row["canonical_facets_json"] or "{}"),
            metadata_warnings=json.loads(row["metadata_warnings_json"] or "[]"),
            metadata_provenance=json.loads(
                row["metadata_provenance_json"] or "{}"
            ),
        )

    @staticmethod
    def _row_to_relation(row: sqlite3.Row) -> LiteratureRelation:
        return LiteratureRelation(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            source_paper_id=str(row["source_paper_id"]),
            relation_type=str(row["relation_type"]),
            target_id=str(row["target_id"]),
            target_type=str(row["target_type"]),
            qualifiers=json.loads(row["qualifiers_json"] or "{}"),
            evidence_refs=[
                EvidenceReference.from_dict(item)
                for item in json.loads(row["evidence_refs_json"] or "[]")
                if isinstance(item, dict)
            ],
            confidence=float(row["confidence"]),
            status=str(row["status"]),
            extractor_version=str(row["extractor_version"]),
            raw_relation_type=str(
                row["raw_relation_type"] or row["relation_type"]
            ),
            canonical_relation_type=str(
                row["canonical_relation_type"] or row["relation_type"]
            ),
            normalizer_version=str(row["normalizer_version"] or ""),
            normalization_confidence=float(
                row["normalization_confidence"] or 0
            ),
            resolution_method=str(row["resolution_method"] or ""),
        )


__all__ = ["LiteratureMapRepository"]
