from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.delivery import DeliveryManager, DeliveryReceipt
from bidpilot.intent import IntentParser
from bidpilot.models import (
    DeliveryPolicy,
    EventType,
    Opportunity,
    OpportunityCreate,
    OpportunityStage,
    OpportunityUpdate,
    RunResult,
    RunStatus,
    ScheduleKind,
    SourceStatus,
    Subscription,
    SubscriptionUpdate,
    TenderQuerySpec,
    TenderRecord,
)
from bidpilot.pipeline import TenderPipeline
from bidpilot.report import generate_report
from bidpilot.scheduler import maintain_subscription_lease, next_schedule_time, retry_time
from bidpilot.sources import CCGPSource, CECBidSource, GGZYSource, MofcomSource, QianlimaSource
from bidpilot.sources.base import SourceAdapter

_EVENT_RECENCY_RANK = {
    EventType.INTENTION: 0,
    EventType.TENDER: 1,
    EventType.CHANGE: 2,
    EventType.AWARD: 3,
    EventType.CONTRACT: 4,
    EventType.OTHER: 0,
}


class RunExecutionError(RuntimeError):
    def __init__(self, run_id: str, message: str):
        super().__init__(message)
        self.run_id = run_id


class SubscriptionBusyError(RuntimeError):
    pass


class BidPilotService:
    def __init__(self, settings: Settings, sources: list[SourceAdapter] | None = None):
        self.settings = settings
        self.settings.ensure_directories()
        self.db = Database(settings.database_path)
        self.parser = IntentParser(settings.timezone)
        self.sources = sources or [
            CECBidSource(settings),
            CCGPSource(settings),
            GGZYSource(settings),
            MofcomSource(settings),
            QianlimaSource(settings),
        ]
        self.pipeline = TenderPipeline(settings, self.sources)
        self.delivery = DeliveryManager(settings)
        self._live_results: dict[str, RunResult] = {}
        self.worker = None

    async def run_query(
        self,
        query: str,
        *,
        subscription_id: str | None = None,
        delivery_channel: str | None = None,
        trigger_reason: str = "manual",
    ) -> RunResult:
        started_at = datetime.now(ZoneInfo(self.settings.timezone))
        spec = self.parser.parse(query, now=started_at)
        if delivery_channel:
            spec.delivery_channel = delivery_channel
        channel = spec.delivery_channel
        run_id = uuid4().hex
        self.db.create_run(
            run_id,
            spec,
            subscription_id=subscription_id,
            trigger_reason=trigger_reason,
        )
        diagnostics_dump: list[dict] = []
        result_count = 0
        report_path: Path | None = None
        subscription_row = self.db.get_subscription(subscription_id) if subscription_id else None
        try:
            pipeline_result = await self.pipeline.run(spec)
            records = pipeline_result.records
            result_count = len(records)
            self.db.upsert_records([record.model_dump(mode="json") for record in records])
            self._refresh_opportunities_for_projects(records)
            diagnostics_dump = [
                item.model_dump(mode="json") for item in pipeline_result.diagnostics
            ]
            for diagnostic in diagnostics_dump:
                self.db.add_source_run(run_id, diagnostic)

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

            if output_records or not incremental:
                report_path = generate_report(
                    spec,
                    output_records,
                    pipeline_result.diagnostics,
                    self.settings.report_dir,
                    generated_at=started_at,
                    incremental=incremental,
                )

            policy = DeliveryPolicy(
                subscription_row.get("delivery_policy", DeliveryPolicy.ALWAYS.value)
                if subscription_row
                else DeliveryPolicy.ALWAYS.value
            )
            should_notify = report_path is not None or (
                incremental and policy == DeliveryPolicy.ALWAYS
            )
            if should_notify:
                try:
                    receipt = await self.delivery.deliver(
                        report_path,
                        channel,
                        new_count=len(output_records),
                        subscription_name=subscription_row["name"] if subscription_row else None,
                    )
                except Exception as exc:
                    self.db.create_delivery_attempt(
                        run_id=run_id,
                        subscription_id=subscription_id,
                        channel=channel,
                        success=False,
                        skipped=False,
                        message=str(exc),
                        external_id=None,
                    )
                    raise
            else:
                receipt = DeliveryReceipt(
                    channel=channel,
                    success=True,
                    skipped=True,
                    message="本轮无新增；按“仅有变化时通知”策略未外发。",
                )
            self.db.create_delivery_attempt(
                run_id=run_id,
                subscription_id=subscription_id,
                channel=receipt.channel,
                success=receipt.success,
                skipped=receipt.skipped,
                message=receipt.message,
                external_id=receipt.external_id,
            )
            if not receipt.success:
                raise RuntimeError(receipt.message)

            if report_path:
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
                delivery_channel=receipt.channel,
                delivery_status="skipped" if receipt.skipped else "success",
                delivery_message=receipt.message,
            )
            self.db.complete_run(
                run_id,
                status,
                report_path=str(report_path) if report_path else None,
                result_count=result_count,
                new_count=len(output_records),
                diagnostics=diagnostics_dump,
            )
            self._live_results[run_id] = result
            return result
        except Exception as exc:
            self.db.complete_run(
                run_id,
                RunStatus.FAILED,
                report_path=str(report_path) if report_path else None,
                result_count=result_count,
                new_count=0,
                diagnostics=diagnostics_dump,
                error=str(exc),
            )
            raise RunExecutionError(run_id, str(exc)) from exc

    def _channel_is_configured(self, channel: str) -> bool:
        if channel == "feishu":
            return any(
                item["configured"]
                for item in self.delivery.channel_status()
                if item["id"].startswith("feishu_")
            )
        return any(
            item["id"] == channel and item["configured"] for item in self.delivery.channel_status()
        )

    def create_subscription(
        self,
        name: str,
        query: str,
        delivery_channel: str = "local",
        delivery_policy: DeliveryPolicy = DeliveryPolicy.ALWAYS,
        run_immediately: bool = True,
    ) -> Subscription:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        spec = self.parser.parse(query, now=now)
        if spec.schedule.kind == ScheduleKind.IMMEDIATE:
            raise ValueError("订阅问题必须包含每天、每周或明确的未来发送时间")
        if not self._channel_is_configured(delivery_channel):
            raise ValueError(f"投递通道 {delivery_channel} 尚未配置，不能创建虚假推送承诺")
        delivery_policy = DeliveryPolicy(delivery_policy)
        spec.delivery_channel = delivery_channel
        for row in self.db.list_subscriptions():
            if row["raw_query"] == spec.raw_query and row["delivery_channel"] == delivery_channel:
                existing = self._subscription_from_row(row)
                if run_immediately and not existing.in_progress:
                    self.db.set_subscription_due(row["id"], now, enabled=True)
                    row = self.db.get_subscription(row["id"])
                    assert row is not None
                return self._subscription_from_row(row)

        subscription_id = uuid4().hex
        next_run_at = now if run_immediately else next_schedule_time(spec.schedule, now)
        self.db.create_subscription(
            subscription_id,
            name,
            spec,
            delivery_channel,
            next_run_at,
            delivery_policy,
        )
        row = self.db.get_subscription(subscription_id)
        assert row is not None
        return self._subscription_from_row(row)

    async def run_subscription(
        self,
        subscription_id: str,
        *,
        trigger_reason: str = "manual",
        lease_owner: str | None = None,
    ) -> RunResult:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        now = datetime.now(ZoneInfo(self.settings.timezone))
        manually_claimed = lease_owner is None
        if lease_owner is None:
            lease_owner = f"manual:{uuid4().hex}"
            if not self.db.claim_subscription(
                subscription_id,
                worker_id=lease_owner,
                now=now,
                lease_until=now + timedelta(seconds=self.settings.worker_lease_seconds),
            ):
                raise SubscriptionBusyError("该订阅正在执行，请等待本轮完成后再试")
            row = self.db.get_subscription(subscription_id)
            assert row is not None
        elif row.get("lease_owner") != lease_owner:
            raise SubscriptionBusyError("订阅租约已被其他 worker 接管，本轮不再重复执行")
        spec = TenderQuerySpec.model_validate_json(row["spec_json"])
        renewal = None
        if manually_claimed:
            renewal = asyncio.create_task(
                maintain_subscription_lease(
                    self.db,
                    subscription_id,
                    worker_id=lease_owner,
                    timezone=ZoneInfo(self.settings.timezone),
                    lease_seconds=self.settings.worker_lease_seconds,
                ),
                name=f"bidpilot-manual-lease:{subscription_id}",
            )
        try:
            try:
                result = await self.run_query(
                    spec.raw_query,
                    subscription_id=subscription_id,
                    delivery_channel=row["delivery_channel"],
                    trigger_reason=trigger_reason,
                )
            except RunExecutionError as exc:
                failed_at = datetime.now(ZoneInfo(self.settings.timezone))
                retry_at = retry_time(failed_at, int(row.get("consecutive_failures", 0)))
                regular_at = next_schedule_time(spec.schedule, failed_at)
                next_run_at = min(retry_at, regular_at) if regular_at else retry_at
                self.db.finish_subscription_attempt(
                    subscription_id,
                    last_run_at=failed_at,
                    next_run_at=next_run_at,
                    status=RunStatus.FAILED,
                    message=str(exc),
                    new_count=0,
                    run_id=exc.run_id,
                    success=False,
                )
                raise

            finished_at = result.completed_at or datetime.now(ZoneInfo(self.settings.timezone))
            next_run_at = next_schedule_time(spec.schedule, finished_at)
            self.db.finish_subscription_attempt(
                subscription_id,
                last_run_at=finished_at,
                next_run_at=next_run_at,
                status=result.status,
                message=result.delivery_message or f"运行完成，新增 {result.new_count} 条。",
                new_count=result.new_count,
                run_id=result.run_id,
                success=True,
            )
            if spec.schedule.kind == ScheduleKind.ONCE and next_run_at is None:
                self.db.set_subscription_due(subscription_id, None, enabled=False)
            return result
        finally:
            if renewal:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal

    def repair_subscription_schedules(self) -> int:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        repaired = 0
        for row in self.db.list_subscriptions():
            if not row["enabled"] or row["next_run_at"]:
                continue
            spec = TenderQuerySpec.model_validate_json(row["spec_json"])
            next_run_at = next_schedule_time(spec.schedule, now)
            if next_run_at:
                self.db.set_subscription_due(row["id"], next_run_at)
                repaired += 1
        return repaired

    def _subscription_from_row(self, row: dict) -> Subscription:
        lease_until = datetime.fromisoformat(row["lease_until"]) if row.get("lease_until") else None
        now = datetime.now(ZoneInfo(self.settings.timezone))
        return Subscription(
            id=row["id"],
            name=row["name"],
            spec=TenderQuerySpec.model_validate_json(row["spec_json"]),
            enabled=bool(row["enabled"]),
            delivery_channel=row["delivery_channel"],
            delivery_policy=DeliveryPolicy(row.get("delivery_policy", "always")),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row.get("updated_at") else None,
            last_run_at=(
                datetime.fromisoformat(row["last_run_at"]) if row.get("last_run_at") else None
            ),
            next_run_at=(
                datetime.fromisoformat(row["next_run_at"]) if row.get("next_run_at") else None
            ),
            last_status=RunStatus(row["last_status"]) if row.get("last_status") else None,
            last_message=row.get("last_message"),
            last_new_count=int(row.get("last_new_count", 0)),
            consecutive_failures=int(row.get("consecutive_failures", 0)),
            last_run_id=row.get("last_run_id"),
            in_progress=bool(row.get("lease_owner") and lease_until and lease_until > now),
        )

    def get_subscription(self, subscription_id: str) -> Subscription | None:
        row = self.db.get_subscription(subscription_id)
        return self._subscription_from_row(row) if row else None

    def list_subscriptions(self) -> list[dict]:
        return [
            self._subscription_from_row(row).model_dump(mode="json")
            for row in self.db.list_subscriptions()
        ]

    def update_subscription(self, subscription_id: str, update: SubscriptionUpdate) -> Subscription:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        if update.delivery_channel and not self._channel_is_configured(update.delivery_channel):
            raise ValueError(f"投递通道 {update.delivery_channel} 尚未配置")
        spec = None
        next_run_at = None
        if update.query is not None:
            current = self._subscription_from_row(row)
            if current.in_progress:
                raise SubscriptionBusyError("该订阅正在执行，请在本轮完成后再修改规则")
            now = datetime.now(ZoneInfo(self.settings.timezone))
            spec = self.parser.parse(update.query, now=now)
            if spec.schedule.kind == ScheduleKind.IMMEDIATE:
                raise ValueError("订阅规则必须包含每天、每周或明确的未来发送时间")
            spec.delivery_channel = update.delivery_channel or row["delivery_channel"]
            next_run_at = next_schedule_time(spec.schedule, now) if row["enabled"] else None
        self.db.update_subscription(
            subscription_id,
            name=update.name,
            spec=spec,
            next_run_at=next_run_at,
            update_next_run=spec is not None,
            delivery_channel=update.delivery_channel,
            delivery_policy=update.delivery_policy,
        )
        subscription = self.get_subscription(subscription_id)
        assert subscription is not None
        return subscription

    def pause_subscription(self, subscription_id: str) -> Subscription:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        if self._subscription_from_row(row).in_progress:
            raise SubscriptionBusyError("该订阅正在执行，请在本轮完成后再暂停")
        self.db.set_subscription_due(subscription_id, None, enabled=False)
        subscription = self.get_subscription(subscription_id)
        assert subscription is not None
        return subscription

    def resume_subscription(
        self, subscription_id: str, *, run_immediately: bool = False
    ) -> Subscription:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        now = datetime.now(ZoneInfo(self.settings.timezone))
        spec = TenderQuerySpec.model_validate_json(row["spec_json"])
        next_run_at = now if run_immediately else next_schedule_time(spec.schedule, now)
        if next_run_at is None:
            raise ValueError("一次性订阅已经过期，请创建新的发送计划")
        self.db.set_subscription_due(subscription_id, next_run_at, enabled=True)
        subscription = self.get_subscription(subscription_id)
        assert subscription is not None
        return subscription

    def delete_subscription(self, subscription_id: str) -> None:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        if self._subscription_from_row(row).in_progress:
            raise SubscriptionBusyError("该订阅正在执行，请在本轮完成后再删除")
        self.db.delete_subscription(subscription_id)

    def list_subscription_runs(self, subscription_id: str, limit: int = 20) -> list[dict]:
        rows = self.db.list_subscription_runs(subscription_id, limit)
        for row in rows:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["diagnostics"] = json.loads(row.pop("diagnostics_json"))
        return rows

    def list_delivery_attempts(self, subscription_id: str | None = None) -> list[dict]:
        return self.db.list_delivery_attempts(subscription_id=subscription_id)

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

    @staticmethod
    def _opportunity_from_row(row: dict) -> Opportunity:
        return Opportunity(
            id=row["id"],
            project_key=row["project_key"],
            record=TenderRecord.model_validate_json(row["snapshot_json"]),
            stage=OpportunityStage(row["stage"]),
            owner=row.get("owner", ""),
            next_action_at=(
                datetime.fromisoformat(row["next_action_at"]) if row.get("next_action_at") else None
            ),
            notes=row.get("notes", ""),
            tags=json.loads(row.get("tags_json") or "[]"),
            is_read=bool(row.get("is_read")),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _record_recency_key(self, record: TenderRecord) -> tuple[datetime, int]:
        published_at = record.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=ZoneInfo(self.settings.timezone))
        return (
            published_at.astimezone(UTC),
            _EVENT_RECENCY_RANK[record.event_type],
        )

    def _latest_project_record(self, project_key: str) -> TenderRecord:
        records = [
            TenderRecord.model_validate_json(row["payload_json"])
            for row in self.db.list_project_items(project_key)
        ]
        if not records:
            raise KeyError(f"项目不存在：{project_key}")
        _, latest = max(
            enumerate(records),
            key=lambda item: (*self._record_recency_key(item[1]), item[0]),
        )
        return latest

    def _refresh_opportunities_for_projects(self, records: list[TenderRecord]) -> None:
        for project_key in {record.project_key for record in records}:
            if self.db.get_opportunity_by_project_key(project_key) is None:
                continue
            latest = self._latest_project_record(project_key)
            self.db.refresh_opportunity_snapshot(
                project_key=project_key,
                canonical_id=latest.canonical_id,
                version_hash=latest.version_hash,
                snapshot=latest.model_dump(mode="json"),
            )

    def create_opportunity(self, request: OpportunityCreate) -> Opportunity:
        selected = self.db.get_tender_item(request.canonical_id, request.version_hash)
        if selected is None:
            raise KeyError("只能收藏系统已抓取并验证过的标讯记录")
        latest = self._latest_project_record(selected["project_key"])
        row = self.db.upsert_opportunity(
            opportunity_id=uuid4().hex,
            project_key=latest.project_key,
            canonical_id=latest.canonical_id,
            version_hash=latest.version_hash,
            snapshot=latest.model_dump(mode="json"),
        )
        return self._opportunity_from_row(row)

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        row = self.db.get_opportunity(opportunity_id)
        return self._opportunity_from_row(row) if row else None

    def list_opportunities(
        self,
        *,
        stage: OpportunityStage | None = None,
        search: str | None = None,
    ) -> list[dict]:
        opportunities = [
            self._opportunity_from_row(row)
            for row in self.db.list_opportunities(stage.value if stage else None)
        ]
        needle = (search or "").strip().casefold()
        if needle:
            opportunities = [
                item
                for item in opportunities
                if needle
                in " ".join(
                    (
                        item.record.title,
                        item.record.buyer or "",
                        item.record.region or "",
                        item.owner,
                        item.notes,
                        " ".join(item.tags),
                    )
                ).casefold()
            ]
        return [item.model_dump(mode="json") for item in opportunities]

    def update_opportunity(self, opportunity_id: str, update: OpportunityUpdate) -> Opportunity:
        if self.db.get_opportunity(opportunity_id) is None:
            raise KeyError(f"机会不存在：{opportunity_id}")
        changes: dict[str, object] = {}
        fields = update.model_fields_set
        if "stage" in fields and update.stage is not None:
            changes["stage"] = update.stage.value
        if "owner" in fields:
            changes["owner"] = (update.owner or "").strip()
        if "next_action_at" in fields:
            next_action_at = update.next_action_at
            if next_action_at and next_action_at.tzinfo is None:
                next_action_at = next_action_at.replace(tzinfo=ZoneInfo(self.settings.timezone))
            changes["next_action_at"] = next_action_at.isoformat() if next_action_at else None
        if "notes" in fields:
            changes["notes"] = (update.notes or "").strip()
        if "tags" in fields:
            changes["tags_json"] = json.dumps(update.tags or [], ensure_ascii=False)
        if "is_read" in fields:
            changes["is_read"] = int(bool(update.is_read))
        self.db.update_opportunity(opportunity_id, changes)
        result = self.get_opportunity(opportunity_id)
        assert result is not None
        return result

    def opportunity_timeline(self, opportunity_id: str) -> list[dict]:
        opportunity = self.db.get_opportunity(opportunity_id)
        if opportunity is None:
            raise KeyError(f"机会不存在：{opportunity_id}")
        records = [
            TenderRecord.model_validate_json(row["payload_json"])
            for row in self.db.list_project_items(opportunity["project_key"])
        ]
        records.sort(key=self._record_recency_key)
        return [item.model_dump(mode="json") for item in records]

    def system_status(self) -> dict:
        now = datetime.now(UTC)
        workers = []
        for row in self.db.list_workers():
            heartbeat = datetime.fromisoformat(row["heartbeat_at"])
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=UTC)
            row["online"] = (now - heartbeat).total_seconds() <= self.settings.worker_heartbeat_ttl
            workers.append(row)
        subscriptions = self.db.list_subscriptions()
        local_now = datetime.now(ZoneInfo(self.settings.timezone))
        running_count = sum(
            1
            for row in subscriptions
            if row.get("lease_owner")
            and row.get("lease_until")
            and datetime.fromisoformat(row["lease_until"]) > local_now
        )
        due_count = sum(
            1
            for row in subscriptions
            if row["enabled"]
            and row.get("next_run_at")
            and datetime.fromisoformat(row["next_run_at"]) <= local_now
        )
        return {
            "worker_online": any(item["online"] for item in workers),
            "workers": workers,
            "subscription_count": len(subscriptions),
            "enabled_subscription_count": sum(bool(row["enabled"]) for row in subscriptions),
            "running_subscription_count": running_count,
            "due_count": due_count,
            "delivery_channels": self.delivery.channel_status(),
            "timezone": self.settings.timezone,
        }

    def source_status(self) -> list[dict]:
        latest = self.db.latest_source_runs()
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
                "official": isinstance(source, (CCGPSource, GGZYSource, MofcomSource)),
                "last_status": latest.get(source.name, {}).get("status"),
                "last_checked_at": latest.get(source.name, {}).get("started_at"),
                "last_message": latest.get(source.name, {}).get("message"),
                "last_fetched_count": latest.get(source.name, {}).get("fetched_count", 0),
                "last_kept_count": latest.get(source.name, {}).get("kept_count", 0),
            }
            for source in self.sources
        ]
