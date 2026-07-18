from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass

from bidpilot.config import Settings
from bidpilot.fetch import HttpFetcher
from bidpilot.models import (
    SearchExplanation,
    SearchSuggestion,
    SourceDiagnostic,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
    TenderRecord,
)
from bidpilot.normalize import deduplicate_records, evaluate_item
from bidpilot.sources.base import SourceAdapter
from bidpilot.summarize import EvidenceSummarizer


@dataclass(slots=True)
class PipelineResult:
    records: list[TenderRecord]
    diagnostics: list[SourceDiagnostic]
    raw_count: int
    search_explanation: SearchExplanation


class TenderPipeline:
    def __init__(self, settings: Settings, sources: list[SourceAdapter]):
        self.settings = settings
        self.sources = sources
        self.summarizer = EvidenceSummarizer(settings)

    async def run(self, spec: TenderQuerySpec) -> PipelineResult:
        async with HttpFetcher(self.settings) as fetcher:
            source_results = await asyncio.gather(
                *(source.search(spec, fetcher) for source in self.sources),
                return_exceptions=True,
            )

        successful: list[SourceSearchResult] = []
        diagnostics: list[SourceDiagnostic] = []
        for source, result in zip(self.sources, source_results, strict=True):
            if isinstance(result, Exception):
                diagnostics.append(
                    SourceDiagnostic(
                        source=source.name,
                        status=SourceStatus.FAILED,
                        message=str(result),
                    )
                )
                continue
            successful.append(result)

        raw_items = [item for result in successful for item in result.items]
        evaluated = await asyncio.gather(
            *(evaluate_item(item, spec, self.summarizer) for item in raw_items)
        )
        records = deduplicate_records(
            [record for record, _reason in evaluated if record is not None]
        )
        rejected_by_source: dict[str, Counter[str]] = defaultdict(Counter)
        for item, (_record, reason) in zip(raw_items, evaluated, strict=True):
            if reason:
                rejected_by_source[item.source][reason] += 1

        for result in successful:
            kept_sources = sum(1 for record in records if result.source in record.sources)
            rejection_reasons = Counter(result.prefilter_reasons)
            rejection_reasons.update(rejected_by_source[result.source])
            diagnostics.append(
                SourceDiagnostic(
                    source=result.source,
                    status=result.status,
                    scanned_count=result.scanned_count or len(result.items),
                    fetched_count=len(result.items),
                    kept_count=kept_sources,
                    rejected_count=sum(rejection_reasons.values()),
                    rejection_reasons=dict(rejection_reasons),
                    latency_ms=result.latency_ms,
                    message=result.message,
                )
            )
        explanation = self._build_search_explanation(spec, diagnostics, records)
        return PipelineResult(
            records=records,
            diagnostics=diagnostics,
            raw_count=len(raw_items),
            search_explanation=explanation,
        )

    @staticmethod
    def _build_search_explanation(
        spec: TenderQuerySpec,
        diagnostics: list[SourceDiagnostic],
        records: list[TenderRecord],
    ) -> SearchExplanation:
        reasons: Counter[str] = Counter()
        for item in diagnostics:
            reasons.update(item.rejection_reasons)
        total_scanned = sum(item.scanned_count for item in diagnostics)
        total_candidates = sum(item.fetched_count for item in diagnostics)
        incomplete = [
            item
            for item in diagnostics
            if item.status
            in {SourceStatus.PARTIAL, SourceStatus.AUTH_REQUIRED, SourceStatus.FAILED}
        ]
        status_labels = {
            SourceStatus.PARTIAL: "覆盖不完整",
            SourceStatus.AUTH_REQUIRED: "需要登录",
            SourceStatus.FAILED: "抓取失败",
        }
        coverage_notes = [
            f"{item.source}：{status_labels[item.status]}（{item.message}）" for item in incomplete
        ]
        suggestions: list[SearchSuggestion] = []
        region = spec.region or "全国"
        if not records and (reasons.get("outside_time", 0) or total_candidates == 0):
            suggestions.append(
                SearchSuggestion(
                    id="expand_time",
                    title="扩大到最近 3 个月",
                    query=f"最近3个月{region}{spec.topic}采购招标信息",
                    explanation="扩大时间可能找到更早公告，也会增加已结束项目和重复候选。只会填回查询框，不会自动抓取。",
                )
            )
        if (
            not records
            and spec.region
            and (reasons.get("region_mismatch", 0) or total_candidates == 0)
        ):
            suggestions.append(
                SearchSuggestion(
                    id="expand_region",
                    title="临时改为全国",
                    query=f"最近3个月全国{spec.topic}采购招标信息",
                    explanation=(
                        f"去掉地域限制可以判断是否是{spec.region}本地确实没有，"
                        "但全国结果噪声会明显增加。"
                    ),
                )
            )
        alternative = next(
            (keyword for keyword in spec.keywords if keyword.casefold() != spec.topic.casefold()),
            None,
        )
        if not records and alternative:
            suggestions.append(
                SearchSuggestion(
                    id="try_synonym",
                    title=f"改用同义词“{alternative}”",
                    query=f"最近3个月{region}{alternative}采购招标信息",
                    explanation="部分平台标题只写同义词。分开检索可提高召回，但仍需人工确认与原主题的业务一致性。",
                )
            )

        coverage_complete = not incomplete
        if records:
            outcome = "matched_partial" if incomplete else "matched"
            summary = (
                f"扫描 {total_scanned} 条、进入统一过滤 {total_candidates} 条，"
                f"最终保留 {len(records)} 条。"
            )
            if incomplete:
                summary += "本轮存在部分/未授权来源，结果不能解释为全网完整覆盖。"
        elif total_candidates:
            outcome = "all_filtered"
            top = reasons.most_common(1)
            labels = {
                "outside_time": "超出时间范围",
                "region_mismatch": "地域不匹配",
                "event_type_mismatch": "公告类型不匹配",
                "excluded_keyword": "命中排除词",
                "keyword_mismatch": "主题或同义词未命中",
                "low_relevance": "综合相关度不足",
            }
            top_text = (
                f"，最多的淘汰原因是 {labels.get(top[0][0], top[0][0])}（{top[0][1]} 条）"
                if top
                else ""
            )
            summary = (
                f"来源扫描 {total_scanned} 条，其中 {max(0, total_scanned - total_candidates)} 条"
                f"在来源端按时间、地域或主题预过滤；{total_candidates} 条进入统一证据过滤，"
                f"最终保留 0 条{top_text}。"
            )
        else:
            outcome = "no_candidates"
            summary = (
                f"来源共扫描 {total_scanned} 条公开列表记录，但没有返回符合来源端初筛的候选。"
                "这可能表示当前范围确实较窄，也可能受来源公开入口、授权或历史检索能力限制。"
            )
        return SearchExplanation(
            outcome=outcome,
            total_scanned=total_scanned,
            total_candidates=total_candidates,
            total_kept=len(records),
            rejection_reasons=dict(reasons),
            coverage_complete=coverage_complete,
            coverage_notes=coverage_notes,
            summary=summary,
            suggestions=suggestions[:3],
        )
