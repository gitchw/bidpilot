from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from bidpilot.config import Settings
from bidpilot.fetch import HttpFetcher
from bidpilot.models import (
    RetrievalQuery,
    RetrievalTrace,
    SearchExplanation,
    SearchRoundDiagnostic,
    SearchSuggestion,
    SourceDiagnostic,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
    TenderRecord,
)
from bidpilot.normalize import (
    deduplicate_records,
    evaluate_item,
    hard_filter_reason,
    keyword_hits,
)
from bidpilot.retrieval import RetrievalPlanner
from bidpilot.sources.base import SourceAdapter
from bidpilot.summarize import EvidenceSummarizer


@dataclass(slots=True)
class PipelineResult:
    records: list[TenderRecord]
    diagnostics: list[SourceDiagnostic]
    raw_count: int
    search_explanation: SearchExplanation
    retrieval: RetrievalTrace


@dataclass(slots=True)
class _SourceCall:
    source: SourceAdapter
    query: RetrievalQuery
    round: int
    result: SourceSearchResult | None = None
    error: str = ""


class TenderPipeline:
    def __init__(
        self,
        settings: Settings,
        sources: list[SourceAdapter],
        planner: RetrievalPlanner | None = None,
    ):
        self.settings = settings
        self.sources = sources
        self.summarizer = EvidenceSummarizer(settings)
        self.planner = planner or RetrievalPlanner(settings, sources)

    async def run(self, spec: TenderQuerySpec) -> PipelineResult:
        plan = await self.planner.plan(spec)
        effective_spec = spec.model_copy(deep=True)
        effective_spec.keywords = list(
            dict.fromkeys([*spec.keywords, *(term.text for term in plan.terms)])
        )
        primary = next(query for query in plan.queries if query.round == 1)
        all_calls: list[_SourceCall] = []
        rounds: list[SearchRoundDiagnostic] = []

        async with HttpFetcher(self.settings) as fetcher:
            round_one_started = time.perf_counter()
            round_one = await self._execute_calls(
                [(source, primary) for source in self.sources],
                effective_spec,
                fetcher,
                round_number=1,
            )
            all_calls.extend(round_one)
            cumulative_items = self._unique_raw_items(all_calls)
            gaps = self._gap_analysis(cumulative_items, effective_spec, all_calls)
            rounds.append(
                self._round_diagnostic(
                    round_one,
                    effective_spec,
                    trigger="所有来源先执行用户核心主题，建立确定性召回基线。",
                    gaps=gaps,
                    elapsed_ms=round((time.perf_counter() - round_one_started) * 1000),
                )
            )

            reserve_queries = [query for query in plan.queries if query.round == 2]
            second_call_specs = self._second_round_calls(round_one, reserve_queries, plan)
            if (
                self._needs_second_round(cumulative_items, effective_spec, gaps)
                and second_call_specs
            ):
                round_two_started = time.perf_counter()
                round_two = await self._execute_calls(
                    second_call_specs,
                    effective_spec,
                    fetcher,
                    round_number=2,
                )
                all_calls.extend(round_two)
                cumulative_items = self._unique_raw_items(all_calls)
                gaps = self._gap_analysis(cumulative_items, effective_spec, all_calls)
                rounds.append(
                    self._round_diagnostic(
                        round_two,
                        effective_spec,
                        trigger="首轮可信候选不足，按受控同义词和行业术语执行一次缺口补搜。",
                        gaps=gaps,
                        elapsed_ms=round((time.perf_counter() - round_two_started) * 1000),
                    )
                )

        raw_items = self._unique_raw_items(all_calls)
        lexical_items = []
        boundary_items = []
        rejected_by_source: dict[str, Counter[str]] = defaultdict(Counter)
        for item in raw_items:
            reason = hard_filter_reason(item, effective_spec)
            if reason:
                rejected_by_source[item.source][reason] += 1
                continue
            hits, exact_title = keyword_hits(item, effective_spec)
            if hits or exact_title:
                lexical_items.append(item)
            else:
                boundary_items.append(item)

        (
            review_status,
            semantic_decisions,
            semantic_acceptance,
        ) = await self.planner.review_candidates(effective_spec, plan.terms, boundary_items)
        evaluation_items = [*lexical_items]
        semantic_scores: list[float | None] = [None] * len(lexical_items)
        for item in boundary_items:
            confidence = semantic_acceptance.get(item.source_url)
            if confidence is None:
                rejected_by_source[item.source]["keyword_mismatch"] += 1
                continue
            evaluation_items.append(item)
            semantic_scores.append(confidence)

        evaluated = await asyncio.gather(
            *(
                evaluate_item(
                    item,
                    effective_spec,
                    self.summarizer,
                    semantic_confidence=semantic_confidence,
                )
                for item, semantic_confidence in zip(evaluation_items, semantic_scores, strict=True)
            )
        )
        records = deduplicate_records(
            [record for record, _reason in evaluated if record is not None]
        )
        for item, (_record, reason) in zip(evaluation_items, evaluated, strict=True):
            if reason:
                rejected_by_source[item.source][reason] += 1

        diagnostics = self._source_diagnostics(all_calls, records, rejected_by_source)
        explanation = self._build_search_explanation(spec, diagnostics, records)
        accepted_semantic = sum(decision.outcome == "accepted" for decision in semantic_decisions)
        trace = RetrievalTrace(
            plan=plan,
            rounds=rounds,
            gap_analysis=gaps,
            semantic_review_status=review_status,
            semantic_decisions=semantic_decisions,
            unique_raw_candidates=len(raw_items),
            summary=(
                f"使用 {len(plan.terms)} 个受控概念完成 {len(rounds)} 轮检索，"
                f"去重后获得 {len(raw_items)} 个原始候选；语义复核接受 {accepted_semantic} 个，"
                f"最终保留 {len(records)} 条可追溯标讯。"
            ),
        )
        return PipelineResult(
            records=records,
            diagnostics=diagnostics,
            raw_count=len(raw_items),
            search_explanation=explanation,
            retrieval=trace,
        )

    async def _execute_calls(
        self,
        call_specs: list[tuple[SourceAdapter, RetrievalQuery]],
        effective_spec: TenderQuerySpec,
        fetcher: HttpFetcher,
        *,
        round_number: int,
    ) -> list[_SourceCall]:
        async def execute(source: SourceAdapter, query: RetrievalQuery) -> _SourceCall:
            query_spec = effective_spec.model_copy(deep=True)
            query_spec.topic = query.text
            try:
                result = await source.search(query_spec, fetcher)
                return _SourceCall(source=source, query=query, round=round_number, result=result)
            except Exception as exc:
                return _SourceCall(
                    source=source,
                    query=query,
                    round=round_number,
                    error=str(exc),
                )

        return list(await asyncio.gather(*(execute(source, query) for source, query in call_specs)))

    def _second_round_calls(
        self,
        round_one: list[_SourceCall],
        reserve_queries: list[RetrievalQuery],
        plan,
    ) -> list[tuple[SourceAdapter, RetrievalQuery]]:
        if plan.max_rounds < 2 or not reserve_queries:
            return []
        first_status = {
            call.source.source_id: call.result.status if call.result else SourceStatus.FAILED
            for call in round_one
        }
        priorities = {source_id: index for index, source_id in enumerate(plan.source_priorities)}
        sources = sorted(
            self.sources,
            key=lambda source: priorities.get(source.source_id, len(priorities)),
        )
        calls: list[tuple[SourceAdapter, RetrievalQuery]] = []
        for source in sources:
            if not source.supports_query_variants:
                continue
            if first_status.get(source.source_id) in {
                SourceStatus.AUTH_REQUIRED,
                SourceStatus.FAILED,
            }:
                continue
            for query in reserve_queries[: plan.query_budget_per_source]:
                calls.append((source, query))
        return calls

    @staticmethod
    def _unique_raw_items(calls: list[_SourceCall]) -> list:
        by_url = {}
        order: list[str] = []
        for call in calls:
            if call.result is None:
                continue
            for item in call.result.items:
                existing = by_url.get(item.source_url)
                if existing is None:
                    by_url[item.source_url] = item
                    order.append(item.source_url)
                elif len(item.body) > len(existing.body):
                    by_url[item.source_url] = item
        return [by_url[url] for url in order]

    @staticmethod
    def _matching_count(items: list, spec: TenderQuerySpec) -> int:
        matched = 0
        for item in items:
            if hard_filter_reason(item, spec):
                continue
            hits, exact_title = keyword_hits(item, spec)
            matched += int(bool(hits or exact_title))
        return matched

    def _gap_analysis(
        self,
        items: list,
        spec: TenderQuerySpec,
        calls: list[_SourceCall],
    ) -> list[str]:
        matched = self._matching_count(items, spec)
        gaps: list[str] = []
        if len(items) < 8:
            gaps.append(f"去重候选仅 {len(items)} 条，低于 8 条召回观察线")
        if matched < 5:
            gaps.append(f"通过硬条件且命中受控主题词的候选仅 {matched} 条，低于 5 条补搜线")
        limited = sorted(
            {
                call.source.name
                for call in calls
                if call.result is None
                or call.result.status
                in {SourceStatus.PARTIAL, SourceStatus.AUTH_REQUIRED, SourceStatus.FAILED}
            }
        )
        if limited:
            gaps.append(
                f"{len(limited)} 个来源存在公开范围、授权或可用性缺口：{'、'.join(limited)}"
            )
        return gaps

    def _needs_second_round(
        self,
        items: list,
        spec: TenderQuerySpec,
        gaps: list[str],
    ) -> bool:
        return bool(gaps) and self._matching_count(items, spec) < 5

    def _round_diagnostic(
        self,
        calls: list[_SourceCall],
        spec: TenderQuerySpec,
        *,
        trigger: str,
        gaps: list[str],
        elapsed_ms: int,
    ) -> SearchRoundDiagnostic:
        items = [item for call in calls if call.result for item in call.result.items]
        unique = self._unique_raw_items(calls)
        queries = list({call.query.id: call.query for call in calls}.values())
        return SearchRoundDiagnostic(
            round=calls[0].round if calls else 1,
            trigger=trigger,
            queries=queries,
            source_calls=len(calls),
            scanned_count=sum(
                (call.result.scanned_count or len(call.result.items))
                for call in calls
                if call.result
            ),
            candidate_count=len(items),
            unique_candidate_count=len(unique),
            matched_candidate_count=self._matching_count(unique, spec),
            latency_ms=elapsed_ms,
            gaps_after_round=gaps,
        )

    def _source_diagnostics(
        self,
        calls: list[_SourceCall],
        records: list[TenderRecord],
        rejected_by_source: dict[str, Counter[str]],
    ) -> list[SourceDiagnostic]:
        diagnostics: list[SourceDiagnostic] = []
        for source in self.sources:
            source_calls = [call for call in calls if call.source is source]
            results = [call.result for call in source_calls if call.result is not None]
            source_items = self._unique_raw_items(source_calls)
            prefilter: Counter[str] = Counter()
            messages: list[str] = []
            for call in source_calls:
                if call.result:
                    prefilter.update(call.result.prefilter_reasons)
                    if call.result.message:
                        messages.append(call.result.message)
                elif call.error:
                    messages.append(call.error)
            prefilter.update(rejected_by_source[source.name])
            statuses = [result.status for result in results]
            if not statuses:
                status = SourceStatus.FAILED
            elif all(item == SourceStatus.AUTH_REQUIRED for item in statuses):
                status = SourceStatus.AUTH_REQUIRED
            elif all(item == SourceStatus.FAILED for item in statuses):
                status = SourceStatus.FAILED
            elif any(
                item in {SourceStatus.PARTIAL, SourceStatus.AUTH_REQUIRED, SourceStatus.FAILED}
                for item in statuses
            ):
                status = SourceStatus.PARTIAL
            else:
                status = SourceStatus.OK
            query_count = len({call.query.text for call in source_calls})
            message = "；".join(dict.fromkeys(messages))
            if query_count > 1:
                message = f"完成 {query_count} 个受控查询变体。" + message
            diagnostics.append(
                SourceDiagnostic(
                    source=source.name,
                    status=status,
                    scanned_count=sum(
                        (result.scanned_count or len(result.items)) for result in results
                    ),
                    fetched_count=len(source_items),
                    kept_count=sum(1 for record in records if source.name in record.sources),
                    rejected_count=sum(prefilter.values()),
                    rejection_reasons=dict(prefilter),
                    latency_ms=sum(result.latency_ms for result in results),
                    message=message,
                )
            )
        return diagnostics

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
