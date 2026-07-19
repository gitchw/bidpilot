from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from bidpilot.config import Settings
from bidpilot.models import RawTender, SourceSearchResult, SourceStatus, TenderQuerySpec
from bidpilot.service import BidPilotService
from bidpilot.sources.base import SourceAdapter


class RejectionFixtureSource(SourceAdapter):
    name = "淘汰漏斗测试源"

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        now = datetime.now()
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.PARTIAL,
            scanned_count=5,
            prefilter_reasons={"keyword_mismatch": 2},
            items=[
                RawTender(
                    source=self.name,
                    source_url="https://example.com/old",
                    title="上海充电桩采购公告",
                    published_at=now - timedelta(days=400),
                    region="上海",
                    body="充电设施",
                ),
                RawTender(
                    source=self.name,
                    source_url="https://example.com/region",
                    title="北京充电桩采购公告",
                    published_at=now,
                    region="北京",
                    body="充电设施",
                ),
                RawTender(
                    source=self.name,
                    source_url="https://example.com/topic",
                    title="上海办公家具采购公告",
                    published_at=now,
                    region="上海",
                    body="桌椅",
                ),
            ],
            message="公开入口只覆盖部分列表",
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        request_interval=0.1,
        intent_llm_mode="off",
    )


async def test_zero_results_explain_scan_filter_and_safe_relaxations(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[RejectionFixtureSource()])
    result = await service.run_query("最近1个月上海充电桩招标信息")
    explanation = result.search_explanation
    assert explanation is not None
    assert explanation.outcome == "all_filtered"
    assert explanation.total_scanned == 5
    assert explanation.total_candidates == 3
    assert explanation.total_kept == 0
    assert explanation.coverage_complete is False
    assert explanation.rejection_reasons == {
        "keyword_mismatch": 3,
        "outside_time": 1,
        "region_mismatch": 1,
    }
    assert {item.id for item in explanation.suggestions} == {
        "expand_time",
        "expand_region",
        "try_synonym",
    }
    assert all(item.query for item in explanation.suggestions)
    assert all("partial" not in note for note in explanation.coverage_notes)
    assert "覆盖不完整" in explanation.coverage_notes[0]
    region_suggestion = next(item for item in explanation.suggestions if item.id == "expand_region")
    assert "上海本地" in region_suggestion.explanation
    diagnostic = result.diagnostics[0]
    assert diagnostic.scanned_count == 5
    assert diagnostic.fetched_count == 3
    assert diagnostic.rejected_count == 5

    persisted = service.list_runs(limit=1)[0]["diagnostics"][0]
    assert persisted["scanned_count"] == 5
    assert persisted["rejection_reasons"]["region_mismatch"] == 1
