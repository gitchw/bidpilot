from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.models import (
    EventType,
    EvidenceSpan,
    RawTender,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
)
from bidpilot.service import BidPilotService
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

        response = client.post(
            "/api/v1/runs",
            json={"query": "最近1个月安徽服务器招标信息", "delivery_channel": "local"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["new_count"] == 1
        assert result["records"][0]["project_id"] == "AH-2026-001"
        report_name = Path(result["report_path"]).name
        download = client.get(f"/api/v1/reports/{report_name}")
        assert download.status_code == 200
        assert download.content.startswith(b"PK")
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


def test_subscription_requires_schedule(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    try:
        service.create_subscription("无效", "最近1个月安徽服务器招标信息")
    except ValueError as exc:
        assert "必须包含" in str(exc)
    else:
        raise AssertionError("Expected schedule validation")
