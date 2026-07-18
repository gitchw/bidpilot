from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bidpilot.models import DeliveryPolicy, RunStatus, TenderQuerySpec


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
            subscription_id TEXT,
            trigger_reason TEXT NOT NULL DEFAULT 'manual',
            raw_query TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            report_path TEXT,
            result_count INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            diagnostics_json TEXT NOT NULL DEFAULT '[]',
            retrieval_json TEXT NOT NULL DEFAULT '{}',
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS source_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            scanned_count INTEGER NOT NULL DEFAULT 0,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            kept_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            rejection_json TEXT NOT NULL DEFAULT '{}',
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
            delivery_policy TEXT NOT NULL DEFAULT 'always',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            last_run_at TEXT,
            next_run_at TEXT,
            last_status TEXT,
            last_message TEXT,
            last_new_count INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_run_id TEXT,
            lease_owner TEXT,
            lease_until TEXT
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
        CREATE TABLE IF NOT EXISTS delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            subscription_id TEXT,
            channel TEXT NOT NULL,
            success INTEGER NOT NULL,
            skipped INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL,
            external_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            pid INTEGER NOT NULL,
            hostname TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS opportunities (
            id TEXT PRIMARY KEY,
            project_key TEXT NOT NULL UNIQUE,
            canonical_id TEXT NOT NULL,
            version_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'new',
            owner TEXT NOT NULL DEFAULT '',
            next_action_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_config (
            field TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_authorizations (
            source_id TEXT PRIMARY KEY,
            encrypted_cookie TEXT NOT NULL,
            cookie_names_json TEXT NOT NULL DEFAULT '[]',
            domains_json TEXT NOT NULL DEFAULT '[]',
            authorized_at TEXT NOT NULL,
            expires_at TEXT,
            last_test_at TEXT,
            last_test_status TEXT NOT NULL DEFAULT 'not_tested',
            last_message TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_items_project ON tender_items(project_key);
        CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_subscriptions_due ON subscriptions(enabled, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_delivery_attempts_run ON delivery_attempts(run_id);
        CREATE INDEX IF NOT EXISTS idx_opportunities_stage ON opportunities(stage, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_opportunities_next_action ON opportunities(next_action_at);
        CREATE INDEX IF NOT EXISTS idx_source_auth_test ON source_authorizations(last_test_at DESC);
        """
        with self.connection() as conn:
            conn.executescript(schema)
            # Additive migrations keep existing demo databases usable across releases.
            self._ensure_column(conn, "runs", "subscription_id", "TEXT")
            self._ensure_column(conn, "runs", "trigger_reason", "TEXT NOT NULL DEFAULT 'manual'")
            self._ensure_column(
                conn, "subscriptions", "delivery_policy", "TEXT NOT NULL DEFAULT 'always'"
            )
            self._ensure_column(conn, "subscriptions", "updated_at", "TEXT")
            self._ensure_column(conn, "subscriptions", "last_status", "TEXT")
            self._ensure_column(conn, "subscriptions", "last_message", "TEXT")
            self._ensure_column(
                conn, "subscriptions", "last_new_count", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                conn, "subscriptions", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "subscriptions", "last_run_id", "TEXT")
            self._ensure_column(conn, "subscriptions", "lease_owner", "TEXT")
            self._ensure_column(conn, "subscriptions", "lease_until", "TEXT")
            self._ensure_column(conn, "source_runs", "scanned_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "source_runs", "rejected_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "source_runs", "rejection_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "runs", "retrieval_json", "TEXT NOT NULL DEFAULT '{}'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_subscription "
                "ON runs(subscription_id, started_at DESC)"
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def create_run(
        self,
        run_id: str,
        spec: TenderQuerySpec,
        *,
        subscription_id: str | None = None,
        trigger_reason: str = "manual",
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs(
                  id, subscription_id, trigger_reason, raw_query, spec_json, status, started_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    subscription_id,
                    trigger_reason,
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
        retrieval: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs SET status=?, completed_at=?, report_path=?, result_count=?,
                  new_count=?, diagnostics_json=?, retrieval_json=?, error=? WHERE id=?
                """,
                (
                    status.value,
                    utcnow_iso(),
                    report_path,
                    result_count,
                    new_count,
                    json.dumps(diagnostics, ensure_ascii=False),
                    json.dumps(retrieval or {}, ensure_ascii=False),
                    error,
                    run_id,
                ),
            )

    def add_source_run(self, run_id: str, diagnostic: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO source_runs(run_id, source, status, scanned_count, fetched_count,
                  kept_count, rejected_count, rejection_json, latency_ms, message)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    diagnostic["source"],
                    diagnostic["status"],
                    diagnostic.get("scanned_count", 0),
                    diagnostic.get("fetched_count", 0),
                    diagnostic.get("kept_count", 0),
                    diagnostic.get("rejected_count", 0),
                    json.dumps(diagnostic.get("rejection_reasons", {}), ensure_ascii=False),
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

    def get_tender_item(self, canonical_id: str, version_hash: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM tender_items WHERE canonical_id=? AND version_hash=?
                """,
                (canonical_id, version_hash),
            ).fetchone()
        return dict(row) if row else None

    def list_project_items(self, project_key: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tender_items
                WHERE project_key=?
                ORDER BY first_seen_at ASC, rowid ASC
                """,
                (project_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_opportunity(
        self,
        *,
        opportunity_id: str,
        project_key: str,
        canonical_id: str,
        version_hash: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        now = utcnow_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO opportunities(
                  id, project_key, canonical_id, version_hash, snapshot_json,
                  created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(project_key) DO UPDATE SET
                  canonical_id=excluded.canonical_id,
                  version_hash=excluded.version_hash,
                  snapshot_json=excluded.snapshot_json,
                  is_read=0,
                  updated_at=excluded.updated_at
                WHERE opportunities.canonical_id != excluded.canonical_id
                   OR opportunities.version_hash != excluded.version_hash
                """,
                (
                    opportunity_id,
                    project_key,
                    canonical_id,
                    version_hash,
                    json.dumps(snapshot, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM opportunities WHERE project_key=?", (project_key,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM opportunities WHERE id=?", (opportunity_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_opportunity_by_project_key(self, project_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM opportunities WHERE project_key=?", (project_key,)
            ).fetchone()
        return dict(row) if row else None

    def refresh_opportunity_snapshot(
        self,
        *,
        project_key: str,
        canonical_id: str,
        version_hash: str,
        snapshot: dict[str, Any],
    ) -> bool:
        """Surface a newly observed lifecycle event without overwriting user follow-up fields."""
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE opportunities
                SET canonical_id=?, version_hash=?, snapshot_json=?, is_read=0, updated_at=?
                WHERE project_key=? AND (canonical_id != ? OR version_hash != ?)
                """,
                (
                    canonical_id,
                    version_hash,
                    json.dumps(snapshot, ensure_ascii=False, default=str),
                    utcnow_iso(),
                    project_key,
                    canonical_id,
                    version_hash,
                ),
            )
        return cursor.rowcount > 0

    def list_opportunities(self, stage: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if stage:
                rows = conn.execute(
                    """
                    SELECT * FROM opportunities WHERE stage=? ORDER BY updated_at DESC
                    """,
                    (stage,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM opportunities ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(row) for row in rows]

    def update_opportunity(self, opportunity_id: str, changes: dict[str, Any]) -> bool:
        allowed = {"stage", "owner", "next_action_at", "notes", "tags_json", "is_read"}
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unsupported opportunity fields: {sorted(invalid)}")
        if not changes:
            return self.get_opportunity(opportunity_id) is not None
        assignments = [f"{column}=?" for column in changes]
        assignments.append("updated_at=?")
        values = [*changes.values(), utcnow_iso(), opportunity_id]
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE opportunities SET {', '.join(assignments)} WHERE id=?", values
            )
        return cursor.rowcount > 0

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
        delivery_policy: DeliveryPolicy = DeliveryPolicy.ALWAYS,
    ) -> None:
        now = utcnow_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions(
                  id, name, raw_query, spec_json, delivery_channel, delivery_policy,
                  created_at, updated_at, next_run_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    subscription_id,
                    name,
                    spec.raw_query,
                    spec.model_dump_json(),
                    delivery_channel,
                    delivery_policy.value,
                    now,
                    now,
                    next_run_at.isoformat() if next_run_at else None,
                ),
            )

    def finish_subscription_attempt(
        self,
        subscription_id: str,
        *,
        last_run_at: datetime,
        next_run_at: datetime | None,
        status: RunStatus,
        message: str,
        new_count: int,
        run_id: str | None,
        success: bool,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE subscriptions SET
                  last_run_at=?, next_run_at=?, last_status=?, last_message=?,
                  last_new_count=?, last_run_id=?, updated_at=?, lease_owner=NULL,
                  lease_until=NULL,
                  consecutive_failures=CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END
                WHERE id=?
                """,
                (
                    last_run_at.isoformat(),
                    next_run_at.isoformat() if next_run_at else None,
                    status.value,
                    message,
                    new_count,
                    run_id,
                    utcnow_iso(),
                    int(success),
                    subscription_id,
                ),
            )

    def set_subscription_due(
        self, subscription_id: str, next_run_at: datetime | None, *, enabled: bool | None = None
    ) -> bool:
        assignments = ["next_run_at=?", "updated_at=?", "lease_owner=NULL", "lease_until=NULL"]
        values: list[Any] = [next_run_at.isoformat() if next_run_at else None, utcnow_iso()]
        if enabled is not None:
            assignments.append("enabled=?")
            values.append(int(enabled))
        values.append(subscription_id)
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE subscriptions SET {', '.join(assignments)} WHERE id=?", values
            )
        return cursor.rowcount > 0

    def update_subscription(
        self,
        subscription_id: str,
        *,
        name: str | None = None,
        spec: TenderQuerySpec | None = None,
        next_run_at: datetime | None = None,
        update_next_run: bool = False,
        delivery_channel: str | None = None,
        delivery_policy: DeliveryPolicy | None = None,
    ) -> bool:
        assignments = ["updated_at=?"]
        values: list[Any] = [utcnow_iso()]
        for column, value in (
            ("name", name),
            ("delivery_channel", delivery_channel),
            ("delivery_policy", delivery_policy.value if delivery_policy else None),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        if spec is not None:
            assignments.extend(("raw_query=?", "spec_json=?"))
            values.extend((spec.raw_query, spec.model_dump_json()))
        if update_next_run:
            assignments.append("next_run_at=?")
            values.append(next_run_at.isoformat() if next_run_at else None)
        values.append(subscription_id)
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE subscriptions SET {', '.join(assignments)} WHERE id=?", values
            )
        return cursor.rowcount > 0

    def delete_subscription(self, subscription_id: str) -> bool:
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,))
        return cursor.rowcount > 0

    def claim_due_subscription(
        self, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=?
                  AND (lease_until IS NULL OR lease_until<?)
                ORDER BY next_run_at ASC, created_at ASC
                LIMIT 1
                """,
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE subscriptions SET lease_owner=?, lease_until=?, updated_at=?
                WHERE id=?
                """,
                (worker_id, lease_until.isoformat(), utcnow_iso(), row["id"]),
            )
        return dict(row)

    def renew_subscription_lease(
        self, subscription_id: str, *, worker_id: str, lease_until: datetime
    ) -> bool:
        """Extend a lease only while it is still owned by the same worker."""
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE subscriptions SET lease_until=?, updated_at=?
                WHERE id=? AND lease_owner=?
                """,
                (
                    lease_until.isoformat(),
                    utcnow_iso(),
                    subscription_id,
                    worker_id,
                ),
            )
        return cursor.rowcount > 0

    def claim_subscription(
        self,
        subscription_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        """Claim one subscription for a user-triggered run without racing a worker."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE subscriptions SET lease_owner=?, lease_until=?, updated_at=?
                WHERE id=? AND (
                  lease_owner IS NULL OR lease_until IS NULL OR lease_until<? OR lease_owner=?
                )
                """,
                (
                    worker_id,
                    lease_until.isoformat(),
                    utcnow_iso(),
                    subscription_id,
                    now.isoformat(),
                    worker_id,
                ),
            )
        return cursor.rowcount > 0

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

    def list_subscription_runs(self, subscription_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs WHERE subscription_id=?
                ORDER BY started_at DESC LIMIT ?
                """,
                (subscription_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def create_delivery_attempt(
        self,
        *,
        run_id: str,
        subscription_id: str | None,
        channel: str,
        success: bool,
        skipped: bool,
        message: str,
        external_id: str | None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO delivery_attempts(
                  run_id, subscription_id, channel, success, skipped, message,
                  external_id, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    subscription_id,
                    channel,
                    int(success),
                    int(skipped),
                    message,
                    external_id,
                    utcnow_iso(),
                ),
            )

    def list_delivery_attempts(
        self, *, subscription_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if subscription_id:
                rows = conn.execute(
                    """
                    SELECT * FROM delivery_attempts WHERE subscription_id=?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (subscription_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM delivery_attempts ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def heartbeat_worker(
        self, *, worker_id: str, kind: str, started_at: datetime, pid: int, hostname: str
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO workers(id, kind, started_at, heartbeat_at, pid, hostname)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at,
                  pid=excluded.pid, hostname=excluded.hostname
                """,
                (
                    worker_id,
                    kind,
                    started_at.isoformat(),
                    utcnow_iso(),
                    pid,
                    hostname,
                ),
            )

    def remove_worker(self, worker_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM workers WHERE id=?", (worker_id,))

    def list_workers(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY heartbeat_at DESC").fetchall()
        return [dict(row) for row in rows]

    def get_runtime_config(self) -> dict[str, dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT field, value, is_secret, updated_at FROM runtime_config"
            ).fetchall()
        return {row["field"]: dict(row) for row in rows}

    def set_runtime_config(self, values: dict[str, tuple[str, bool]]) -> None:
        if not values:
            return
        updated_at = utcnow_iso()
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO runtime_config(field, value, is_secret, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(field) DO UPDATE SET value=excluded.value,
                  is_secret=excluded.is_secret, updated_at=excluded.updated_at
                """,
                [
                    (field, value, int(is_secret), updated_at)
                    for field, (value, is_secret) in values.items()
                ],
            )

    def get_source_authorization(self, source_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM source_authorizations WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_source_authorizations(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM source_authorizations ORDER BY authorized_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_source_authorization(
        self,
        *,
        source_id: str,
        encrypted_cookie: str,
        cookie_names: list[str],
        domains: list[str],
        authorized_at: str,
        expires_at: str | None,
        message: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO source_authorizations(
                  source_id, encrypted_cookie, cookie_names_json, domains_json,
                  authorized_at, expires_at, last_test_status, last_message
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                  encrypted_cookie=excluded.encrypted_cookie,
                  cookie_names_json=excluded.cookie_names_json,
                  domains_json=excluded.domains_json,
                  authorized_at=excluded.authorized_at,
                  expires_at=excluded.expires_at,
                  last_test_at=NULL,
                  last_test_status='not_tested',
                  last_message=excluded.last_message
                """,
                (
                    source_id,
                    encrypted_cookie,
                    json.dumps(cookie_names, ensure_ascii=False),
                    json.dumps(domains, ensure_ascii=False),
                    authorized_at,
                    expires_at,
                    "not_tested",
                    message,
                ),
            )

    def update_source_authorization_test(
        self,
        source_id: str,
        *,
        status: str,
        message: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE source_authorizations
                SET last_test_at=?, last_test_status=?, last_message=?
                WHERE source_id=?
                """,
                (utcnow_iso(), status, message, source_id),
            )

    def delete_source_authorization(self, source_id: str) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM source_authorizations WHERE source_id=?",
                (source_id,),
            )
        return cursor.rowcount > 0

    def latest_source_runs(self) -> dict[str, dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT source_runs.*, runs.started_at
                FROM source_runs
                JOIN runs ON runs.id = source_runs.run_id
                JOIN (
                  SELECT source, MAX(id) AS latest_id FROM source_runs GROUP BY source
                ) latest ON latest.latest_id = source_runs.id
                """
            ).fetchall()
        return {row["source"]: dict(row) for row in rows}
