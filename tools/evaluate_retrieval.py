from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from bidpilot.config import Settings
from bidpilot.intent import IntentParser
from bidpilot.pipeline import TenderPipeline
from bidpilot.sources import CCGPSource, CECBidSource, GGZYSource, MofcomSource, QianlimaSource

EVALUATION_QUERIES = (
    "最近1个月深圳充电桩招标信息",
    "最近1个月北京医疗设备采购信息",
    "最近3个月江苏数据中心招标信息",
    "最近1个月全国服务器采购信息",
)


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


async def evaluate_query(
    query: str,
    *,
    parser: IntentParser,
    pipeline: TenderPipeline,
    now: datetime,
) -> dict[str, Any]:
    spec = parser.parse(query, now=now)
    started = perf_counter()
    result = await pipeline.run(spec)
    elapsed_ms = round((perf_counter() - started) * 1000)
    urls = sorted({url for record in result.records for url in record.source_urls})
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
        "sources": [item.model_dump(mode="json") for item in result.diagnostics],
        "rejection_reasons": result.search_explanation.rejection_reasons,
        "coverage_complete": result.search_explanation.coverage_complete,
    }


async def run(output: Path, *, now: datetime | None = None) -> dict[str, Any]:
    settings = Settings()
    # The baseline isolates retrieval behavior. It intentionally avoids model calls,
    # database writes, report generation and delivery side effects.
    settings.intent_llm_mode = "off"
    settings.llm_base_url = ""
    settings.llm_api_key = ""
    settings.llm_model = ""
    parser = IntentParser(settings.timezone)
    pipeline = build_legacy_pipeline(settings)
    evaluated_at = now or datetime.now(ZoneInfo(settings.timezone))
    results = []
    for query in EVALUATION_QUERIES:
        print(f"[baseline] {query}", flush=True)
        results.append(
            await evaluate_query(query, parser=parser, pipeline=pipeline, now=evaluated_at)
        )
    payload = {
        "schema_version": 1,
        "mode": "legacy-v0.5-single-query",
        "evaluated_at": evaluated_at.isoformat(),
        "queries": results,
        "totals": {
            "scanned": sum(item["scanned"] for item in results),
            "candidates": sum(item["candidates"] for item in results),
            "kept": sum(item["kept"] for item in results),
            "unique_evidence_urls": sum(item["unique_evidence_urls"] for item in results),
        },
    }
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
    args = parser.parse_args()
    payload = asyncio.run(run(args.output))
    print(json.dumps(payload["totals"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
