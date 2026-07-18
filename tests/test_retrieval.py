from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bidpilot.config import Settings
from bidpilot.intent import IntentParser
from bidpilot.models import RawTender, SourceSearchResult, SourceStatus, TenderQuerySpec
from bidpilot.pipeline import TenderPipeline
from bidpilot.retrieval import RetrievalPlanner
from bidpilot.sources.base import SourceAdapter

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def make_settings(tmp_path: Path, **updates) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "report_dir": tmp_path / "reports",
        "database_path": tmp_path / "data" / "test.db",
        "request_interval": 0.1,
        "intent_llm_mode": "off",
        "retrieval_llm_mode": "off",
    }
    values.update(updates)
    return Settings(**values)


class QueryAwareSource(SourceAdapter):
    source_id = "query_fixture"
    name = "多轮检索测试源"
    supports_query_variants = True

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        self.queries.append(spec.topic)
        titles = {
            "医疗设备": "北京市医学装备更新项目",
            "医疗器械": "北京市医疗器械采购项目",
            "医用设备": "北京市医用设备购置项目",
        }
        title = titles.get(spec.topic)
        items = []
        if title:
            items.append(
                RawTender(
                    source=self.name,
                    source_url=f"https://example.com/{len(self.queries)}",
                    title=title,
                    published_at=NOW,
                    region="北京",
                    body=f"{title}，公开采购。",
                )
            )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=items,
            scanned_count=len(items),
            message="fixture",
        )


class FixedFeedSource(SourceAdapter):
    source_id = "fixed_fixture"
    name = "固定列表测试源"
    supports_query_variants = False

    def __init__(self):
        self.calls = 0

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        self.calls += 1
        return SourceSearchResult(source=self.name, status=SourceStatus.OK)


class BoundarySource(SourceAdapter):
    source_id = "boundary_fixture"
    name = "语义边界测试源"

    def __init__(self, region: str = "北京"):
        self.region = region

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        item = RawTender(
            source=self.name,
            source_url="https://example.com/ct",
            title="64排螺旋CT扫描仪购置项目",
            published_at=NOW,
            region=self.region,
            body="采购一套用于临床影像诊断的64排螺旋CT扫描仪。",
        )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=[item],
            scanned_count=1,
        )


async def test_deterministic_plan_and_bounded_second_round(tmp_path: Path):
    settings = make_settings(tmp_path)
    query_source = QueryAwareSource()
    fixed_source = FixedFeedSource()
    pipeline = TenderPipeline(settings, [query_source, fixed_source])
    spec = IntentParser().parse("最近1个月北京医疗设备采购信息", now=NOW)

    result = await pipeline.run(spec)

    assert {term.text for term in result.retrieval.plan.terms} >= {
        "医疗设备",
        "医疗器械",
        "医用设备",
        "医学装备",
    }
    assert [item.round for item in result.retrieval.rounds] == [1, 2]
    assert query_source.queries == ["医疗设备", "医疗器械", "医用设备"]
    assert fixed_source.calls == 1
    assert len(result.records) == 3
    assert result.retrieval.rounds[1].source_calls == 2


async def test_llm_plan_accepts_only_locally_validated_terms(tmp_path: Path):
    settings = make_settings(
        tmp_path,
        retrieval_llm_mode="auto",
        llm_base_url="https://model.invalid/v1",
        llm_model="fixture-model",
    )

    async def requester(payload):
        assert "不能生成或猜测公告" in payload["messages"][0]["content"]
        return json.dumps(
            {
                "terms": [
                    {
                        "text": "医学影像设备",
                        "kind": "industry",
                        "confidence": 0.93,
                        "reason": "医疗设备公告常用的专业品类表达",
                    },
                    {
                        "text": "采购",
                        "kind": "synonym",
                        "confidence": 0.99,
                        "reason": "通用词",
                    },
                    {
                        "text": "2026年医疗设备",
                        "kind": "industry",
                        "confidence": 0.99,
                        "reason": "混入日期",
                    },
                    {
                        "text": "影像科装备",
                        "kind": "industry",
                        "confidence": 0.2,
                        "reason": "低置信",
                    },
                ],
                "query_variants": ["医学影像设备", "采购"],
                "source_priorities": ["fixture", "unknown-id"],
                "rationale": "补充专业表述",
            },
            ensure_ascii=False,
        )

    source = FixedFeedSource()
    source.source_id = "fixture"
    planner = RetrievalPlanner(settings, [source], requester=requester)
    spec = IntentParser().parse("最近1个月北京医疗设备采购信息", now=NOW)

    plan = await planner.plan(spec)

    assert plan.mode == "hybrid"
    assert plan.llm_status == "applied"
    assert "医学影像设备" in {term.text for term in plan.terms}
    assert "采购" not in {term.text for term in plan.terms}
    assert len(plan.rejected_terms) == 3
    assert plan.source_priorities == ["fixture"]
    assert any(query.text == "医学影像设备" for query in plan.queries)


async def test_invalid_llm_plan_falls_back_without_failing(tmp_path: Path):
    settings = make_settings(
        tmp_path,
        retrieval_llm_mode="auto",
        llm_base_url="https://model.invalid/v1",
        llm_model="fixture-model",
    )

    async def requester(_payload):
        return "```json\n{}\n```"

    planner = RetrievalPlanner(settings, [], requester=requester)
    spec = IntentParser().parse("最近1个月深圳充电桩招标信息", now=NOW)
    plan = await planner.plan(spec)

    assert plan.mode == "fallback"
    assert plan.llm_status == "invalid_response"
    assert "充电桩" in {term.text for term in plan.terms}


async def test_semantic_review_can_accept_hard_filter_safe_boundary_candidate(tmp_path: Path):
    pipeline_settings = make_settings(tmp_path)
    planner_settings = make_settings(
        tmp_path,
        retrieval_llm_mode="auto",
        retrieval_max_rounds=1,
        retrieval_semantic_review=True,
        llm_base_url="https://model.invalid/v1",
        llm_model="fixture-model",
    )

    async def requester(payload):
        system = payload["messages"][0]["content"]
        if "检索规划器" in system:
            return json.dumps(
                {
                    "terms": [],
                    "query_variants": [],
                    "source_priorities": [],
                    "rationale": "本地词典足够",
                },
                ensure_ascii=False,
            )
        assert "不得放宽日期、地域" in system
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": "c001",
                        "relevant": True,
                        "confidence": 0.94,
                        "matched_concepts": ["诊疗设备"],
                        "reason": "CT扫描仪属于临床诊疗设备",
                    }
                ]
            },
            ensure_ascii=False,
        )

    source = BoundarySource()
    planner = RetrievalPlanner(planner_settings, [source], requester=requester)
    pipeline = TenderPipeline(pipeline_settings, [source], planner=planner)
    spec = IntentParser().parse("最近1个月北京医疗设备采购信息", now=NOW)

    result = await pipeline.run(spec)

    assert len(result.records) == 1
    assert result.records[0].title.startswith("64排螺旋CT")
    assert result.retrieval.semantic_review_status == "applied"
    assert result.retrieval.semantic_decisions[0].outcome == "accepted"
    assert result.records[0].relevance_score >= 45


async def test_semantic_review_never_overrides_region_hard_filter(tmp_path: Path):
    pipeline_settings = make_settings(tmp_path)
    planner_settings = make_settings(
        tmp_path,
        retrieval_llm_mode="auto",
        retrieval_max_rounds=1,
        llm_base_url="https://model.invalid/v1",
        llm_model="fixture-model",
    )
    calls = []

    async def requester(payload):
        calls.append(payload)
        return json.dumps(
            {
                "terms": [],
                "query_variants": [],
                "source_priorities": [],
                "rationale": "不扩展",
            },
            ensure_ascii=False,
        )

    source = BoundarySource(region="上海")
    planner = RetrievalPlanner(planner_settings, [source], requester=requester)
    pipeline = TenderPipeline(pipeline_settings, [source], planner=planner)
    spec = IntentParser().parse("最近1个月北京医疗设备采购信息", now=NOW)

    result = await pipeline.run(spec)

    assert result.records == []
    assert len(calls) == 1
    assert result.retrieval.semantic_review_status == "not_needed"
    assert result.diagnostics[0].rejection_reasons["region_mismatch"] == 1
