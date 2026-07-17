from __future__ import annotations

from abc import ABC, abstractmethod

from bidpilot.fetch import HttpFetcher
from bidpilot.models import SourceSearchResult, TenderQuerySpec


class SourceAdapter(ABC):
    name: str
    requires_auth: bool = False

    @abstractmethod
    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        raise NotImplementedError
