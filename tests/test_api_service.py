import asyncio
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from docx import Document
from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.models import (
    EventType,
    EvidenceSpan,
    OpportunityCreate,
    OpportunityStage,
    OpportunityUpdate,
    RawTender,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
)
from bidpilot.runtime_config import RuntimeConfigUpdate
from bidpilot.scheduler import SubscriptionWorker, next_schedule_time
from bidpilot.service import BidPilotService, RunExecutionError, SubscriptionBusyError
from bidpilot.sources.base import SourceAdapter


class FakeSource(SourceAdapter):
    name = "可验证测试源"

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        body = "项目编号：AH-2026-001。预算金额：1200万元。采购20台GPU服务器。"
        item = RawTender(
            source=self.name,
            source_url="https://example.com/tender/1",
            title="安徽大学 GPU 服务器采购公开招标公告",
            published_at=datetime(2026, 7, 10, 9, 0),
            region="安徽",
            buyer="安徽大学",
            body=body,
            event_type=EventType.TENDER,
            project_id="AH-2026-001",
            evidence=[EvidenceSpan(text=body, source_url="https://example.com/tender/1")],
        )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=[item],
            message="fixture",
            latency_ms=5,
        )


class SequencedLifecycleSource(SourceAdapter):
    name = "生命周期测试源"

    def __init__(self):
        self.calls = 0

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        self.calls += 1
        changed = self.calls > 1
        body = "项目编号：AH-2026-009。采购 GPU 服务器，资格条件以招标文件为准。"
        item = RawTender(
            source=self.name,
            source_url=(
                "https://example.com/tender/9-change" if changed else "https://example.com/tender/9"
            ),
            title=(
                "安徽大学 GPU 服务器采购更正公告" if changed else "安徽大学 GPU 服务器采购招标公告"
            ),
            published_at=datetime(2026, 7, 10, 9, 0),
            region="安徽",
            buyer="安徽大学",
            body=body,
            event_type=EventType.CHANGE if changed else EventType.TENDER,
            project_id="AH-2026-009",
            evidence=[
                EvidenceSpan(
                    text=body,
                    source_url=(
                        "https://example.com/tender/9-change"
                        if changed
                        else "https://example.com/tender/9"
                    ),
                )
            ],
        )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=[item],
            message="fixture",
            latency_ms=5,
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        request_interval=0.1,
    )


def test_api_query_to_docx_flow(tmp_path: Path):
    app = create_app(make_settings(tmp_path), sources=[FakeSource()])
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        parsed = client.post("/api/v1/intent/parse", json={"query": "最近1个月安徽服务器招标信息"})
        assert parsed.status_code == 200
        assert parsed.json()["topic"] == "服务器"
        assert parsed.json()["resolution"]["llm_status"] == "not_needed"
        compared = client.post(
            "/api/v1/intent/compare",
            json={"query": "最近1个月安徽服务器招标信息"},
        )
        assert compared.status_code == 200
        assert compared.json()["changed_fields"] == []

        response = client.post(
            "/api/v1/runs",
            json={"query": "最近1个月安徽服务器招标信息", "delivery_channel": "local"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["new_count"] == 1
        assert result["records"][0]["project_id"] == "AH-2026-001"
        assert result["intelligence_brief"]["status"] == "not_configured"
        assert result["intelligence_brief"]["priorities"][0]["evidence_id"] == "E01"
        report_name = Path(result["report_path"]).name
        download = client.get(f"/api/v1/reports/{report_name}")
        assert download.status_code == 200
        assert download.content.startswith(b"PK")
        report = Document(BytesIO(download.content))
        assert "情报副驾驶" in "\n".join(item.text for item in report.paragraphs)
        assert client.get("/api/v1/reports").json()[0]["item_count"] == 1


async def test_subscription_second_run_is_zero_increment(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "每日服务器简报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
    )
    first = await service.run_subscription(subscription.id)
    second = await service.run_subscription(subscription.id)
    assert first.new_count == 1
    assert first.report_path is not None
    assert second.new_count == 0
    assert second.report_path is None


def test_subscription_creation_is_idempotent(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    first = service.create_subscription("每日服务器简报", query)
    second = service.create_subscription("重复点击不应新建", query)
    assert first.id == second.id
    assert len(service.list_subscriptions()) == 1


async def test_recreating_existing_subscription_requeues_a_fresh_run(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    first = service.create_subscription("每日服务器简报", query)
    await service.run_subscription(first.id)
    completed = service.get_subscription(first.id)
    assert completed.next_run_at > datetime.now(ZoneInfo("Asia/Shanghai"))

    existing = service.create_subscription("重复点击", query, run_immediately=True)
    assert existing.id == first.id
    assert existing.enabled is True
    assert existing.next_run_at <= datetime.now(ZoneInfo("Asia/Shanghai"))


def test_subscription_requires_schedule(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    try:
        service.create_subscription("无效", "最近1个月安徽服务器招标信息")
    except ValueError as exc:
        assert "必须包含" in str(exc)
    else:
        raise AssertionError("Expected schedule validation")


def test_schedule_calculation_is_strictly_future():
    timezone = ZoneInfo("Asia/Shanghai")
    from bidpilot.intent import IntentParser

    daily = IntentParser().parse(
        "最近1个月安徽服务器招标信息，每天9:00发送",
        now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone),
    )
    assert next_schedule_time(
        daily.schedule, datetime(2026, 7, 17, 10, 0, tzinfo=timezone)
    ) == datetime(2026, 7, 18, 9, 0, tzinfo=timezone)

    weekly = IntentParser().parse(
        "最近1个月安徽服务器招标信息，每周一8:30发送",
        now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone),
    )
    next_week = next_schedule_time(weekly.schedule, datetime(2026, 7, 20, 8, 30, tzinfo=timezone))
    assert next_week == datetime(2026, 7, 27, 8, 30, tzinfo=timezone)

    monthly = IntentParser().parse(
        "最近1个月安徽服务器招标信息，每月31日8:30发送",
        now=datetime(2026, 2, 15, 10, 0, tzinfo=timezone),
    )
    assert next_schedule_time(
        monthly.schedule, datetime(2026, 2, 15, 10, 0, tzinfo=timezone)
    ) == datetime(2026, 2, 28, 8, 30, tzinfo=timezone)
    assert next_schedule_time(
        monthly.schedule, datetime(2026, 2, 28, 8, 30, tzinfo=timezone)
    ) == datetime(2026, 3, 31, 8, 30, tzinfo=timezone)


def test_subscription_management_api(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.embedded_worker = False
    app = create_app(settings, sources=[FakeSource()])
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/subscriptions",
            json={
                "name": "服务器日报",
                "query": query,
                "delivery_channel": "local",
                "delivery_policy": "always",
                "run_immediately": False,
            },
        )
        assert created.status_code == 200
        subscription = created.json()
        subscription_id = subscription["id"]
        assert subscription["next_run_at"] is not None

        paused = client.post(f"/api/v1/subscriptions/{subscription_id}/pause")
        assert paused.json()["enabled"] is False
        assert paused.json()["next_run_at"] is None

        resumed = client.post(
            f"/api/v1/subscriptions/{subscription_id}/resume",
            json={"run_immediately": False},
        )
        assert resumed.json()["enabled"] is True
        assert resumed.json()["next_run_at"] is not None

        updated = client.patch(
            f"/api/v1/subscriptions/{subscription_id}",
            json={
                "name": "重点服务器周报",
                "query": "近2周安徽服务器招标信息，每周一8:30发送",
                "delivery_policy": "on_change",
            },
        )
        updated_row = updated.json()
        assert updated_row["name"] == "重点服务器周报"
        assert updated_row["delivery_policy"] == "on_change"
        assert updated_row["spec"]["schedule"]["kind"] == "weekly"
        assert updated_row["spec"]["schedule"]["send_time"] == "08:30:00"
        assert updated_row["next_run_at"] is not None

        deleted = client.delete(f"/api/v1/subscriptions/{subscription_id}")
        assert deleted.json() == {"deleted": True}
        assert client.get(f"/api/v1/subscriptions/{subscription_id}").status_code == 404


async def test_durable_worker_continues_after_service_restart(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.embedded_worker = False
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    first_service = BidPilotService(settings, sources=[FakeSource()])
    subscription = first_service.create_subscription("服务器日报", query, run_immediately=True)
    first_worker = SubscriptionWorker(first_service, kind="test")
    assert await first_worker.run_once() is True
    first_row = first_service.db.get_subscription(subscription.id)
    assert first_row["last_status"] == "completed"
    assert first_row["last_new_count"] == 1
    assert datetime.fromisoformat(first_row["next_run_at"]) > datetime.now(
        ZoneInfo("Asia/Shanghai")
    )

    # A new process/service instance reads the same durable state and can continue.
    due = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(seconds=1)
    first_service.db.set_subscription_due(subscription.id, due)
    restarted_service = BidPilotService(settings, sources=[FakeSource()])
    restarted_worker = SubscriptionWorker(restarted_service, kind="test-restart")
    assert await restarted_worker.run_once() is True
    second_row = restarted_service.db.get_subscription(subscription.id)
    assert second_row["last_status"] == "completed"
    assert second_row["last_new_count"] == 0
    attempts = restarted_service.db.list_delivery_attempts(subscription_id=subscription.id)
    assert len(attempts) == 2
    assert all(attempt["success"] for attempt in attempts)


async def test_delivery_failure_keeps_increment_uncommitted_and_schedules_retry(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )

    async def fail_delivery(*args, **kwargs):
        raise RuntimeError("模拟投递服务不可用")

    monkeypatch.setattr(service.delivery, "deliver", fail_delivery)
    before = datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        await service.run_subscription(subscription.id, trigger_reason="schedule")
    except RunExecutionError as exc:
        assert "模拟投递服务不可用" in str(exc)
    else:
        raise AssertionError("Expected delivery failure")

    row = service.db.get_subscription(subscription.id)
    assert row["last_status"] == "failed"
    assert row["consecutive_failures"] == 1
    retry_at = datetime.fromisoformat(row["next_run_at"])
    assert timedelta(seconds=45) <= retry_at - before <= timedelta(seconds=90)
    with service.db.connection() as conn:
        delivered = conn.execute(
            "SELECT COUNT(*) FROM delivery_ledger WHERE subscription_id=?",
            (subscription.id,),
        ).fetchone()[0]
        reports = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE subscription_id=?",
            (subscription.id,),
        ).fetchone()[0]
    assert delivered == 0
    assert reports == 0
    attempts = service.db.list_delivery_attempts(subscription_id=subscription.id)
    assert len(attempts) == 1
    assert attempts[0]["success"] == 0


def test_subscription_lease_blocks_duplicate_claim_and_recovers_after_expiry(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    first = service.db.claim_due_subscription(
        worker_id="worker-a",
        now=now,
        lease_until=now + timedelta(minutes=5),
    )
    assert first and first["id"] == subscription.id
    assert (
        service.db.claim_due_subscription(
            worker_id="worker-b",
            now=now + timedelta(minutes=1),
            lease_until=now + timedelta(minutes=6),
        )
        is None
    )
    recovered = service.db.claim_due_subscription(
        worker_id="worker-b",
        now=now + timedelta(minutes=5, seconds=1),
        lease_until=now + timedelta(minutes=10),
    )
    assert recovered and recovered["id"] == subscription.id


async def test_manual_run_is_rejected_while_worker_owns_subscription(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    claimed = service.db.claim_due_subscription(
        worker_id="worker-a",
        now=now,
        lease_until=now + timedelta(minutes=5),
    )
    assert claimed is not None
    assert service.get_subscription(subscription.id).in_progress is True
    try:
        await service.run_subscription(subscription.id)
    except SubscriptionBusyError as exc:
        assert "正在执行" in str(exc)
    else:
        raise AssertionError("Expected an active lease to block a duplicate manual run")
    for action in (service.pause_subscription, service.delete_subscription):
        try:
            action(subscription.id)
        except SubscriptionBusyError as exc:
            assert "正在执行" in str(exc)
        else:
            raise AssertionError("Expected an active lease to block destructive management")


async def test_worker_renews_lease_during_long_running_subscription(tmp_path: Path):
    started = asyncio.Event()

    class SlowSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.sleep(0.65)
            return await super().search(spec, fetcher)

    settings = make_settings(tmp_path)
    # Production validation enforces >=30s. A short interval keeps this concurrency test fast.
    settings.worker_lease_seconds = 0.3
    service = BidPilotService(settings, sources=[SlowSource()])
    service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    worker = SubscriptionWorker(service, kind="lease-test")
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), timeout=3)
    await asyncio.sleep(0.42)

    other_service = BidPilotService(settings, sources=[FakeSource()])
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    duplicate = other_service.db.claim_due_subscription(
        worker_id="competing-worker",
        now=now,
        lease_until=now + timedelta(seconds=10),
    )
    assert duplicate is None
    assert await task is True


async def test_manual_run_renews_lease_during_long_running_subscription(tmp_path: Path):
    started = asyncio.Event()

    class SlowSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.sleep(0.65)
            return await super().search(spec, fetcher)

    settings = make_settings(tmp_path)
    settings.worker_lease_seconds = 0.3
    service = BidPilotService(settings, sources=[SlowSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    task = asyncio.create_task(service.run_subscription(subscription.id))
    await asyncio.wait_for(started.wait(), timeout=3)
    await asyncio.sleep(0.42)

    other_service = BidPilotService(settings, sources=[FakeSource()])
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    duplicate = other_service.db.claim_due_subscription(
        worker_id="competing-worker",
        now=now,
        lease_until=now + timedelta(seconds=10),
    )
    assert duplicate is None
    result = await task
    assert result.new_count == 1


async def test_opportunity_workspace_is_project_idempotent_and_persistent(tmp_path: Path):
    settings = make_settings(tmp_path)
    service = BidPilotService(settings, sources=[FakeSource()])
    run = await service.run_query("最近1个月安徽服务器招标信息")
    tender = run.records[0]
    award = tender.model_copy(
        update={
            "canonical_id": f"{tender.canonical_id}-award",
            "version_hash": "award-version",
            "title": "安徽大学 GPU 服务器采购中标公告",
            "event_type": EventType.AWARD,
            "published_at": tender.published_at + timedelta(days=1),
            "source_urls": ["https://example.com/tender/award"],
        }
    )
    service.db.upsert_records([award.model_dump(mode="json")])

    opportunity = service.create_opportunity(
        OpportunityCreate(
            canonical_id=tender.canonical_id,
            version_hash=tender.version_hash,
        )
    )
    assert opportunity.record.event_type == EventType.AWARD
    duplicate = service.create_opportunity(
        OpportunityCreate(
            canonical_id=award.canonical_id,
            version_hash=award.version_hash,
        )
    )
    assert duplicate.id == opportunity.id
    assert len(service.list_opportunities()) == 1

    next_action = datetime(2026, 7, 20, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    updated = service.update_opportunity(
        opportunity.id,
        OpportunityUpdate(
            stage=OpportunityStage.FOLLOWING,
            owner="王同学",
            next_action_at=next_action,
            notes="联系采购人并核验资质要求",
            tags=["重点", "服务器", "重点"],
            is_read=True,
        ),
    )
    assert updated.stage == OpportunityStage.FOLLOWING
    assert updated.owner == "王同学"
    assert updated.tags == ["重点", "服务器"]
    assert updated.next_action_at == next_action
    assert service.list_opportunities(search="采购人")[0]["id"] == opportunity.id

    timeline = service.opportunity_timeline(opportunity.id)
    assert [item["event_type"] for item in timeline] == ["招标公告", "中标公告"]
    assert timeline[1]["source_urls"] == ["https://example.com/tender/award"]

    restarted = BidPilotService(settings, sources=[FakeSource()])
    persisted = restarted.get_opportunity(opportunity.id)
    assert persisted is not None
    assert persisted.owner == "王同学"
    assert persisted.notes == "联系采购人并核验资质要求"

    app = create_app(settings, sources=[FakeSource()])
    with TestClient(app) as client:
        invalid = client.patch(
            f"/api/v1/opportunities/{opportunity.id}",
            json={"stage": "not-a-stage"},
        )
        assert invalid.status_code == 422
        missing = client.post(
            "/api/v1/opportunities",
            json={"canonical_id": "forged", "version_hash": "forged"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "只能收藏系统已抓取并验证过的标讯记录"


async def test_new_lifecycle_event_refreshes_opportunity_without_losing_follow_up(tmp_path: Path):
    source = SequencedLifecycleSource()
    service = BidPilotService(make_settings(tmp_path), sources=[source])
    first_run = await service.run_query("最近1个月安徽服务器招标信息")
    tender = first_run.records[0]
    opportunity = service.create_opportunity(
        OpportunityCreate(
            canonical_id=tender.canonical_id,
            version_hash=tender.version_hash,
        )
    )
    service.update_opportunity(
        opportunity.id,
        OpportunityUpdate(
            stage=OpportunityStage.FOLLOWING,
            owner="项目负责人",
            notes="已核验初始招标公告",
            is_read=True,
        ),
    )

    await service.run_query("最近1个月安徽服务器招标信息")

    refreshed = service.get_opportunity(opportunity.id)
    assert refreshed is not None
    assert refreshed.record.event_type == EventType.CHANGE
    assert refreshed.record.title == "安徽大学 GPU 服务器采购更正公告"
    assert refreshed.is_read is False
    assert refreshed.stage == OpportunityStage.FOLLOWING
    assert refreshed.owner == "项目负责人"
    assert refreshed.notes == "已核验初始招标公告"
    assert [item["event_type"] for item in service.opportunity_timeline(opportunity.id)] == [
        "招标公告",
        "更正公告",
    ]


def test_runtime_config_is_masked_token_guarded_and_persistent(tmp_path: Path):
    settings = make_settings(tmp_path)
    secret_value = "test-key-that-must-never-be-returned"
    app = create_app(settings, sources=[FakeSource()])
    with TestClient(app) as client:
        initial = client.get("/api/v1/config")
        assert initial.status_code == 200
        assert initial.json()["ai"]["llm_api_key"] == {"configured": False}

        denied = client.put(
            "/api/v1/config",
            json={"llm_base_url": "http://127.0.0.1:8045/v1"},
        )
        assert denied.status_code == 403

        token_response = client.post("/api/v1/config/edit-token")
        assert token_response.status_code == 200
        token = token_response.json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        saved = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "llm_base_url": "http://127.0.0.1:8045/v1",
                "llm_model": "compatible-test-model",
                "llm_api_key": secret_value,
                "intent_llm_mode": "always",
                "intent_llm_confidence_threshold": 0.91,
                "intelligence_brief_mode": "off",
                "intelligence_brief_max_records": 9,
                "smtp_port": 587,
                "smtp_security": "starttls",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["ai"]["ready"] is True
        assert saved.json()["ai"]["llm_api_key"] == {"configured": True}
        assert secret_value not in saved.text

        preserved = client.put(
            "/api/v1/config",
            headers=headers,
            json={"llm_api_key": ""},
        )
        assert preserved.json()["ai"]["llm_api_key"] == {"configured": True}

        rejected = client.put(
            "/api/v1/config",
            headers=headers,
            json={"unknown_setting": "unsafe"},
        )
        assert rejected.status_code == 422

    restarted = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    persisted = restarted.runtime_config.snapshot()
    assert persisted.ai.llm_base_url == "http://127.0.0.1:8045/v1"
    assert persisted.ai.llm_model == "compatible-test-model"
    assert persisted.ai.llm_api_key.configured is True
    assert persisted.ai.intent_llm_mode == "always"
    assert persisted.ai.intent_llm_confidence_threshold == 0.91
    assert persisted.ai.intelligence_brief_mode == "off"
    assert persisted.ai.intelligence_brief_max_records == 9
    assert restarted.settings.smtp_port == 587
    with restarted.db.connection() as conn:
        stored_secret = conn.execute(
            "SELECT value FROM runtime_config WHERE field='llm_api_key'"
        ).fetchone()["value"]
    assert secret_value not in stored_secret
    assert stored_secret.startswith("fernet:v1:")
    assert (settings.data_dir / "secrets" / "runtime_config.key").exists()

    app = create_app(make_settings(tmp_path), sources=[FakeSource()])
    with TestClient(app) as client:
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        cleared = client.put(
            "/api/v1/config",
            headers={"X-BidPilot-Config-Token": token},
            json={"clear_secrets": ["llm_api_key"]},
        )
        assert cleared.json()["ai"]["llm_api_key"] == {"configured": False}


def test_every_openapi_operation_has_detailed_chinese_usage_contract(tmp_path: Path):
    schema = create_app(make_settings(tmp_path), sources=[FakeSource()]).openapi()
    api_reference = Path("docs/API_REFERENCE.md").read_text(encoding="utf-8")
    required_sections = (
        "### 用途",
        "### 参数与请求体",
        "### 返回值",
        "### 副作用",
        "### 常见错误",
        "### 示例",
    )
    operations = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operations.append((method.upper(), path, operation))

    assert len(operations) == 37
    for method, path, operation in operations:
        description = operation.get("description", "")
        assert path in api_reference, f"{method} {path} 未写入独立 API 参考"
        assert operation.get("summary"), f"{method} {path} 缺少中文摘要"
        assert operation.get("tags"), f"{method} {path} 缺少中文分组"
        for section in required_sections:
            assert section in description, f"{method} {path} 缺少 {section}"


async def test_standalone_worker_reloads_web_runtime_config_before_run(tmp_path: Path):
    web_service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    worker_service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    assert worker_service.settings.delivery_webhook_timeout == 20

    web_service.runtime_config.update(RuntimeConfigUpdate(delivery_webhook_timeout=47))
    await worker_service.run_query("最近1个月安徽服务器招标信息")

    assert worker_service.settings.delivery_webhook_timeout == 47
