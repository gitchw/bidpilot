import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.buyer_radar import buyer_identity
from bidpilot.config import Settings
from bidpilot.models import (
    EventType,
    EvidenceSpan,
    IntentSchedule,
    RawTender,
    RetrievalTerm,
    ScheduleKind,
    SourceDiagnostic,
    SourceSearchResult,
    SourceStatus,
    SubscriptionUpdate,
    TenderQuerySpec,
    TenderRecord,
)
from bidpilot.normalize import buyer_matches, hard_filter_reason, keyword_hits, relevance_score
from bidpilot.pipeline import TenderPipeline
from bidpilot.retrieval import RetrievalPlanner
from bidpilot.service import BidPilotService
from bidpilot.sources.base import SourceAdapter

TIMEZONE = ZoneInfo("Asia/Shanghai")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        request_interval=0.1,
        embedded_worker=False,
        intent_llm_mode="off",
        retrieval_llm_mode="off",
        intelligence_brief_mode="off",
        decision_assessment_mode="off",
    )


def make_record(
    canonical_id: str,
    version_hash: str,
    project_key: str,
    *,
    buyer: str | None,
    title: str,
    published_at: datetime,
    event_type: EventType = EventType.TENDER,
    source: str = "本地测试来源",
) -> TenderRecord:
    url = f"https://evidence.example/{canonical_id}/{version_hash}"
    return TenderRecord(
        canonical_id=canonical_id,
        project_key=project_key,
        version_hash=version_hash,
        title=title,
        published_at=published_at,
        region="安徽",
        buyer=buyer,
        event_type=event_type,
        project_id=f"P-{project_key}",
        summary=f"{title} 的本地证据摘要",
        body_excerpt=f"采购单位：{buyer or '未识别'}。采购内容：服务器与存储系统。",
        evidence=[EvidenceSpan(text=title, source_url=url, field="title")],
        source_urls=[url],
        sources=[source],
        relevance_score=88,
        opportunity_score=76,
        lifecycle_id=project_key,
    )


def seed_radar(service: BidPilotService) -> list[TenderRecord]:
    records = [
        make_record(
            "notice-a",
            "version-a1",
            "project-a",
            buyer="安徽大学",
            title="安徽大学 GPU 服务器采购公告（旧版）",
            published_at=datetime(2026, 7, 1, 9, 0, tzinfo=TIMEZONE),
        ),
        make_record(
            "notice-a",
            "version-a2",
            "project-a",
            buyer="安徽大学",
            title="安徽大学 GPU 服务器采购公告",
            published_at=datetime(2026, 7, 2, 9, 0, tzinfo=TIMEZONE),
        ),
        make_record(
            "notice-b",
            "version-b1",
            "project-b",
            buyer="安徽大学",
            title="安徽大学存储系统采购中标公告",
            published_at=datetime(2026, 7, 12, 9, 0, tzinfo=TIMEZONE),
            event_type=EventType.AWARD,
        ),
        make_record(
            "notice-c",
            "version-c1",
            "project-c",
            buyer="合肥研究院",
            title="合肥研究院数据中心设备采购公告",
            published_at=datetime(2026, 7, 8, 9, 0, tzinfo=TIMEZONE),
        ),
        make_record(
            "notice-d",
            "version-d1",
            "project-d",
            buyer=None,
            title="未识别采购单位的网络安全设备采购公告",
            published_at=datetime(2026, 7, 7, 9, 0, tzinfo=TIMEZONE),
        ),
    ]
    service.db.upsert_records([record.model_dump(mode="json") for record in records])
    return records


class CountingBuyerSource(SourceAdapter):
    source_id = "buyer-test"
    name = "买方过滤测试来源"

    def __init__(self) -> None:
        self.calls = 0
        self.queries: list[str] = []
        self.published_at = datetime.now(TIMEZONE).replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(days=2)

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        self.calls += 1
        self.queries.append(spec.topic)
        items = []
        for suffix, buyer in (("anhui", "安徽大学"), ("beijing", "北京大学")):
            url = f"https://source.example/{suffix}"
            body = f"采购单位：{buyer}。采购 GPU 服务器及配套存储。"
            items.append(
                RawTender(
                    source=self.name,
                    source_url=url,
                    title=f"{buyer} GPU 服务器采购公告",
                    published_at=self.published_at,
                    region="安徽",
                    buyer=buyer,
                    body=body,
                    event_type=EventType.TENDER,
                    project_id=f"TEST-{suffix.upper()}",
                    evidence=[EvidenceSpan(text=body, source_url=url)],
                )
            )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=items,
            scanned_count=len(items),
            message="fixture",
        )


def test_buyer_radar_aggregates_local_notices_with_auditable_counts(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    seed_radar(service)
    now = datetime.now(TIMEZONE).isoformat()
    with service.db.connection() as conn:
        conn.execute(
            """
            INSERT INTO tender_items(
              canonical_id, version_hash, project_key, title,
              payload_json, first_seen_at, last_seen_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            ("notice-damaged", "broken", "broken", "损坏快照", "{}", now, now),
        )

    result = service.list_buyer_radar(activity_limit=10)

    assert result.total_local_notice_count == 5
    assert result.identified_buyer_notice_count == 3
    assert result.unknown_buyer_notice_count == 2
    assert result.invalid_version_count == 1
    assert result.buyer_coverage_rate == 60.0
    assert result.total_buyer_count == 2
    assert "tender_items" in result.coverage_note
    assert "估算" in result.coverage_note

    university = next(card for card in result.buyers if card.buyer_name == "安徽大学")
    assert university.buyer_id == buyer_identity("安徽大学")
    assert university.notice_count == 2
    assert university.version_count == 3
    assert university.project_count == 2
    assert university.project_count_is_estimate is True
    assert university.stage_counts == {"中标公告": 1, "招标公告": 1}
    assert {topic.name for topic in university.top_topics} >= {"服务器", "存储"}
    assert university.evidence_notice_count == 2
    assert len(university.recent_activities) == 2
    activity = university.recent_activities[0]
    assert activity.canonical_id == "notice-b"
    assert activity.source_url == "https://evidence.example/notice-b/version-b1"
    assert activity.evidence_available is True
    changed_notice = next(
        item for item in university.recent_activities if item.canonical_id == "notice-a"
    )
    assert changed_notice.version_hash == "version-a2"


def test_buyer_radar_search_and_limits_do_not_change_global_coverage(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    seed_radar(service)

    searched = service.list_buyer_radar(search="研究院", limit=1, activity_limit=10)

    assert searched.total_buyer_count == 2
    assert searched.matched_buyer_count == 1
    assert searched.returned_buyer_count == 1
    assert searched.buyers[0].buyer_name == "合肥研究院"
    assert searched.total_local_notice_count == 4
    assert searched.identified_buyer_notice_count == 3


def test_buyer_radar_does_not_turn_non_http_snapshot_values_into_links(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    record = make_record(
        "unsafe-url",
        "unsafe-version",
        "unsafe-project",
        buyer="安全采购单位",
        title="安全采购单位服务器采购公告",
        published_at=datetime(2026, 7, 10, 9, 0, tzinfo=TIMEZONE),
    )
    record.evidence[0].source_url = "javascript:alert(1)"
    record.source_urls = ["data:text/html,unsafe"]
    service.db.upsert_records([record.model_dump(mode="json")])

    activity = service.list_buyer_radar().buyers[0].recent_activities[0]

    assert activity.source_url == ""
    assert activity.evidence_available is False


def test_buyer_radar_api_is_local_only_and_documents_every_boundary(tmp_path: Path):
    source = CountingBuyerSource()
    app = create_app(make_settings(tmp_path), sources=[source])
    seed_radar(app.state.service)

    with TestClient(app) as client:
        response = client.get("/api/v1/buyers?search=大学&limit=20&activity_limit=2")
        assert response.status_code == 200
        assert response.json()["buyers"][0]["buyer_name"] == "安徽大学"
        assert source.calls == 0

        invalid = client.get("/api/v1/buyers?limit=0")
        assert invalid.status_code == 422

        schema = client.get("/openapi.json").json()
        for path, method in (
            ("/api/v1/buyers", "get"),
            ("/api/v1/buyers/{buyer_id}/subscriptions", "post"),
        ):
            operation = schema["paths"][path][method]
            assert operation["summary"]
            assert operation["tags"] == ["买方雷达"]
            for section in (
                "### 用途",
                "### 参数与请求体",
                "### 返回值",
                "### 副作用",
                "### 常见错误",
                "### 示例",
            ):
                assert section in operation["description"]

        spec_schema = schema["components"]["schemas"]["TenderQuerySpec"]
        buyer_field = spec_schema["properties"]["buyer_keywords"]
        assert "精确过滤" in buyer_field["description"]
        assert "buyer_keywords" not in spec_schema.get("required", [])
        for schema_name in (
            "BuyerRadarTopic",
            "BuyerRadarActivity",
            "BuyerRadarCard",
            "BuyerRadarResult",
            "BuyerSubscriptionCreate",
        ):
            component = schema["components"]["schemas"][schema_name]
            assert any("\u4e00" <= char <= "\u9fff" for char in component["description"])
            for field_name, field_schema in component["properties"].items():
                assert field_schema.get("description"), f"{schema_name}.{field_name} 缺少中文说明"
                assert any("\u4e00" <= char <= "\u9fff" for char in field_schema["description"]), (
                    f"{schema_name}.{field_name} 不是中文说明"
                )


def test_buyer_subscription_api_uses_unified_path_and_preserves_filter_on_edit(tmp_path: Path):
    source = CountingBuyerSource()
    app = create_app(make_settings(tmp_path), sources=[source])
    seed_radar(app.state.service)
    buyer_id = buyer_identity("安徽大学")
    query = "每天9点汇总最近1个月服务器采购公告"

    with TestClient(app) as client:
        created = client.post(
            f"/api/v1/buyers/{buyer_id}/subscriptions",
            json={
                "name": "安徽大学采购监控",
                "query": query,
                "delivery_channel": "local",
                "delivery_policy": "on_change",
                "run_immediately": False,
            },
        )
        assert created.status_code == 200
        subscription = created.json()
        assert subscription["spec"]["buyer_keywords"] == ["安徽大学"]
        assert subscription["spec"]["resolution"]["llm_status"] == "disabled"
        buyer_decision = next(
            decision
            for decision in subscription["spec"]["resolution"]["decisions"]
            if decision["field"] == "buyer_keywords"
        )
        assert buyer_decision["outcome"] == "locked"
        assert buyer_decision["final_value"] == ["安徽大学"]
        assert subscription["delivery_policy"] == "on_change"

        repeated = client.post(
            f"/api/v1/buyers/{buyer_id}/subscriptions",
            json={
                "name": "重复点击",
                "query": query,
                "delivery_channel": "local",
                "delivery_policy": "on_change",
                "run_immediately": False,
            },
        )
        assert repeated.json()["id"] == subscription["id"]

        general = client.post(
            "/api/v1/subscriptions",
            json={
                "name": "普通服务器监控",
                "query": query,
                "delivery_channel": "local",
                "delivery_policy": "on_change",
                "run_immediately": False,
            },
        )
        assert general.status_code == 200
        assert general.json()["id"] != subscription["id"]
        assert general.json()["spec"]["buyer_keywords"] == []

        updated = client.patch(
            f"/api/v1/subscriptions/{subscription['id']}",
            json={"query": "每周一8点汇总最近2周服务器采购公告"},
        )
        assert updated.status_code == 200
        assert updated.json()["spec"]["buyer_keywords"] == ["安徽大学"]

        missing = client.post(
            "/api/v1/buyers/ffffffffffffffffffffffff/subscriptions",
            json={"name": "不存在", "query": query, "run_immediately": False},
        )
        assert missing.status_code == 404

        forged_buyer = client.post(
            f"/api/v1/buyers/{buyer_id}/subscriptions",
            json={
                "name": "伪造买方",
                "query": query,
                "buyer_keywords": ["北京大学"],
                "run_immediately": False,
            },
        )
        assert forged_buyer.status_code == 422

        no_schedule = client.post(
            f"/api/v1/buyers/{buyer_id}/subscriptions",
            json={"name": "无计划", "query": "最近1个月服务器采购公告"},
        )
        assert no_schedule.status_code == 422
        assert source.calls == 0


async def test_buyer_subscription_run_hard_filters_other_buyers_and_uses_ledger(tmp_path: Path):
    source = CountingBuyerSource()
    settings = make_settings(tmp_path)
    service = BidPilotService(settings, sources=[source])
    seed_radar(service)
    subscription = await service.create_buyer_subscription(
        buyer_identity("安徽大学"),
        "安徽大学服务器监控",
        "每天9点汇总最近1个月服务器采购公告",
        run_immediately=False,
    )
    restarted = BidPilotService(settings, sources=[source])
    restored = restarted.get_subscription(subscription.id)
    assert restored is not None
    assert restored.spec.buyer_keywords == ["安徽大学"]
    restored = restarted.update_subscription(
        subscription.id,
        SubscriptionUpdate(query="每天8点汇总最近1个月服务器采购公告"),
    )
    assert restored.spec.buyer_keywords == ["安徽大学"]

    first = await restarted.run_subscription(subscription.id)
    second = await restarted.run_subscription(subscription.id)

    assert first.spec.buyer_keywords == ["安徽大学"]
    assert [record.buyer for record in first.records] == ["安徽大学"]
    assert first.retrieval is not None
    assert first.retrieval.plan.queries[0].text == "安徽大学"
    assert first.retrieval.plan.queries[0].term_kind == "buyer"
    assert any(
        query.text == "服务器" and query.round == 2 for query in first.retrieval.plan.queries
    )
    assert "安徽大学" not in {term.text for term in first.retrieval.plan.terms}
    assert first.new_count == 1
    assert second.new_count == 0
    assert source.calls == 2
    assert source.queries == ["安徽大学", "安徽大学"]
    stored_run = restarted.db.get_run(first.run_id)
    assert json.loads(stored_run["spec_json"])["buyer_keywords"] == ["安徽大学"]
    assert restarted.db.list_delivery_attempts(subscription_id=subscription.id)


def test_buyer_filter_is_exact_and_old_subscription_specs_default_to_unfiltered(tmp_path: Path):
    today = datetime.now(TIMEZONE).date()
    spec = TenderQuerySpec(
        raw_query="每天监控服务器",
        topic="服务器",
        keywords=["服务器"],
        buyer_keywords=["安徽大学"],
        start_date=today - timedelta(days=30),
        end_date=today,
        schedule=IntentSchedule(kind=ScheduleKind.DAILY),
    )
    exact = RawTender(
        source="测试",
        source_url="https://example.com/exact",
        title="服务器采购公告",
        published_at=datetime.now(TIMEZONE),
        buyer=" 安徽大学 ",
        body="采购服务器",
    )
    subsidiary = exact.model_copy(update={"buyer": "安徽大学附属医院"})
    missing = exact.model_copy(update={"buyer": None})

    assert buyer_matches(exact, spec) is True
    assert hard_filter_reason(exact, spec) is None
    assert hard_filter_reason(subsidiary, spec) == "buyer_mismatch"
    assert hard_filter_reason(missing, spec) == "buyer_mismatch"

    deceptive_buyer_spec = spec.model_copy(update={"buyer_keywords": ["安徽服务器科技大学"]})
    unrelated_notice = exact.model_copy(
        update={
            "buyer": "安徽服务器科技大学",
            "title": "安徽服务器科技大学办公家具采购公告",
            "body": "采购单位：安徽服务器科技大学。采购内容：桌椅和文件柜。",
        }
    )
    assert hard_filter_reason(unrelated_notice, deceptive_buyer_spec) is None
    assert keyword_hits(unrelated_notice, deceptive_buyer_spec) == (0, False)
    assert relevance_score(unrelated_notice, deceptive_buyer_spec) < 45
    review_payload = RetrievalPlanner(make_settings(tmp_path), [])._review_payload(
        deceptive_buyer_spec,
        [
            RetrievalTerm(
                text="服务器",
                kind="topic",
                origin="query",
                reason="用户主题",
            )
        ],
        {"c001": unrelated_notice},
    )
    review_candidate = json.loads(review_payload["messages"][1]["content"])["candidates"][0]
    assert review_candidate["buyer"] == ""
    assert "安徽服务器科技大学" not in review_candidate["title"]
    assert "安徽服务器科技大学" not in review_candidate["evidence_excerpt"]
    assert "服务器" not in review_candidate["title"]
    assert "服务器" not in review_candidate["evidence_excerpt"]

    service = BidPilotService(make_settings(tmp_path), sources=[])
    old_payload = spec.model_dump(mode="json")
    old_payload.pop("buyer_keywords")
    now = datetime.now(TIMEZONE).isoformat()
    with service.db.connection() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions(
              id, name, raw_query, spec_json, delivery_channel, delivery_policy,
              created_at, updated_at, next_run_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-subscription",
                "旧订阅",
                old_payload["raw_query"],
                json.dumps(old_payload, ensure_ascii=False, default=str),
                "local",
                "always",
                now,
                now,
                None,
            ),
        )

    restarted = BidPilotService(make_settings(tmp_path), sources=[])
    legacy = restarted.get_subscription("legacy-subscription")
    assert legacy is not None
    assert legacy.spec.buyer_keywords == []


def test_buyer_zero_result_explanation_never_suggests_dropping_buyer_filter(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    spec = service.parser.parse("最近1个月服务器采购公告")
    spec = service._lock_buyer_filter(spec, ["安徽大学"])
    diagnostic = SourceDiagnostic(
        source="测试来源",
        status=SourceStatus.OK,
        scanned_count=2,
        fetched_count=2,
        kept_count=0,
        rejected_count=2,
        rejection_reasons={"buyer_mismatch": 2},
    )

    explanation = TenderPipeline._build_search_explanation(spec, [diagnostic], [])

    assert explanation.suggestions == []
    assert explanation.rejection_reasons == {"buyer_mismatch": 2}
    assert "采购单位不匹配" in explanation.summary
    assert "安徽大学" in explanation.summary
    assert any("不会自动取消" in note for note in explanation.coverage_notes)


def test_unrelated_corrupted_subscription_does_not_block_new_subscription(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[])
    now = datetime.now(TIMEZONE).isoformat()
    with service.db.connection() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions(
              id, name, raw_query, spec_json, delivery_channel, delivery_policy,
              created_at, updated_at, next_run_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "damaged-unrelated",
                "损坏旧订阅",
                "每天9点监控打印机采购公告",
                "{not-json",
                "local",
                "always",
                now,
                now,
                None,
            ),
        )

    created = service.create_subscription(
        "服务器日报",
        "每天9点汇总最近1个月服务器采购公告",
        run_immediately=False,
    )

    assert created.id != "damaged-unrelated"
    assert created.spec.buyer_keywords == []
