from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.delivery import (
    DeliveryError,
    DeliveryPermanentError,
    DeliveryReceipt,
    DeliveryRetryAfterError,
)
from bidpilot.intent import IntentParser
from bidpilot.models import (
    EventType,
    EvidenceSpan,
    RawTender,
    RunStatus,
    SourceSearchResult,
    SourceStatus,
    SubscriptionUpdate,
    TenderQuerySpec,
)
from bidpilot.runtime_config import RuntimeConfigUpdate
from bidpilot.service import BidPilotService, RunExecutionError, SubscriptionBusyError
from bidpilot.sources.base import SourceAdapter


class StableTenderSource(SourceAdapter):
    name = "Outbox 测试源"

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        body = "项目编号：OB-2026-001。安徽大学采购 GPU 服务器 20 台。"
        item = RawTender(
            source=self.name,
            source_url="https://example.com/outbox/1",
            title="安徽大学 GPU 服务器采购招标公告",
            published_at=datetime.combine(spec.end_date, time(9, 0)),
            region="安徽",
            buyer="安徽大学",
            body=body,
            event_type=EventType.TENDER,
            project_id="OB-2026-001",
            evidence=[EvidenceSpan(text=body, source_url="https://example.com/outbox/1")],
        )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=[item],
            message="fixture",
            latency_ms=3,
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "bidpilot.db",
        generic_webhook_url="https://automation.example.com/bidpilot",
        request_interval=0.1,
    )


def scheduled_spec() -> TenderQuerySpec:
    return IntentParser().parse(
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        now=datetime(2026, 7, 19, 12, 0),
    )


def test_legacy_subscription_and_ledger_migrate_to_single_target(tmp_path: Path):
    path = tmp_path / "legacy.db"
    database = Database(path)
    spec = scheduled_spec()
    # The old positional contract remains valid: channel is followed by next_run_at.
    database.create_subscription("legacy", "旧订阅", spec, "local", None)
    database.mark_delivered("legacy", [("notice-1", "version-1")], "legacy.docx")

    with database.connection() as conn:
        conn.execute("UPDATE subscriptions SET delivery_targets_json='[]' WHERE id='legacy'")
        conn.execute("DROP TABLE delivery_outbox_items")
        conn.execute("DROP TABLE delivery_outbox")
        conn.execute("DROP TABLE delivery_target_ledger")

    migrated = Database(path)
    row = migrated.get_subscription("legacy")
    assert json.loads(row["delivery_targets_json"]) == ["local"]
    missing = migrated.undelivered_keys_by_target(
        "legacy",
        ["local"],
        [("notice-1", "version-1")],
    )
    assert missing == {"local": set()}


async def test_multitarget_run_persists_report_and_outbox_before_dispatch(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[StableTenderSource()])
    calls: list[tuple[str, Path | None]] = []
    persistence_checks: list[tuple[int, int, bool, str]] = []

    async def deliver(path, channel, **kwargs):
        calls.append((channel, path))
        with service.db.connection() as conn:
            report_count = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            outbox_count = conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
            run_status = conn.execute(
                "SELECT status FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
        persistence_checks.append(
            (report_count, outbox_count, bool(path and path.exists()), run_status)
        )
        return DeliveryReceipt(channel, True, f"{channel} 已确认", external_id=f"id-{channel}")

    monkeypatch.setattr(service.delivery, "deliver", deliver)
    result = await service.run_query(
        "最近1个月安徽服务器招标信息",
        delivery_targets=["local", "generic_webhook"],
    )

    assert result.delivery_targets == ["local", "generic_webhook"]
    assert [receipt.status for receipt in result.delivery_receipts] == [
        "succeeded",
        "succeeded",
    ]
    assert all(receipt.outbox_id for receipt in result.delivery_receipts)
    assert Counter(channel for channel, _ in calls) == {
        "local": 1,
        "generic_webhook": 1,
    }
    assert all(check == (1, 2, True, "partial") for check in persistence_checks)
    outbox = service.db.list_delivery_outbox(run_id=result.run_id)
    assert {row["status"] for row in outbox} == {"succeeded"}
    assert {row["item_count"] for row in outbox} == {1}


async def test_retry_sends_only_failed_target_and_commits_ledgers_atomically(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[StableTenderSource()])
    subscription = service.create_subscription(
        "双渠道日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        delivery_targets=["local", "generic_webhook"],
    )
    calls: Counter[str] = Counter()

    async def deliver(path, channel, **kwargs):
        calls[channel] += 1
        if channel == "generic_webhook" and calls[channel] == 1:
            raise DeliveryError("模拟目标暂时不可用")
        return DeliveryReceipt(channel, True, f"{channel} 已确认")

    monkeypatch.setattr(service.delivery, "deliver", deliver)
    monkeypatch.setattr("bidpilot.service.retry_time", lambda after, failures: after)

    first = await service.run_subscription(subscription.id)
    assert first.status == RunStatus.PARTIAL
    assert calls == {"local": 1, "generic_webhook": 1}
    assert await service.process_due_delivery_outbox(worker_id="restart-worker") is True
    assert calls == {"local": 1, "generic_webhook": 2}

    rows = service.db.list_delivery_outbox(run_id=first.run_id)
    assert {row["status"] for row in rows} == {"succeeded"}
    assert service.get_run(first.run_id).status == RunStatus.COMPLETED
    with service.db.connection() as conn:
        target_rows = conn.execute(
            """
            SELECT channel, COUNT(*) AS count FROM delivery_target_ledger
            WHERE subscription_id=? GROUP BY channel
            """,
            (subscription.id,),
        ).fetchall()
        legacy_count = conn.execute(
            "SELECT COUNT(*) FROM delivery_ledger WHERE subscription_id=?",
            (subscription.id,),
        ).fetchone()[0]
    assert {row["channel"]: row["count"] for row in target_rows} == {
        "local": 1,
        "generic_webhook": 1,
    }
    assert legacy_count == 1


async def test_later_run_does_not_duplicate_items_reserved_by_retrying_outbox(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[StableTenderSource()])
    subscription = service.create_subscription(
        "双渠道日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        delivery_targets=["local", "generic_webhook"],
    )
    calls: Counter[str] = Counter()

    async def deliver(path, channel, **kwargs):
        calls[channel] += 1
        if channel == "generic_webhook":
            raise DeliveryError("持续不可用")
        return DeliveryReceipt(channel, True, "已确认")

    monkeypatch.setattr(service.delivery, "deliver", deliver)
    first = await service.run_subscription(subscription.id)
    second = await service.run_subscription(subscription.id)

    assert first.new_count == 1
    assert second.new_count == 0
    assert calls == {"local": 2, "generic_webhook": 1}
    second_rows = service.db.list_delivery_outbox(run_id=second.run_id)
    by_channel = {row["channel"]: row for row in second_rows}
    assert by_channel["generic_webhook"]["status"] == "skipped"
    assert "不重复排队" in by_channel["generic_webhook"]["last_message"]


async def test_removing_target_cancels_its_queued_retry_without_resending(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[StableTenderSource()])
    subscription = service.create_subscription(
        "可编辑双渠道日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        delivery_targets=["local", "generic_webhook"],
    )
    calls: Counter[str] = Counter()

    async def deliver(path, channel, **kwargs):
        calls[channel] += 1
        if channel == "generic_webhook":
            raise DeliveryError("等待用户移除")
        return DeliveryReceipt(channel, True, "已确认")

    monkeypatch.setattr(service.delivery, "deliver", deliver)
    run = await service.run_subscription(subscription.id)
    assert run.status == RunStatus.PARTIAL

    updated = service.update_subscription(
        subscription.id,
        SubscriptionUpdate(delivery_targets=["local"]),
    )
    assert updated.delivery_targets == ["local"]
    outbox = {row["channel"]: row for row in service.db.list_delivery_outbox(run_id=run.run_id)}
    assert outbox["generic_webhook"]["status"] == "skipped"
    assert "已取消" in outbox["generic_webhook"]["last_message"]
    assert service.get_run(run.run_id).status == RunStatus.COMPLETED
    assert await service.process_due_delivery_outbox(worker_id="worker") is False
    assert calls == {"local": 1, "generic_webhook": 1}


def test_target_cannot_be_removed_while_its_external_send_is_in_flight(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    subscription = service.create_subscription(
        "发送中保护",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        delivery_targets=["local", "generic_webhook"],
    )
    service.db.create_run("sending-run", subscription.spec, subscription_id=subscription.id)
    row = service.db.create_delivery_outbox_item(
        run_id="sending-run",
        subscription_id=subscription.id,
        channel="generic_webhook",
        report_path=None,
        new_count=0,
        subscription_name=subscription.name,
    )
    now = datetime.now(UTC)
    claimed = service.db.claim_new_delivery_outbox(
        row["id"],
        worker_id="active-worker",
        now=now,
        lease_until=now + timedelta(minutes=1),
    )
    assert claimed is not None

    try:
        service.update_subscription(
            subscription.id,
            SubscriptionUpdate(delivery_targets=["local"]),
        )
    except SubscriptionBusyError as exc:
        assert "刷新后重试" in str(exc)
    else:
        raise AssertionError("Expected an in-flight destination to block removal")

    assert service.get_subscription(subscription.id).delivery_targets == [
        "local",
        "generic_webhook",
    ]


def test_expired_lease_can_be_taken_over_and_stale_token_cannot_finish(tmp_path: Path):
    database = Database(tmp_path / "lease.db")
    spec = scheduled_spec()
    database.create_subscription("sub", "租约测试", spec, "local", None)
    database.create_run("run", spec, subscription_id="sub")
    row = database.create_delivery_outbox_item(
        run_id="run",
        subscription_id="sub",
        channel="local",
        report_path="report.docx",
        new_count=1,
        subscription_name="租约测试",
        keys=[("notice-1", "version-1")],
    )
    now = datetime.now(UTC)
    first = database.claim_new_delivery_outbox(
        row["id"],
        worker_id="worker-a",
        now=now,
        lease_until=now + timedelta(seconds=1),
    )
    second = database.claim_due_delivery_outbox(
        worker_id="worker-b",
        now=now + timedelta(seconds=2),
        lease_until=now + timedelta(seconds=30),
        outbox_id=row["id"],
    )

    assert first["lease_token"] != second["lease_token"]
    assert (
        database.finish_delivery_outbox(
            row["id"],
            lease_token=first["lease_token"],
            status="succeeded",
            message="旧 worker 回写",
        )
        is False
    )
    assert database.finish_delivery_outbox(
        row["id"],
        lease_token=second["lease_token"],
        status="succeeded",
        message="新 worker 回写",
    )
    assert database.get_delivery_outbox(row["id"])["status"] == "succeeded"
    assert database.undelivered_keys_by_target("sub", ["local"], [("notice-1", "version-1")]) == {
        "local": set()
    }


async def test_fifth_failure_enters_dead_letter_and_manual_retry_recovers(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    spec = scheduled_spec()
    service.db.create_run("dead-run", spec)
    row = service.db.create_delivery_outbox_item(
        run_id="dead-run",
        subscription_id=None,
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="死信测试",
        max_attempts=5,
    )

    async def fail(*args, **kwargs):
        raise DeliveryError("持续失败")

    monkeypatch.setattr(service.delivery, "deliver", fail)
    monkeypatch.setattr("bidpilot.service.retry_time", lambda after, failures: after)
    receipt = await service.dispatch_delivery_outbox(row["id"], worker_id="worker", claim_new=True)
    for _ in range(4):
        assert await service.process_due_delivery_outbox(worker_id="worker") is True
    assert receipt.status == "retrying"
    assert service.db.get_delivery_outbox(row["id"])["status"] == "dead_letter"

    assert service.db.retry_delivery_outbox(row["id"], now=datetime.now(UTC)) is True

    async def succeed(path, channel, **kwargs):
        return DeliveryReceipt(channel, True, "恢复成功")

    monkeypatch.setattr(service.delivery, "deliver", succeed)
    assert await service.process_due_delivery_outbox(worker_id="worker-restarted") is True
    recovered = service.db.get_delivery_outbox(row["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["attempt_count"] == 1


async def test_cancelled_outbox_dispatch_requeues_for_immediate_takeover(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    spec = scheduled_spec()
    service.db.create_run("cancelled-outbox-run", spec)
    row = service.db.create_delivery_outbox_item(
        run_id="cancelled-outbox-run",
        subscription_id=None,
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="取消恢复测试",
    )
    started = asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked delivery should only finish through cancellation")

    monkeypatch.setattr(service.delivery, "deliver", block)
    task = asyncio.create_task(
        service.dispatch_delivery_outbox(row["id"], worker_id="stopping-worker", claim_new=True)
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    sending = service.db.get_delivery_outbox(row["id"])
    assert sending is not None
    assert sending["status"] == "sending"
    assert sending["lease_owner"] == "stopping-worker"

    cancelled_at = datetime.now(UTC)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    queued = service.db.get_delivery_outbox(row["id"])
    assert queued is not None
    assert queued["status"] == "retrying"
    assert queued["lease_owner"] is None
    assert queued["lease_token"] is None
    assert queued["lease_until"] is None
    assert datetime.fromisoformat(queued["next_attempt_at"]) >= cancelled_at
    assert datetime.fromisoformat(queued["next_attempt_at"]) <= datetime.now(UTC)

    async def succeed(path, channel, **kwargs):
        return DeliveryReceipt(channel, True, "新 worker 接管成功")

    monkeypatch.setattr(service.delivery, "deliver", succeed)
    assert await service.process_due_delivery_outbox(worker_id="replacement-worker") is True
    recovered = service.db.get_delivery_outbox(row["id"])
    assert recovered is not None
    assert recovered["status"] == "succeeded"


async def test_cancelled_outbox_dispatch_cannot_release_replacement_lease(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    spec = scheduled_spec()
    service.db.create_run("fenced-cancelled-outbox-run", spec)
    row = service.db.create_delivery_outbox_item(
        run_id="fenced-cancelled-outbox-run",
        subscription_id=None,
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="取消栅栏测试",
    )
    started = asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked delivery should only finish through cancellation")

    monkeypatch.setattr(service.delivery, "deliver", block)
    task = asyncio.create_task(
        service.dispatch_delivery_outbox(row["id"], worker_id="old-worker", claim_new=True)
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    replacement_until = datetime.now(UTC) + timedelta(minutes=5)
    with service.db.connection() as conn:
        conn.execute(
            """
            UPDATE delivery_outbox SET lease_owner=?, lease_token=?, lease_until=?
            WHERE id=? AND status='sending'
            """,
            ("replacement-worker", "replacement-token", replacement_until.isoformat(), row["id"]),
        )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    fenced = service.db.get_delivery_outbox(row["id"])
    assert fenced is not None
    assert fenced["status"] == "sending"
    assert fenced["lease_owner"] == "replacement-worker"
    assert fenced["lease_token"] == "replacement-token"
    assert fenced["lease_until"] == replacement_until.isoformat()


@pytest.mark.parametrize(
    ("source_status", "expected_status"),
    [
        (SourceStatus.OK, RunStatus.COMPLETED),
        (SourceStatus.PARTIAL, RunStatus.PARTIAL),
    ],
)
async def test_dead_letter_recovery_preserves_retrieval_state_and_diagnostics(
    tmp_path: Path,
    monkeypatch,
    source_status: SourceStatus,
    expected_status: RunStatus,
):
    class RetrievalStatusSource(StableTenderSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            result = await super().search(spec, fetcher)
            return result.model_copy(update={"status": source_status})

    service = BidPilotService(make_settings(tmp_path), sources=[RetrievalStatusSource()])
    subscription = service.create_subscription(
        "检索与投递分离日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        delivery_targets=["local"],
    )

    async def reject(*args, **kwargs):
        raise DeliveryPermanentError("模拟永久投递错误")

    monkeypatch.setattr(service.delivery, "deliver", reject)
    first = await service.run_subscription(subscription.id)
    outbox = service.db.list_delivery_outbox(run_id=first.run_id)
    assert len(outbox) == 1
    assert outbox[0]["status"] == "dead_letter"
    stored_before = service.db.get_run(first.run_id)
    assert stored_before is not None
    diagnostics_before = stored_before["diagnostics_json"]
    assert json.loads(diagnostics_before)[0]["status"] == source_status.value
    subscription_before = service.get_subscription(subscription.id)
    assert subscription_before is not None
    assert subscription_before.last_status == expected_status
    assert subscription_before.last_delivery_status == "partial"

    service.retry_dead_letter(outbox[0]["id"])

    async def succeed(path, channel, **kwargs):
        return DeliveryReceipt(channel, True, "恢复成功")

    monkeypatch.setattr(service.delivery, "deliver", succeed)
    assert await service.process_due_delivery_outbox(worker_id="recovery-worker") is True

    stored_after = service.db.get_run(first.run_id)
    assert stored_after is not None
    assert stored_after["status"] == expected_status.value
    assert stored_after["diagnostics_json"] == diagnostics_before
    subscription_after = service.get_subscription(subscription.id)
    assert subscription_after is not None
    assert subscription_after.last_status == expected_status
    assert subscription_after.last_delivery_status == "success"


async def test_missing_staged_report_goes_directly_to_dead_letter(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    spec = scheduled_spec()
    service.db.create_run("missing-report-run", spec)
    row = service.db.create_delivery_outbox_item(
        run_id="missing-report-run",
        subscription_id=None,
        channel="local",
        report_path=str(tmp_path / "deleted.docx"),
        new_count=1,
        subscription_name="报告缺失",
    )

    receipt = await service.dispatch_delivery_outbox(row["id"], worker_id="worker", claim_new=True)
    assert receipt.status == "dead_letter"
    assert receipt.attempt_count == 1
    assert "已不存在" in receipt.message


async def test_channel_retry_after_and_permanent_failures_control_outbox_state(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    spec = scheduled_spec()
    service.db.create_run("classified-run", spec)
    retrying = service.db.create_delivery_outbox_item(
        run_id="classified-run",
        subscription_id=None,
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="分类错误",
    )

    async def limited(*args, **kwargs):
        raise DeliveryRetryAfterError("平台限流", 180)

    monkeypatch.setattr(service.delivery, "deliver", limited)
    before = datetime.now(UTC)
    receipt = await service.dispatch_delivery_outbox(
        retrying["id"], worker_id="worker", claim_new=True
    )
    assert receipt.status == "retrying"
    assert receipt.next_attempt_at >= before + timedelta(seconds=175)

    service.db.create_run("permanent-run", spec)
    permanent = service.db.create_delivery_outbox_item(
        run_id="permanent-run",
        subscription_id=None,
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="永久错误",
    )

    async def rejected(*args, **kwargs):
        raise DeliveryPermanentError("配置已失效")

    monkeypatch.setattr(service.delivery, "deliver", rejected)
    dead = await service.dispatch_delivery_outbox(
        permanent["id"], worker_id="worker", claim_new=True
    )
    assert dead.status == "dead_letter"
    assert dead.attempt_count == 1


async def test_retry_after_is_measured_from_platform_failure_time(tmp_path: Path, monkeypatch):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    spec = scheduled_spec()
    service.db.create_run("slow-limited-run", spec)
    row = service.db.create_delivery_outbox_item(
        run_id="slow-limited-run",
        subscription_id=None,
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="慢请求限流",
    )

    async def slow_limited(*args, **kwargs):
        await asyncio.sleep(0.3)
        raise DeliveryRetryAfterError("平台要求等待", 1)

    monkeypatch.setattr(service.delivery, "deliver", slow_limited)
    receipt = await service.dispatch_delivery_outbox(row["id"], worker_id="worker", claim_new=True)
    returned_at = datetime.now(UTC)

    assert receipt.status == "retrying"
    assert receipt.next_attempt_at >= returned_at + timedelta(seconds=0.85)


async def test_outbox_worker_reloads_web_managed_channel_secrets(tmp_path: Path, monkeypatch):
    web_service = BidPilotService(make_settings(tmp_path), sources=[])
    worker_service = BidPilotService(make_settings(tmp_path), sources=[])
    slack_url = "https://hooks.slack.com/services/T000/B000/WORKERSECRET"
    assert worker_service.settings.slack_webhook_url == ""

    web_service.runtime_config.update(
        RuntimeConfigUpdate(
            revision=web_service.runtime_config.snapshot().revision,
            slack_webhook_url=slack_url,
        )
    )
    spec = scheduled_spec()
    worker_service.db.create_run("runtime-config-outbox", spec)
    row = worker_service.db.create_delivery_outbox_item(
        run_id="runtime-config-outbox",
        subscription_id=None,
        channel="slack_webhook",
        report_path=None,
        new_count=0,
        subscription_name="配置热更新",
    )

    async def succeed(path, channel, **kwargs):
        assert worker_service.settings.slack_webhook_url == slack_url
        return DeliveryReceipt(channel, True, "使用网页新配置完成")

    monkeypatch.setattr(worker_service.delivery, "deliver", succeed)
    receipt = await worker_service.dispatch_delivery_outbox(
        row["id"], worker_id="standalone-worker", claim_new=True
    )

    assert receipt.status == "succeeded"
    assert worker_service.settings.slack_webhook_url == slack_url


async def test_new_service_processes_persisted_pending_outbox(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    first = BidPilotService(settings, sources=[])
    spec = scheduled_spec()
    first.db.create_run("restart-run", spec)
    row = first.db.create_delivery_outbox_item(
        run_id="restart-run",
        subscription_id=None,
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="重启恢复",
    )

    restarted = BidPilotService(make_settings(tmp_path), sources=[])

    async def succeed(path, channel, **kwargs):
        return DeliveryReceipt(channel, True, "重启后完成")

    monkeypatch.setattr(restarted.delivery, "deliver", succeed)
    assert await restarted.process_due_delivery_outbox(worker_id="new-process") is True
    assert restarted.db.get_delivery_outbox(row["id"])["status"] == "succeeded"


async def test_subscription_records_actionable_failure_when_channel_was_cleared(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[StableTenderSource()])
    subscription = service.create_subscription(
        "外部渠道日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        delivery_targets=["generic_webhook"],
    )
    service.settings.generic_webhook_url = ""

    try:
        await service.run_subscription(subscription.id)
    except RunExecutionError as exc:
        assert "尚未配置" in str(exc)
    else:
        raise AssertionError("Expected the cleared channel to fail before retrieval")

    row = service.db.get_subscription(subscription.id)
    assert row["last_status"] == "failed"
    assert row["consecutive_failures"] == 1
    assert row["lease_owner"] is None
    run = service.db.get_run(row["last_run_id"])
    assert run["status"] == "failed"
    assert "尚未配置" in run["error"]


def test_dead_letter_list_and_retry_api(tmp_path: Path):
    app = create_app(make_settings(tmp_path), sources=[])
    service = app.state.service
    spec = scheduled_spec()
    service.db.create_subscription("api-sub", "API 死信", spec, "local", None)
    service.db.create_run("api-run", spec, subscription_id="api-sub")
    row = service.db.create_delivery_outbox_item(
        run_id="api-run",
        subscription_id="api-sub",
        channel="local",
        report_path=None,
        new_count=0,
        subscription_name="API 死信",
    )
    with service.db.connection() as conn:
        conn.execute(
            """
            UPDATE delivery_outbox SET status='dead_letter', attempt_count=5,
              next_attempt_at=NULL, last_error='测试死信', last_message='测试死信'
            WHERE id=?
            """,
            (row["id"],),
        )

    with TestClient(app) as client:
        listed = client.get("/api/v1/subscriptions/api-sub/delivery-outbox")
        assert listed.status_code == 200
        assert listed.json()[0]["status"] == "dead_letter"
        assert "lease_token" not in listed.json()[0]
        retried = client.post(f"/api/v1/delivery-outbox/{row['id']}/retry")
        assert retried.status_code == 200
        assert retried.json()["status"] == "retrying"
        assert client.post(f"/api/v1/delivery-outbox/{row['id']}/retry").status_code == 409
        assert client.post("/api/v1/delivery-outbox/missing/retry").status_code == 404
