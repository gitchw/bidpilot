from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from bidpilot.config import Settings
from bidpilot.intent import IntentParser
from bidpilot.pipeline import TenderPipeline
from bidpilot.sources import (
    CCGPSource,
    CEBPubServiceSource,
    CECBidSource,
    GDGPOSource,
    GGZYSource,
    MofcomSource,
    PLAPSource,
    QianlimaSource,
    SZGGZYSource,
    ZYCGSource,
)

EVALUATION_QUERIES = (
    "最近1个月深圳充电桩招标信息",
    "最近1个月北京医疗设备采购信息",
    "最近3个月江苏数据中心招标信息",
    "最近1个月全国服务器采购信息",
)


def classify_url_status(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "reachable"
    if status_code in {401, 403, 405, 406, 409, 418, 423, 429, 451, 509}:
        return "access_limited"
    if status_code in {404, 410}:
        return "broken"
    return "network_error"


async def validate_evidence_urls(
    urls: list[str],
    settings: Settings,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(8)
    headers = {
        "User-Agent": settings.user_agent,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Range": "bytes=0-1023",
    }
    timeout = httpx.Timeout(15.0, connect=8.0)
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
        limits=limits,
    ) as client:

        async def check(url: str) -> dict[str, Any]:
            started = perf_counter()
            try:
                async with semaphore, client.stream("GET", url) as response:
                    status_code = response.status_code
                    state = classify_url_status(status_code)
                    return {
                        "url": url,
                        "state": state,
                        "status_code": status_code,
                        "final_url": str(response.url),
                        "latency_ms": round((perf_counter() - started) * 1000),
                        "message": (
                            "原文入口可直接访问"
                            if state == "reachable"
                            else "原站限制自动访问，保留给用户在浏览器按原权限核验"
                            if state == "access_limited"
                            else "原文入口返回不存在"
                            if state == "broken"
                            else "原站返回异常状态"
                        ),
                    }
            except (httpx.HTTPError, ValueError) as exc:
                return {
                    "url": url,
                    "state": "network_error",
                    "status_code": None,
                    "final_url": "",
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "message": f"自动核验未完成：{exc.__class__.__name__}",
                }

        return await asyncio.gather(*(check(url) for url in urls))


def compare_with_baseline(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    baseline_queries = {item["query"]: item for item in baseline.get("queries", [])}
    comparisons = []
    improved_queries = 0
    regressions: list[str] = []
    for item in current.get("queries", []):
        previous = baseline_queries.get(item["query"], {})
        candidate_delta = item["candidates"] - int(previous.get("candidates", 0))
        kept_delta = item["kept"] - int(previous.get("kept", 0))
        url_delta = item["unique_evidence_urls"] - int(previous.get("unique_evidence_urls", 0))
        improved = candidate_delta > 0 or kept_delta > 0
        improved_queries += int(improved)
        if url_delta < 0:
            regressions.append(f"{item['query']} 的证据 URL 比基线减少 {abs(url_delta)} 个")
        comparisons.append(
            {
                "query": item["query"],
                "candidate_delta": candidate_delta,
                "kept_delta": kept_delta,
                "evidence_url_delta": url_delta,
                "improved": improved,
            }
        )
    broken_urls = [
        row["url"]
        for item in current.get("queries", [])
        for row in item.get("url_validation", [])
        if row["state"] in {"broken", "network_error"}
    ]
    criteria = {
        "no_evidence_url_regression": not regressions,
        "at_least_three_queries_improved": improved_queries >= 3,
        "all_evidence_urls_verified": not broken_urls,
    }
    return {
        "baseline_mode": baseline.get("mode", "legacy-v0.5-single-query"),
        "criteria": criteria,
        "passed": all(criteria.values()),
        "improved_query_count": improved_queries,
        "comparisons": comparisons,
        "regressions": regressions,
        "unverified_or_broken_urls": broken_urls,
    }


def build_legacy_pipeline(settings: Settings) -> TenderPipeline:
    """Build the v0.5 single-query source matrix without touching business data."""
    sources = [
        CECBidSource(settings),
        CCGPSource(settings),
        GGZYSource(settings),
        MofcomSource(settings),
        QianlimaSource(settings),
    ]
    return TenderPipeline(settings, sources)


def build_current_pipeline(settings: Settings) -> TenderPipeline:
    sources = [
        SZGGZYSource(settings),
        GDGPOSource(settings),
        CEBPubServiceSource(settings),
        PLAPSource(settings),
        ZYCGSource(settings),
        CECBidSource(settings),
        CCGPSource(settings),
        GGZYSource(settings),
        MofcomSource(settings),
        QianlimaSource(settings),
    ]
    return TenderPipeline(settings, sources)


async def evaluate_query(
    query: str,
    *,
    parser: IntentParser,
    pipeline: TenderPipeline,
    now: datetime,
    validate_urls: bool,
) -> dict[str, Any]:
    spec = parser.parse(query, now=now)
    started = perf_counter()
    result = await pipeline.run(spec)
    elapsed_ms = round((perf_counter() - started) * 1000)
    urls = sorted({url for record in result.records for url in record.source_urls})
    url_validation = await validate_evidence_urls(urls, pipeline.settings) if validate_urls else []
    validation_counts: dict[str, int] = {}
    for item in url_validation:
        validation_counts[item["state"]] = validation_counts.get(item["state"], 0) + 1
    return {
        "query": query,
        "spec": {
            "topic": spec.topic,
            "keywords": spec.keywords,
            "region": spec.region,
            "region_level": spec.region_level,
            "start_date": spec.start_date.isoformat(),
            "end_date": spec.end_date.isoformat(),
        },
        "elapsed_ms": elapsed_ms,
        "scanned": sum(item.scanned_count for item in result.diagnostics),
        "candidates": sum(item.fetched_count for item in result.diagnostics),
        "kept": len(result.records),
        "unique_evidence_urls": len(urls),
        "evidence_urls": urls,
        "records": [
            {
                "title": record.title,
                "published_at": record.published_at.isoformat(),
                "region": record.region,
                "buyer": record.buyer,
                "event_type": record.event_type.value,
                "sources": record.sources,
                "source_urls": record.source_urls,
                "relevance_score": record.relevance_score,
                "opportunity_score": record.opportunity_score,
            }
            for record in result.records
        ],
        "url_validation": url_validation,
        "url_validation_counts": validation_counts,
        "sources": [item.model_dump(mode="json") for item in result.diagnostics],
        "rejection_reasons": result.search_explanation.rejection_reasons,
        "coverage_complete": result.search_explanation.coverage_complete,
        "retrieval": result.retrieval.model_dump(mode="json"),
    }


async def run(
    output: Path,
    *,
    mode: str = "legacy",
    now: datetime | None = None,
    validate_urls: bool = True,
) -> dict[str, Any]:
    if mode == "legacy":
        if not output.exists():
            raise FileNotFoundError("历史基线不存在；必须在修改检索代码前生成，不能用新代码伪造")
        return json.loads(output.read_text(encoding="utf-8"))
    settings = Settings()
    # The baseline isolates retrieval behavior. It intentionally avoids model calls,
    # database writes, report generation and delivery side effects.
    settings.intent_llm_mode = "off"
    settings.llm_base_url = ""
    settings.llm_api_key = ""
    settings.llm_model = ""
    settings.retrieval_llm_mode = "off"
    parser = IntentParser(settings.timezone)
    pipeline = build_current_pipeline(settings)
    evaluated_at = now or datetime.now(ZoneInfo(settings.timezone))
    results = []
    for query in EVALUATION_QUERIES:
        print(f"[{mode}] {query}", flush=True)
        results.append(
            await evaluate_query(
                query,
                parser=parser,
                pipeline=pipeline,
                now=evaluated_at,
                validate_urls=validate_urls,
            )
        )
    payload = {
        "schema_version": 2,
        "mode": "current-v0.6-multi-round-deterministic",
        "evaluated_at": evaluated_at.isoformat(),
        "queries": results,
        "totals": {
            "scanned": sum(item["scanned"] for item in results),
            "candidates": sum(item["candidates"] for item in results),
            "kept": sum(item["kept"] for item in results),
            "unique_evidence_urls": sum(item["unique_evidence_urls"] for item in results),
            "reachable_urls": sum(
                item["url_validation_counts"].get("reachable", 0) for item in results
            ),
            "access_limited_urls": sum(
                item["url_validation_counts"].get("access_limited", 0) for item in results
            ),
            "broken_urls": sum(item["url_validation_counts"].get("broken", 0) for item in results),
            "network_error_urls": sum(
                item["url_validation_counts"].get("network_error", 0) for item in results
            ),
        },
    }
    baseline_path = Path("outputs/evaluations/retrieval-baseline-v0.5.json")
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        payload["benchmark"] = compare_with_baseline(baseline, payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="评测 BidPilot 固定查询的真实检索召回")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/evaluations/retrieval-baseline-v0.5.json"),
        help="评测 JSON 输出路径",
    )
    parser.add_argument("--mode", choices=("legacy", "current"), default="current")
    parser.add_argument(
        "--skip-url-validation",
        action="store_true",
        help="只在开发调试时跳过原文 URL 在线核验",
    )
    args = parser.parse_args()
    if args.mode == "current" and args.output == Path(
        "outputs/evaluations/retrieval-baseline-v0.5.json"
    ):
        args.output = Path("outputs/evaluations/retrieval-current-v0.6.json")
    payload = asyncio.run(
        run(
            args.output,
            mode=args.mode,
            validate_urls=not args.skip_url_validation,
        )
    )
    print(json.dumps(payload["totals"], ensure_ascii=False), flush=True)
    if payload.get("benchmark"):
        print(
            json.dumps(
                {
                    "benchmark_passed": payload["benchmark"]["passed"],
                    "improved_query_count": payload["benchmark"]["improved_query_count"],
                    "unverified_or_broken_urls": len(
                        payload["benchmark"]["unverified_or_broken_urls"]
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
