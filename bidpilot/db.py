from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

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
            brief_json TEXT NOT NULL DEFAULT '{}',
            assessment_json TEXT NOT NULL DEFAULT '{}',
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
            delivery_targets_json TEXT NOT NULL DEFAULT '[]',
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
        CREATE TABLE IF NOT EXISTS delivery_target_ledger (
            subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            channel TEXT NOT NULL,
            canonical_id TEXT NOT NULL,
            version_hash TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            report_path TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (subscription_id, channel, canonical_id, version_hash)
        );
        CREATE TABLE IF NOT EXISTS delivery_outbox (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            subscription_id TEXT REFERENCES subscriptions(id) ON DELETE CASCADE,
            channel TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            report_path TEXT,
            new_count INTEGER NOT NULL DEFAULT 0,
            subscription_name TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            next_attempt_at TEXT,
            lease_owner TEXT,
            lease_token TEXT,
            lease_until TEXT,
            last_error TEXT,
            last_message TEXT,
            external_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            delivered_at TEXT,
            UNIQUE (run_id, channel)
        );
        CREATE TABLE IF NOT EXISTS delivery_outbox_items (
            outbox_id TEXT NOT NULL REFERENCES delivery_outbox(id) ON DELETE CASCADE,
            canonical_id TEXT NOT NULL,
            version_hash TEXT NOT NULL,
            PRIMARY KEY (outbox_id, canonical_id, version_hash)
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
        CREATE TABLE IF NOT EXISTS intelligence_briefs (
            cache_key TEXT PRIMARY KEY,
            brief_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_profiles (
            id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_items (
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            canonical_id TEXT NOT NULL,
            version_hash TEXT NOT NULL,
            position INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            PRIMARY KEY (run_id, canonical_id, version_hash)
        );
        CREATE TABLE IF NOT EXISTS opportunity_feedback (
            canonical_id TEXT NOT NULL,
            version_hash TEXT NOT NULL,
            verdict TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (canonical_id, version_hash)
        );
        CREATE TABLE IF NOT EXISTS decision_assessments (
            cache_key TEXT PRIMARY KEY,
            assessment_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_evidence_answers (
            cache_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            selection_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_items_project ON tender_items(project_key);
        CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_subscriptions_due ON subscriptions(enabled, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_delivery_attempts_run ON delivery_attempts(run_id);
        CREATE INDEX IF NOT EXISTS idx_delivery_outbox_due
          ON delivery_outbox(status, next_attempt_at, lease_until);
        CREATE INDEX IF NOT EXISTS idx_delivery_outbox_subscription
          ON delivery_outbox(subscription_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_outbox_items_key
          ON delivery_outbox_items(canonical_id, version_hash);
        CREATE INDEX IF NOT EXISTS idx_opportunities_stage ON opportunities(stage, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_opportunities_next_action ON opportunities(next_action_at);
        CREATE INDEX IF NOT EXISTS idx_source_auth_test ON source_authorizations(last_test_at DESC);
        CREATE INDEX IF NOT EXISTS idx_intelligence_briefs_used
          ON intelligence_briefs(last_used_at DESC);
        CREATE INDEX IF NOT EXISTS idx_run_items_position
          ON run_items(run_id, position);
        CREATE INDEX IF NOT EXISTS idx_opportunity_feedback_updated
          ON opportunity_feedback(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_assessments_used
          ON decision_assessments(last_used_at DESC);
        CREATE INDEX IF NOT EXISTS idx_run_evidence_answers_run
          ON run_evidence_answers(run_id, last_used_at DESC);
        """
        with self.connection() as conn:
            conn.executescript(schema)
            # Additive migrations keep existing demo databases usable across releases.
            self._ensure_column(conn, "runs", "subscription_id", "TEXT")
            self._ensure_column(conn, "runs", "trigger_reason", "TEXT NOT NULL DEFAULT 'manual'")
            self._ensure_column(
                conn, "subscriptions", "delivery_policy", "TEXT NOT NULL DEFAULT 'always'"
            )
            self._ensure_column(
                conn, "subscriptions", "delivery_targets_json", "TEXT NOT NULL DEFAULT '[]'"
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
            self._ensure_column(conn, "runs", "brief_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "runs", "assessment_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "delivery_outbox", "last_message", "TEXT")
            legacy_target_rows = conn.execute(
                """
                SELECT id, delivery_channel FROM subscriptions
                WHERE delivery_targets_json IS NULL OR delivery_targets_json='' OR delivery_targets_json='[]'
                """
            ).fetchall()
            conn.executemany(
                "UPDATE subscriptions SET delivery_targets_json=? WHERE id=?",
                [
                    (json.dumps([row["delivery_channel"] or "local"]), row["id"])
                    for row in legacy_target_rows
                ],
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO delivery_target_ledger(
                  subscription_id, channel, canonical_id, version_hash,
                  delivered_at, report_path
                )
                SELECT l.subscription_id, COALESCE(NULLIF(s.delivery_channel, ''), 'local'),
                  l.canonical_id, l.version_hash, l.delivered_at, l.report_path
                FROM delivery_ledger AS l
                JOIN subscriptions AS s ON s.id=l.subscription_id
                """
            )
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
        brief: dict[str, Any] | None = None,
        assessment: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs SET status=?, completed_at=?, report_path=?, result_count=?,
                  new_count=?, diagnostics_json=?, retrieval_json=?, brief_json=?,
                  assessment_json=?, error=? WHERE id=?
                """,
                (
                    status.value,
                    utcnow_iso(),
                    report_path,
                    result_count,
                    new_count,
                    json.dumps(diagnostics, ensure_ascii=False),
                    json.dumps(retrieval or {}, ensure_ascii=False),
                    json.dumps(brief or {}, ensure_ascii=False),
                    json.dumps(assessment or {}, ensure_ascii=False),
                    error,
                    run_id,
                ),
            )

    def get_cached_intelligence_brief(self, cache_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT brief_json FROM intelligence_briefs WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE intelligence_briefs SET last_used_at=? WHERE cache_key=?",
                    (utcnow_iso(), cache_key),
                )
        if row is None:
            return None
        try:
            return json.loads(row["brief_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def set_cached_intelligence_brief(
        self,
        cache_key: str,
        brief: dict[str, Any],
    ) -> None:
        now = utcnow_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO intelligence_briefs(cache_key, brief_json, created_at, last_used_at)
                VALUES(?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  brief_json=excluded.brief_json,
                  last_used_at=excluded.last_used_at
                """,
                (cache_key, json.dumps(brief, ensure_ascii=False), now, now),
            )

    def get_cached_decision_assessment(self, cache_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT assessment_json FROM decision_assessments WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE decision_assessments SET last_used_at=? WHERE cache_key=?",
                    (utcnow_iso(), cache_key),
                )
        if row is None:
            return None
        try:
            return json.loads(row["assessment_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def set_cached_decision_assessment(
        self,
        cache_key: str,
        assessment: dict[str, Any],
    ) -> None:
        now = utcnow_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO decision_assessments(
                  cache_key, assessment_json, created_at, last_used_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  assessment_json=excluded.assessment_json,
                  last_used_at=excluded.last_used_at
                """,
                (cache_key, json.dumps(assessment, ensure_ascii=False), now, now),
            )

    def get_cached_evidence_answer(self, cache_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT selection_json FROM run_evidence_answers WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE run_evidence_answers SET last_used_at=? WHERE cache_key=?",
                    (utcnow_iso(), cache_key),
                )
        if row is None:
            return None
        try:
            return json.loads(row["selection_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def set_cached_evidence_answer(
        self,
        cache_key: str,
        run_id: str,
        selection: dict[str, Any],
    ) -> None:
        now = utcnow_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO run_evidence_answers(
                  cache_key, run_id, selection_json, created_at, last_used_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  run_id=excluded.run_id,
                  selection_json=excluded.selection_json,
                  last_used_at=excluded.last_used_at
                """,
                (
                    cache_key,
                    run_id,
                    json.dumps(selection, ensure_ascii=False),
                    now,
                    now,
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

    def list_tender_items_for_buyer_radar(self) -> list[dict[str, Any]]:
        """Return local evidence rows newest-first without visiting any external source."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT canonical_id, version_hash, project_key, title, payload_json,
                       first_seen_at, last_seen_at
                FROM tender_items
                ORDER BY last_seen_at DESC, first_seen_at DESC, rowid DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

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

    def delete_opportunity(self, opportunity_id: str) -> bool:
        """Delete only the user workspace row; captured tender evidence remains untouched."""
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM opportunities WHERE id=?", (opportunity_id,))
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

    def undelivered_keys_by_target(
        self,
        subscription_id: str,
        targets: list[str],
        keys: list[tuple[str, str]],
    ) -> dict[str, set[tuple[str, str]]]:
        if not keys or not targets:
            return {target: set() for target in targets}
        placeholders = ",".join("?" for _ in targets)
        with self.connection() as conn:
            delivered_rows = conn.execute(
                f"""
                SELECT channel, canonical_id, version_hash
                FROM delivery_target_ledger
                WHERE subscription_id=? AND channel IN ({placeholders})
                """,
                (subscription_id, *targets),
            ).fetchall()
            reserved_rows = conn.execute(
                f"""
                SELECT o.channel, i.canonical_id, i.version_hash
                FROM delivery_outbox AS o
                JOIN delivery_outbox_items AS i ON i.outbox_id=o.id
                WHERE o.subscription_id=? AND o.channel IN ({placeholders})
                  AND o.status IN ('pending','sending','retrying','dead_letter','succeeded')
                """,
                (subscription_id, *targets),
            ).fetchall()
        delivered = {
            (row["channel"], row["canonical_id"], row["version_hash"])
            for row in [*delivered_rows, *reserved_rows]
        }
        return {
            target: {key for key in keys if (target, key[0], key[1]) not in delivered}
            for target in targets
        }

    def undelivered_keys_for_targets(
        self,
        subscription_id: str,
        targets: list[str],
        keys: list[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        """Compatibility helper returning the union missing from any target."""
        by_target = self.undelivered_keys_by_target(subscription_id, targets, keys)
        return set().union(*by_target.values()) if by_target else set()

    def mark_target_delivered(
        self,
        subscription_id: str,
        channel: str,
        keys: list[tuple[str, str]],
        report_path: str | None,
    ) -> None:
        if not keys:
            return
        now = utcnow_iso()
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO delivery_target_ledger(
                  subscription_id, channel, canonical_id, version_hash, delivered_at, report_path
                ) VALUES(?,?,?,?,?,?)
                """,
                [
                    (subscription_id, channel, canonical_id, version_hash, now, report_path or "")
                    for canonical_id, version_hash in keys
                ],
            )

    def open_delivery_targets(self, subscription_id: str) -> set[str]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT channel FROM delivery_outbox
                WHERE subscription_id=?
                  AND status IN ('pending','sending','retrying','dead_letter')
                """,
                (subscription_id,),
            ).fetchall()
        return {row["channel"] for row in rows}

    def delivery_outbox_run_ids_for_targets(
        self,
        subscription_id: str,
        targets: list[str],
    ) -> list[str]:
        if not targets:
            return []
        placeholders = ",".join("?" for _ in targets)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT run_id FROM delivery_outbox
                WHERE subscription_id=? AND channel IN ({placeholders})
                  AND status IN ('pending','sending','retrying','dead_letter')
                """,
                (subscription_id, *targets),
            ).fetchall()
        return [row["run_id"] for row in rows]

    def create_subscription(
        self,
        subscription_id: str,
        name: str,
        spec: TenderQuerySpec,
        delivery_channel: str,
        next_run_at: datetime | None,
        delivery_policy: DeliveryPolicy = DeliveryPolicy.ALWAYS,
        delivery_targets: list[str] | None = None,
    ) -> None:
        targets = delivery_targets or [delivery_channel or "local"]
        now = utcnow_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions(
                  id, name, raw_query, spec_json, delivery_channel, delivery_targets_json,
                  delivery_policy, created_at, updated_at, next_run_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    subscription_id,
                    name,
                    spec.raw_query,
                    spec.model_dump_json(),
                    delivery_channel,
                    json.dumps(targets, ensure_ascii=False),
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
        self,
        subscription_id: str,
        next_run_at: datetime | None,
        *,
        enabled: bool | None = None,
        only_if_idle_at: datetime | None = None,
    ) -> bool:
        assignments = ["next_run_at=?", "updated_at=?", "lease_owner=NULL", "lease_until=NULL"]
        values: list[Any] = [next_run_at.isoformat() if next_run_at else None, utcnow_iso()]
        if enabled is not None:
            assignments.append("enabled=?")
            values.append(int(enabled))
        where = "id=?"
        values.append(subscription_id)
        if only_if_idle_at is not None:
            where += " AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until<=?)"
            values.append(only_if_idle_at.isoformat())
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE subscriptions SET {', '.join(assignments)} WHERE {where}", values
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
        delivery_targets: list[str] | None = None,
        delivery_policy: DeliveryPolicy | None = None,
        cancel_delivery_targets: list[str] | None = None,
        only_if_idle_at: datetime | None = None,
    ) -> bool:
        assignments = ["updated_at=?"]
        values: list[Any] = [utcnow_iso()]
        for column, value in (
            ("name", name),
            ("delivery_channel", delivery_channel),
            (
                "delivery_targets_json",
                json.dumps(delivery_targets, ensure_ascii=False)
                if delivery_targets is not None
                else None,
            ),
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
        where = "id=?"
        values.append(subscription_id)
        if only_if_idle_at is not None:
            where += " AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until<=?)"
            values.append(only_if_idle_at.isoformat())
        cancel_targets = list(dict.fromkeys(cancel_delivery_targets or []))
        cancel_placeholders = ",".join("?" for _ in cancel_targets)
        if cancel_targets:
            where += (
                " AND NOT EXISTS (SELECT 1 FROM delivery_outbox AS o "
                "WHERE o.subscription_id=subscriptions.id "
                f"AND o.channel IN ({cancel_placeholders}) AND o.status='sending')"
            )
            values.extend(cancel_targets)
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE subscriptions SET {', '.join(assignments)} WHERE {where}", values
            )
            if cursor.rowcount == 1 and cancel_targets:
                rows = conn.execute(
                    f"""
                    SELECT id, run_id, channel FROM delivery_outbox
                    WHERE subscription_id=? AND channel IN ({cancel_placeholders})
                      AND status IN ('pending','retrying','dead_letter')
                    """,
                    (subscription_id, *cancel_targets),
                ).fetchall()
                message = "该交付目标已从订阅中移除；原待处理任务已取消。"
                conn.execute(
                    f"""
                    UPDATE delivery_outbox SET status='skipped', next_attempt_at=NULL,
                      lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                      last_error=NULL, last_message=?, updated_at=?
                    WHERE subscription_id=? AND channel IN ({cancel_placeholders})
                      AND status IN ('pending','retrying','dead_letter')
                    """,
                    (message, utcnow_iso(), subscription_id, *cancel_targets),
                )
                conn.executemany(
                    """
                    INSERT INTO delivery_attempts(
                      run_id, subscription_id, channel, success, skipped, message,
                      external_id, created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            row["run_id"],
                            subscription_id,
                            row["channel"],
                            1,
                            1,
                            message,
                            None,
                            utcnow_iso(),
                        )
                        for row in rows
                    ],
                )
        return cursor.rowcount > 0

    def delete_subscription(
        self,
        subscription_id: str,
        *,
        only_if_idle_at: datetime | None = None,
    ) -> bool:
        where = "id=?"
        values: list[Any] = [subscription_id]
        if only_if_idle_at is not None:
            where += " AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until<=?)"
            values.append(only_if_idle_at.isoformat())
        where += (
            " AND NOT EXISTS (SELECT 1 FROM delivery_outbox AS o "
            "WHERE o.subscription_id=subscriptions.id AND o.status='sending')"
        )
        with self.connection() as conn:
            cursor = conn.execute(f"DELETE FROM subscriptions WHERE {where}", values)
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

    def set_run_status(self, run_id: str, status: RunStatus) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status=? WHERE id=? AND status IN ('partial','completed')",
                (status.value, run_id),
            )
        return cursor.rowcount == 1

    def update_run_assessment(self, run_id: str, assessment: dict[str, Any]) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET assessment_json=? WHERE id=?",
                (json.dumps(assessment, ensure_ascii=False), run_id),
            )
        return cursor.rowcount > 0

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

    def stage_delivery_outbox(
        self,
        *,
        run_id: str,
        subscription_id: str | None,
        entries: list[dict[str, Any]],
        available_at: datetime | None = None,
        report_id: str | None = None,
        report_path: str | None = None,
        report_item_count: int = 0,
    ) -> list[dict[str, Any]]:
        """Persist the report row and every destination before any external send."""
        if not entries:
            return []
        now = utcnow_iso()
        due_at = available_at.isoformat() if available_at else now
        outbox_ids: list[str] = []
        with self.connection() as conn:
            if report_id and report_path:
                conn.execute(
                    """
                    INSERT INTO reports(id, run_id, subscription_id, path, item_count, created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        report_id,
                        run_id,
                        subscription_id,
                        report_path,
                        report_item_count,
                        now,
                    ),
                )
            for entry in entries:
                status = str(entry.get("status", "pending"))
                if status not in {"pending", "retrying", "skipped"}:
                    raise ValueError(f"不支持的初始投递队列状态：{status}")
                max_attempts = int(entry.get("max_attempts", 5))
                if not 1 <= max_attempts <= 20:
                    raise ValueError("投递最大尝试次数必须在 1～20 之间")
                outbox_id = uuid4().hex
                outbox_ids.append(outbox_id)
                message = str(entry.get("message") or "")[:500] or None
                next_attempt_at = due_at if status in {"pending", "retrying"} else None
                conn.execute(
                    """
                    INSERT INTO delivery_outbox(
                      id, run_id, subscription_id, channel, status, report_path, new_count,
                      subscription_name, attempt_count, max_attempts, next_attempt_at,
                      last_error, last_message, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        outbox_id,
                        run_id,
                        subscription_id,
                        str(entry["channel"]),
                        status,
                        entry.get("report_path"),
                        int(entry.get("new_count", 0)),
                        entry.get("subscription_name"),
                        0,
                        max_attempts,
                        next_attempt_at,
                        message if status == "retrying" else None,
                        message,
                        now,
                        now,
                    ),
                )
                keys = list(dict.fromkeys(entry.get("keys") or []))
                conn.executemany(
                    """
                    INSERT INTO delivery_outbox_items(outbox_id, canonical_id, version_hash)
                    VALUES(?,?,?)
                    """,
                    [
                        (outbox_id, canonical_id, version_hash)
                        for canonical_id, version_hash in keys
                    ],
                )
                if status == "skipped":
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
                            str(entry["channel"]),
                            1,
                            1,
                            message or "本轮按通知策略跳过外发。",
                            None,
                            now,
                        ),
                    )
        rows = [self.get_delivery_outbox(outbox_id) for outbox_id in outbox_ids]
        if any(row is None for row in rows):
            raise RuntimeError("投递队列批量写入后无法完整读取")
        return [row for row in rows if row is not None]

    def create_delivery_outbox_item(
        self,
        *,
        run_id: str,
        subscription_id: str | None,
        channel: str,
        report_path: str | None,
        new_count: int,
        subscription_name: str | None,
        status: str = "pending",
        message: str | None = None,
        max_attempts: int = 5,
        keys: list[tuple[str, str]] | None = None,
        available_at: datetime | None = None,
    ) -> dict[str, Any]:
        return self.stage_delivery_outbox(
            run_id=run_id,
            subscription_id=subscription_id,
            available_at=available_at,
            entries=[
                {
                    "channel": channel,
                    "report_path": report_path,
                    "new_count": new_count,
                    "subscription_name": subscription_name,
                    "status": status,
                    "message": message,
                    "max_attempts": max_attempts,
                    "keys": keys or [],
                }
            ],
        )[0]

    def get_delivery_outbox(self, outbox_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT o.*,
                  (SELECT COUNT(*) FROM delivery_outbox_items AS i WHERE i.outbox_id=o.id)
                    AS item_count
                FROM delivery_outbox AS o WHERE o.id=?
                """,
                (outbox_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_delivery_outbox_items(self, outbox_id: str) -> list[tuple[str, str]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT canonical_id, version_hash FROM delivery_outbox_items
                WHERE outbox_id=? ORDER BY canonical_id, version_hash
                """,
                (outbox_id,),
            ).fetchall()
        return [(row["canonical_id"], row["version_hash"]) for row in rows]

    def claim_new_delivery_outbox(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> dict[str, Any] | None:
        """Claim a newly staged item before its delayed worker-visible due time."""
        token = uuid4().hex
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE delivery_outbox SET status='sending', lease_owner=?, lease_token=?,
                  lease_until=?, attempt_count=1, updated_at=?
                WHERE id=? AND status='pending' AND attempt_count=0
                  AND (lease_until IS NULL OR lease_until<?)
                """,
                (
                    worker_id,
                    token,
                    lease_until.isoformat(),
                    utcnow_iso(),
                    outbox_id,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM delivery_outbox WHERE id=? AND lease_token=?",
                (outbox_id, token),
            ).fetchone()
        return dict(row) if row else None

    def claim_due_delivery_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        outbox_id: str | None = None,
    ) -> dict[str, Any] | None:
        token = uuid4().hex
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            filters = [
                "((status IN ('pending','retrying') AND next_attempt_at IS NOT NULL "
                "AND next_attempt_at<=?) OR (status='sending' AND lease_until<?))",
                "(lease_until IS NULL OR lease_until<?)",
            ]
            values: list[Any] = [now.isoformat(), now.isoformat(), now.isoformat()]
            if outbox_id is not None:
                filters.append("id=?")
                values.append(outbox_id)
            row = conn.execute(
                f"""
                SELECT * FROM delivery_outbox
                WHERE {" AND ".join(filters)}
                ORDER BY next_attempt_at ASC, created_at ASC
                LIMIT 1
                """,
                values,
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE delivery_outbox SET status='sending', lease_owner=?, lease_token=?,
                  lease_until=?, attempt_count=attempt_count+1, updated_at=?
                WHERE id=? AND (
                  (status IN ('pending','retrying') AND next_attempt_at<=?)
                  OR (status='sending' AND lease_until<?)
                ) AND (lease_until IS NULL OR lease_until<?)
                """,
                (
                    worker_id,
                    token,
                    lease_until.isoformat(),
                    utcnow_iso(),
                    row["id"],
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM delivery_outbox WHERE id=? AND lease_token=?",
                (row["id"], token),
            ).fetchone()
        return dict(claimed) if claimed else None

    def finish_delivery_outbox(
        self,
        outbox_id: str,
        *,
        lease_token: str,
        status: str,
        message: str,
        external_id: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> bool:
        if status not in {"succeeded", "retrying", "dead_letter", "skipped"}:
            raise ValueError(f"不支持的投递队列状态：{status}")
        delivered_at = utcnow_iso() if status == "succeeded" else None
        with self.connection() as conn:
            owned = conn.execute(
                """
                SELECT run_id, subscription_id, channel, report_path FROM delivery_outbox
                WHERE id=? AND lease_token=? AND status='sending'
                """,
                (outbox_id, lease_token),
            ).fetchone()
            if owned is None:
                return False
            cursor = conn.execute(
                """
                UPDATE delivery_outbox SET status=?, next_attempt_at=?, lease_owner=NULL,
                  lease_token=NULL, lease_until=NULL, last_error=?, last_message=?,
                  external_id=?, updated_at=?, delivered_at=?
                WHERE id=? AND lease_token=? AND status='sending'
                """,
                (
                    status,
                    next_attempt_at.isoformat() if next_attempt_at else None,
                    message if status in {"retrying", "dead_letter"} else None,
                    message,
                    external_id,
                    utcnow_iso(),
                    delivered_at,
                    outbox_id,
                    lease_token,
                ),
            )
            if cursor.rowcount == 1:
                if status == "succeeded" and owned["subscription_id"]:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO delivery_target_ledger(
                          subscription_id, channel, canonical_id, version_hash,
                          delivered_at, report_path
                        )
                        SELECT ?, ?, canonical_id, version_hash, ?, ?
                        FROM delivery_outbox_items WHERE outbox_id=?
                        """,
                        (
                            owned["subscription_id"],
                            owned["channel"],
                            delivered_at,
                            owned["report_path"] or "",
                            outbox_id,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO delivery_attempts(
                      run_id, subscription_id, channel, success, skipped, message,
                      external_id, created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        owned["run_id"],
                        owned["subscription_id"],
                        owned["channel"],
                        int(status == "succeeded"),
                        int(status == "skipped"),
                        message,
                        external_id,
                        utcnow_iso(),
                    ),
                )
        return cursor.rowcount == 1

    def retry_delivery_outbox(self, outbox_id: str, *, now: datetime) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE delivery_outbox SET status='retrying', attempt_count=0,
                  next_attempt_at=?, lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                  last_error=NULL, last_message='已手动重新排队。', updated_at=?
                WHERE id=? AND status='dead_letter'
                """,
                (now.isoformat(), utcnow_iso(), outbox_id),
            )
        return cursor.rowcount == 1

    def delivery_outbox_summary(self, run_id: str) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM delivery_outbox WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def list_delivery_outbox(
        self,
        *,
        subscription_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[Any] = []
        if subscription_id is not None:
            filters.append("subscription_id=?")
            values.append(subscription_id)
        if run_id is not None:
            filters.append("run_id=?")
            values.append(run_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT o.*,
                  (SELECT COUNT(*) FROM delivery_outbox_items AS i WHERE i.outbox_id=o.id)
                    AS item_count
                FROM delivery_outbox AS o {where}
                ORDER BY o.created_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_delivery_attempts(
        self, *, subscription_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if subscription_id:
                rows = conn.execute(
                    """
                    SELECT a.*, o.id AS outbox_id, o.status AS outbox_status,
                      o.attempt_count, o.max_attempts, o.next_attempt_at,
                      o.last_error, o.last_message, o.report_path,
                      o.new_count, o.delivered_at
                    FROM delivery_attempts AS a
                    LEFT JOIN delivery_outbox AS o
                      ON o.run_id=a.run_id AND o.channel=a.channel
                    WHERE a.subscription_id=?
                    ORDER BY a.created_at DESC LIMIT ?
                    """,
                    (subscription_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT a.*, o.id AS outbox_id, o.status AS outbox_status,
                      o.attempt_count, o.max_attempts, o.next_attempt_at,
                      o.last_error, o.last_message, o.report_path,
                      o.new_count, o.delivered_at
                    FROM delivery_attempts AS a
                    LEFT JOIN delivery_outbox AS o
                      ON o.run_id=a.run_id AND o.channel=a.channel
                    ORDER BY a.created_at DESC LIMIT ?
                    """,
                    (limit,),
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

    def get_runtime_config_revision(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM runtime_config WHERE field='_revision'"
            ).fetchone()
        if row is None:
            return 0
        try:
            return max(0, int(row["value"]))
        except (TypeError, ValueError):
            return 0

    def set_runtime_config(
        self,
        values: dict[str, tuple[str, bool]],
        *,
        delete_fields: set[str] | frozenset[str] = frozenset(),
        expected_revision: int | None = None,
    ) -> int | None:
        updated_at = utcnow_iso()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM runtime_config WHERE field='_revision'"
            ).fetchone()
            try:
                current_revision = max(0, int(row["value"])) if row else 0
            except (TypeError, ValueError):
                current_revision = 0
            if expected_revision is not None and current_revision != expected_revision:
                return None
            safe_deletes = {field for field in delete_fields if field != "_revision"}
            if not values and not safe_deletes:
                return current_revision
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
            if safe_deletes:
                conn.executemany(
                    "DELETE FROM runtime_config WHERE field=?",
                    [(field,) for field in safe_deletes],
                )
            new_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO runtime_config(field, value, is_secret, updated_at)
                VALUES('_revision', ?, 0, ?)
                ON CONFLICT(field) DO UPDATE SET value=excluded.value,
                  is_secret=0, updated_at=excluded.updated_at
                """,
                (str(new_revision), updated_at),
            )
        return new_revision

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

    def list_source_run_history(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT source_runs.*, runs.started_at, runs.raw_query, runs.status AS run_status
                FROM source_runs
                JOIN runs ON runs.id = source_runs.run_id
                ORDER BY source_runs.id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 5000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_company_profile(self, profile_id: str = "default") -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM company_profiles WHERE id=?",
                (profile_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_company_profile(
        self,
        profile: dict[str, Any],
        profile_id: str = "default",
    ) -> dict[str, Any]:
        updated_at = utcnow_iso()
        payload = json.dumps(profile, ensure_ascii=False)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO company_profiles(id, profile_json, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  profile_json=excluded.profile_json,
                  updated_at=excluded.updated_at
                """,
                (profile_id, payload, updated_at),
            )
        return {"id": profile_id, "profile_json": payload, "updated_at": updated_at}

    def set_run_items(self, run_id: str, records: list[dict[str, Any]]) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM run_items WHERE run_id=?", (run_id,))
            conn.executemany(
                """
                INSERT INTO run_items(
                  run_id, canonical_id, version_hash, position, snapshot_json
                ) VALUES(?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        record["canonical_id"],
                        record["version_hash"],
                        position,
                        json.dumps(record, ensure_ascii=False, default=str),
                    )
                    for position, record in enumerate(records)
                ],
            )

    def list_run_items(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT canonical_id, version_hash, position, snapshot_json
                FROM run_items WHERE run_id=? ORDER BY position ASC
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_feedback(
        self,
        *,
        canonical_id: str,
        version_hash: str,
        verdict: str,
        reason: str,
    ) -> dict[str, Any]:
        now = utcnow_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO opportunity_feedback(
                  canonical_id, version_hash, verdict, reason, created_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(canonical_id, version_hash) DO UPDATE SET
                  verdict=excluded.verdict,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                (canonical_id, version_hash, verdict, reason, now, now),
            )
            row = conn.execute(
                """
                SELECT * FROM opportunity_feedback
                WHERE canonical_id=? AND version_hash=?
                """,
                (canonical_id, version_hash),
            ).fetchone()
        assert row is not None
        return dict(row)

    def get_feedback(self, canonical_id: str, version_hash: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM opportunity_feedback
                WHERE canonical_id=? AND version_hash=?
                """,
                (canonical_id, version_hash),
            ).fetchone()
        return dict(row) if row else None

    def list_feedback(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM opportunity_feedback
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(limit, 5000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_feedback(self, canonical_id: str, version_hash: str) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM opportunity_feedback
                WHERE canonical_id=? AND version_hash=?
                """,
                (canonical_id, version_hash),
            )
        return cursor.rowcount > 0

    def clear_feedback(self) -> int:
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM opportunity_feedback")
        return cursor.rowcount
