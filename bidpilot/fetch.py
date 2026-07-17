from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from bidpilot.config import Settings


class FetchError(RuntimeError):
    pass


@dataclass(slots=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    text: str
    elapsed_ms: int
    content_type: str


class HttpFetcher:
    """Rate-limited async HTTP client with bounded retries and explicit diagnostics."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"},
            transport=transport,
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = defaultdict(float)

    async def __aenter__(self) -> HttpFetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
        retries: int = 2,
    ) -> FetchedPage:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            encoding=encoding,
            retries=retries,
        )

    async def post_form(
        self,
        url: str,
        *,
        data: dict[str, Any] | list[tuple[str, Any]],
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
        retries: int = 2,
    ) -> FetchedPage:
        return await self.request(
            "POST",
            url,
            data=data,
            headers=headers,
            encoding=encoding,
            retries=retries,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        data: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
        retries: int = 2,
    ) -> FetchedPage:
        host = urlparse(url).netloc.lower()
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self._locks[host]:
                    wait_for = self.settings.request_interval - (
                        time.monotonic() - self._last_request[host]
                    )
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                    started = time.perf_counter()
                    response = await self._client.request(
                        method, url, params=params, data=data, headers=headers
                    )
                    self._last_request[host] = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise FetchError(f"HTTP {response.status_code}")
                response.raise_for_status()
                if encoding:
                    response.encoding = encoding
                elif host.endswith("ccgp.gov.cn"):
                    # CCGP pages are UTF-8 but several endpoints omit charset.
                    response.encoding = "utf-8"
                elapsed = int((time.perf_counter() - started) * 1000)
                return FetchedPage(
                    requested_url=str(response.request.url),
                    final_url=str(response.url),
                    status_code=response.status_code,
                    text=response.text,
                    elapsed_ms=elapsed,
                    content_type=response.headers.get("content-type", ""),
                )
            except (httpx.HTTPError, FetchError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(0.4 * (2**attempt))
        raise FetchError(f"抓取失败 {url}: {last_error}") from last_error
