from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bidpilot.config import Settings
from bidpilot.fetch import HttpFetcher
from bidpilot.models import (
    SourceDiagnostic,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
    TenderRecord,
)
from bidpilot.normalize import deduplicate_records, normalize_item
from bidpilot.sources.base import SourceAdapter
from bidpilot.summarize import EvidenceSummarizer


@dataclass(slots=True)
class PipelineResult:
    records: list[TenderRecord]
    diagnostics: list[SourceDiagnostic]
    raw_count: int


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
        normalized = await asyncio.gather(
            *(normalize_item(item, spec, self.summarizer) for item in raw_items)
        )
        records = deduplicate_records([record for record in normalized if record is not None])

        for result in successful:
            kept_sources = sum(1 for record in records if result.source in record.sources)
            diagnostics.append(
                SourceDiagnostic(
                    source=result.source,
                    status=result.status,
                    fetched_count=len(result.items),
                    kept_count=kept_sources,
                    latency_ms=result.latency_ms,
                    message=result.message,
                )
            )
        return PipelineResult(records=records, diagnostics=diagnostics, raw_count=len(raw_items))
