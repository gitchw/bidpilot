from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bidpilot.config import Settings
from bidpilot.fetch import HttpFetcher
from bidpilot.intent import IntentParser
from bidpilot.sources import CEBPubServiceSource, PLAPSource, SZGGZYSource, ZYCGSource


async def run(query: str, output: Path) -> dict:
    settings = Settings(llm_base_url="", llm_api_key="", llm_model="")
    spec = IntentParser(settings.timezone).parse(
        query,
        now=datetime.now(ZoneInfo(settings.timezone)),
    )
    sources = [
        CEBPubServiceSource(settings),
        PLAPSource(settings),
        ZYCGSource(settings),
        SZGGZYSource(settings),
    ]
    async with HttpFetcher(settings) as fetcher:
        results = await asyncio.gather(
            *(source.search(spec, fetcher) for source in sources),
            return_exceptions=True,
        )
    rows = []
    for source, result in zip(sources, results, strict=True):
        if isinstance(result, Exception):
            rows.append(
                {
                    "source_id": source.source_id,
                    "source": source.name,
                    "status": "failed",
                    "message": str(result),
                    "items": [],
                }
            )
            continue
        rows.append(
            {
                "source_id": source.source_id,
                "source": source.name,
                "status": result.status.value,
                "scanned_count": result.scanned_count,
                "candidate_count": len(result.items),
                "message": result.message,
                "items": [
                    {
                        "title": item.title,
                        "published_at": item.published_at.isoformat(),
                        "region": item.region,
                        "url": item.source_url,
                        "body_length": len(item.body),
                    }
                    for item in result.items[:5]
                ],
            }
        )
    payload = {
        "schema_version": 1,
        "evaluated_at": datetime.now(ZoneInfo(settings.timezone)).isoformat(),
        "query": query,
        "sources": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="真实访问 v0.6 新增官方标讯来源")
    parser.add_argument("--query", default="最近1个月全国服务器采购信息")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/evaluations/source-smoke-v0.6.json"),
    )
    args = parser.parse_args()
    payload = asyncio.run(run(args.query, args.output))
    print(
        json.dumps(
            [
                {
                    "source": item["source"],
                    "status": item["status"],
                    "candidates": item.get("candidate_count", 0),
                }
                for item in payload["sources"]
            ],
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
