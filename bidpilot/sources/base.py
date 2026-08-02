from __future__ import annotations

from abc import ABC, abstractmethod

from bidpilot.fetch import HttpFetcher
from bidpilot.models import SourceSearchResult, TenderQuerySpec


class SourceAdapter(ABC):
    source_id: str = "unknown"
    name: str
    requires_auth: bool = False
    official: bool = False
    homepage: str = ""
    access_mode: str = "public"
    query_mode: str = "search"
    supports_query_variants: bool = False
    supports_region_filter: bool = False
    supports_date_filter: bool = False
    supports_pagination: bool = False
    supports_detail: bool = True
    authorization_supported: bool = False
    authorization_url: str = ""
    authorization_action_label: str = "打开原站工作台 ↗"
    coverage_note: str = ""

    def capabilities(self) -> dict[str, object]:
        """Return non-secret source capabilities for planning and the source center."""
        return {
            "id": self.source_id,
            "name": self.name,
            "official": self.official,
            "homepage": self.homepage,
            "access_mode": self.access_mode,
            "query_mode": self.query_mode,
            "supports_query_variants": self.supports_query_variants,
            "supports_region_filter": self.supports_region_filter,
            "supports_date_filter": self.supports_date_filter,
            "supports_pagination": self.supports_pagination,
            "supports_detail": self.supports_detail,
            "requires_auth": self.requires_auth,
            "authorization_supported": self.authorization_supported,
            "authorization_action_label": self.authorization_action_label,
            "coverage_note": self.coverage_note,
        }

    @abstractmethod
    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        raise NotImplementedError
