from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bidpilot.models import RunStatus, TenderQuerySpec


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """Small explicit SQLite repository with transactional delivery semantics."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            raw_query TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            report_path TEXT,
            result_count INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            diagnostics_json TEXT NOT NULL DEFAULT '[]',
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS source_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            kept_count INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tender_items (
            canonical_id TEXT NOT NULL,
            version_hash TEXT NOT NULL,
            project_key TEXT NOT NULL,
            title TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (canonical_id, version_hash)
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            raw_query TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            delivery_channel TEXT NOT NULL DEFAULT 'local',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_run_at TEXT,
            next_run_at TEXT
        );
        CREATE TABLE IF NOT EXISTS delivery_ledger (
            subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            canonical_id TEXT NOT NULL,
            version_hash TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            report_path TEXT NOT NULL,
            PRIMARY KEY (subscription_id, canonical_id, version_hash)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            subscription_id TEXT,
            path TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_items_project ON tender_items(project_key);
        CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);
        """
        with self.connection() as conn:
            conn.executescript(schema)

    def create_run(self, run_id: str, spec: TenderQuerySpec) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO runs(id, raw_query, spec_json, status, started_at) VALUES(?,?,?,?,?)",
                (
                    run_id,
                    spec.raw_query,
                    spec.model_dump_json(),
                    RunStatus.RUNNING.value,
                    utcnow_iso(),
                ),
            )

    def complete_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        report_path: str | None,
        result_count: int,
        new_count: int,
        diagnostics: list[dict[str, Any]],
        error: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs SET status=?, completed_at=?, report_path=?, result_count=?,
                  new_count=?, diagnostics_json=?, error=? WHERE id=?
                """,
                (
                    status.value,
                    utcnow_iso(),
                    report_path,
                    result_count,
                    new_count,
                    json.dumps(diagnostics, ensure_ascii=False),
                    error,
                    run_id,
                ),
            )

    def add_source_run(self, run_id: str, diagnostic: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO source_runs(run_id, source, status, fetched_count, kept_count,
                  latency_ms, message) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    diagnostic["source"],
                    diagnostic["status"],
                    diagnostic.get("fetched_count", 0),
                    diagnostic.get("kept_count", 0),
                    diagnostic.get("latency_ms", 0),
                    diagnostic.get("message", ""),
                ),
            )

    def upsert_records(self, records: list[dict[str, Any]]) -> None:
        now = utcnow_iso()
        with self.connection() as conn:
            for record in records:
                payload = json.dumps(record, ensure_ascii=False, default=str)
                conn.execute(
                    """
                    INSERT INTO tender_items(canonical_id, version_hash, project_key, title,
                      payload_json, first_seen_at, last_seen_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(canonical_id, version_hash) DO UPDATE SET
                      payload_json=excluded.payload_json,
                      last_seen_at=excluded.last_seen_at
                    """,
                    (
                        record["canonical_id"],
                        record["version_hash"],
                        record["project_key"],
                        record["title"],
                        payload,
                        now,
                        now,
                    ),
                )

    def undelivered_keys(
        self, subscription_id: str, keys: list[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        if not keys:
            return set()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT canonical_id, version_hash FROM delivery_ledger WHERE subscription_id=?",
                (subscription_id,),
            ).fetchall()
        delivered = {(row["canonical_id"], row["version_hash"]) for row in rows}
        return set(keys) - delivered

    def mark_delivered(
        self,
        subscription_id: str,
        keys: list[tuple[str, str]],
        report_path: str,
    ) -> None:
        """Record delivery only after report creation/channel success."""
        now = utcnow_iso()
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO delivery_ledger(
                  subscription_id, canonical_id, version_hash, delivered_at, report_path
                ) VALUES(?,?,?,?,?)
                """,
                [(subscription_id, c, v, now, report_path) for c, v in keys],
            )

    def create_subscription(
        self,
        subscription_id: str,
        name: str,
        spec: TenderQuerySpec,
        delivery_channel: str,
        next_run_at: datetime | None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions(
                  id, name, raw_query, spec_json, delivery_channel, created_at, next_run_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    subscription_id,
                    name,
                    spec.raw_query,
                    spec.model_dump_json(),
                    delivery_channel,
                    utcnow_iso(),
                    next_run_at.isoformat() if next_run_at else None,
                ),
            )

    def update_subscription_run(
        self,
        subscription_id: str,
        *,
        last_run_at: datetime,
        next_run_at: datetime | None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_run_at=?, next_run_at=? WHERE id=?",
                (
                    last_run_at.isoformat(),
                    next_run_at.isoformat() if next_run_at else None,
                    subscription_id,
                ),
            )

    def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE id=?", (subscription_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_report(
        self,
        report_id: str,
        run_id: str,
        path: str,
        item_count: int,
        subscription_id: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO reports(id, run_id, subscription_id, path, item_count, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (report_id, run_id, subscription_id, path, item_count, utcnow_iso()),
            )

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_subscriptions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM subscriptions ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None
