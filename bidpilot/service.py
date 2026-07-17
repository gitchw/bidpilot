from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.delivery import DeliveryManager
from bidpilot.intent import IntentParser
from bidpilot.models import (
    RunResult,
    RunStatus,
    ScheduleKind,
    SourceStatus,
    Subscription,
    TenderQuerySpec,
)
from bidpilot.pipeline import TenderPipeline
from bidpilot.report import generate_report
from bidpilot.sources import CCGPSource, CECBidSource, QianlimaSource
from bidpilot.sources.base import SourceAdapter


class BidPilotService:
    def __init__(self, settings: Settings, sources: list[SourceAdapter] | None = None):
        self.settings = settings
        self.settings.ensure_directories()
        self.db = Database(settings.database_path)
        self.parser = IntentParser(settings.timezone)
        self.sources = sources or [
            CECBidSource(settings),
            CCGPSource(settings),
            QianlimaSource(settings),
        ]
        self.pipeline = TenderPipeline(settings, self.sources)
        self.delivery = DeliveryManager(settings)
        self._live_results: dict[str, RunResult] = {}
        self.scheduler = None

    async def run_query(
        self,
        query: str,
        *,
        subscription_id: str | None = None,
        delivery_channel: str | None = None,
    ) -> RunResult:
        started_at = datetime.now(ZoneInfo(self.settings.timezone))
        spec = self.parser.parse(query, now=started_at)
        if delivery_channel:
            spec.delivery_channel = delivery_channel
        run_id = uuid4().hex
        self.db.create_run(run_id, spec)
        try:
            pipeline_result = await self.pipeline.run(spec)
            records = pipeline_result.records
            self.db.upsert_records([record.model_dump(mode="json") for record in records])
            for diagnostic in pipeline_result.diagnostics:
                self.db.add_source_run(run_id, diagnostic.model_dump(mode="json"))

            incremental = subscription_id is not None
            output_records = records
            if subscription_id:
                keys = [(record.canonical_id, record.version_hash) for record in records]
                allowed = self.db.undelivered_keys(subscription_id, keys)
                output_records = [
                    record
                    for record in records
                    if (record.canonical_id, record.version_hash) in allowed
                ]

            report_path: Path | None = None
            if output_records or not incremental:
                report_path = generate_report(
                    spec,
                    output_records,
                    pipeline_result.diagnostics,
                    self.settings.report_dir,
                    generated_at=started_at,
                    incremental=incremental,
                )
                receipt = await self.delivery.deliver(
                    report_path,
                    spec.delivery_channel if delivery_channel is None else delivery_channel,
                )
                if not receipt.success:
                    raise RuntimeError(receipt.message)
                self.db.create_report(
                    uuid4().hex,
                    run_id,
                    str(report_path),
                    len(output_records),
                    subscription_id,
                )
                if subscription_id:
                    self.db.mark_delivered(
                        subscription_id,
                        [(record.canonical_id, record.version_hash) for record in output_records],
                        str(report_path),
                    )

            partial_statuses = {
                SourceStatus.PARTIAL,
                SourceStatus.AUTH_REQUIRED,
                SourceStatus.FAILED,
            }
            status = (
                RunStatus.PARTIAL
                if any(item.status in partial_statuses for item in pipeline_result.diagnostics)
                else RunStatus.COMPLETED
            )
            completed_at = datetime.now(ZoneInfo(self.settings.timezone))
            result = RunResult(
                run_id=run_id,
                status=status,
                spec=spec,
                records=output_records,
                diagnostics=pipeline_result.diagnostics,
                report_path=str(report_path) if report_path else None,
                new_count=len(output_records),
                started_at=started_at,
                completed_at=completed_at,
                warnings=spec.warnings,
            )
            self.db.complete_run(
                run_id,
                status,
                report_path=str(report_path) if report_path else None,
                result_count=len(records),
                new_count=len(output_records),
                diagnostics=[item.model_dump(mode="json") for item in pipeline_result.diagnostics],
            )
            self._live_results[run_id] = result
            return result
        except Exception as exc:
            self.db.complete_run(
                run_id,
                RunStatus.FAILED,
                report_path=None,
                result_count=0,
                new_count=0,
                diagnostics=[],
                error=str(exc),
            )
            raise

    def create_subscription(
        self, name: str, query: str, delivery_channel: str = "local"
    ) -> Subscription:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        spec = self.parser.parse(query, now=now)
        if spec.schedule.kind == ScheduleKind.IMMEDIATE:
            raise ValueError("订阅问题必须包含每天、每周或明确的未来发送时间")
        spec.delivery_channel = delivery_channel
        for row in self.db.list_subscriptions():
            if (
                row["enabled"]
                and row["raw_query"] == spec.raw_query
                and row["delivery_channel"] == delivery_channel
            ):
                existing_spec = TenderQuerySpec.model_validate_json(row["spec_json"])
                return Subscription(
                    id=row["id"],
                    name=row["name"],
                    spec=existing_spec,
                    enabled=bool(row["enabled"]),
                    delivery_channel=row["delivery_channel"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    last_run_at=(
                        datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None
                    ),
                    next_run_at=(
                        datetime.fromisoformat(row["next_run_at"]) if row["next_run_at"] else None
                    ),
                )
        subscription_id = uuid4().hex
        subscription = Subscription(
            id=subscription_id,
            name=name,
            spec=spec,
            delivery_channel=delivery_channel,
            created_at=now,
        )
        self.db.create_subscription(
            subscription_id,
            name,
            spec,
            delivery_channel,
            subscription.next_run_at,
        )
        if self.scheduler:
            self.scheduler.add_subscription(subscription_id, spec)
        return subscription

    async def run_subscription(self, subscription_id: str) -> RunResult:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        spec = TenderQuerySpec.model_validate_json(row["spec_json"])
        result = await self.run_query(
            spec.raw_query,
            subscription_id=subscription_id,
            delivery_channel=row["delivery_channel"],
        )
        self.db.update_subscription_run(
            subscription_id,
            last_run_at=result.completed_at or datetime.now(ZoneInfo(self.settings.timezone)),
            next_run_at=None,
        )
        return result

    def list_subscriptions(self) -> list[dict]:
        rows = self.db.list_subscriptions()
        for row in rows:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["enabled"] = bool(row["enabled"])
        return rows

    def list_runs(self, limit: int = 30) -> list[dict]:
        rows = self.db.list_runs(limit)
        for row in rows:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["diagnostics"] = json.loads(row.pop("diagnostics_json"))
        return rows

    def get_run(self, run_id: str) -> dict | RunResult | None:
        if run_id in self._live_results:
            return self._live_results[run_id]
        row = self.db.get_run(run_id)
        if row:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["diagnostics"] = json.loads(row.pop("diagnostics_json"))
        return row

    def list_reports(self) -> list[dict]:
        return self.db.list_reports()

    def source_status(self) -> list[dict]:
        return [
            {
                "name": source.name,
                "requires_auth": source.requires_auth,
                "configured": (
                    bool(self.settings.load_qianlima_cookie())
                    if isinstance(source, QianlimaSource)
                    else True
                ),
                "member_enhanced": (
                    bool(self.settings.cecbid_cookie)
                    if isinstance(source, CECBidSource)
                    else bool(self.settings.load_qianlima_cookie())
                    if isinstance(source, QianlimaSource)
                    else False
                ),
                "mode": (
                    "授权免费会员"
                    if source.requires_auth
                    else "公开 + 会员增强"
                    if isinstance(source, CECBidSource)
                    else "公开"
                ),
            }
            for source in self.sources
        ]
