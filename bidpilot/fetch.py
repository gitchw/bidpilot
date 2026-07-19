from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

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
            follow_redirects=False,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"},
            transport=transport,
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = defaultdict(float)

    _SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
    _REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
    _MAX_REDIRECTS = 5

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
        authorized_hosts: Collection[str] | None = None,
    ) -> FetchedPage:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            encoding=encoding,
            retries=retries,
            authorized_hosts=authorized_hosts,
        )

    async def post_form(
        self,
        url: str,
        *,
        data: dict[str, Any] | list[tuple[str, Any]],
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
        retries: int = 2,
        authorized_hosts: Collection[str] | None = None,
    ) -> FetchedPage:
        return await self.request(
            "POST",
            url,
            data=data,
            headers=headers,
            encoding=encoding,
            retries=retries,
            authorized_hosts=authorized_hosts,
        )

    async def post_json(
        self,
        url: str,
        *,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
        retries: int = 2,
        authorized_hosts: Collection[str] | None = None,
    ) -> FetchedPage:
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        return await self.request(
            "POST",
            url,
            content=json.dumps(json_body, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            encoding=encoding,
            retries=retries,
            authorized_hosts=authorized_hosts,
        )

    @classmethod
    def _contains_authorization(cls, headers: dict[str, str] | None) -> bool:
        return bool(headers) and any(key.lower() in cls._SENSITIVE_HEADERS for key in headers)

    @staticmethod
    def _normalize_authorized_hosts(hosts: Collection[str] | None) -> frozenset[str]:
        return frozenset(host.strip().lower().rstrip(".") for host in (hosts or ()) if host.strip())

    @classmethod
    def _validate_authorized_url(cls, url: str, allowed_hosts: frozenset[str]) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise FetchError("授权请求地址包含非法端口") from exc
        if (
            parsed.scheme.lower() != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            raise FetchError("授权材料只允许发送到标准 HTTPS 来源地址")
        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts):
            raise FetchError("授权请求被来源域名白名单拒绝")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise FetchError("授权材料不得发送到回环、私网或保留地址")

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None,
        data: dict[str, Any] | list[tuple[str, Any]] | None,
        content: bytes | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        host = urlparse(url).netloc.lower()
        async with self._locks[host]:
            wait_for = self.settings.request_interval - (
                time.monotonic() - self._last_request[host]
            )
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            response = await self._client.request(
                method,
                url,
                params=params,
                data=data,
                content=content,
                headers=headers,
                follow_redirects=False,
            )
            self._last_request[host] = time.monotonic()
            return response

    async def _send_with_redirects(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None,
        data: dict[str, Any] | list[tuple[str, Any]] | None,
        content: bytes | None,
        headers: dict[str, str] | None,
        allowed_hosts: frozenset[str],
    ) -> httpx.Response:
        sensitive = self._contains_authorization(headers)
        if sensitive and not allowed_hosts:
            raise FetchError("携带 Cookie 或 Authorization 时必须声明来源域名白名单")
        current_method = method
        current_url = url
        current_params = params
        current_data = data
        current_content = content
        for redirect_count in range(self._MAX_REDIRECTS + 1):
            if sensitive:
                self._validate_authorized_url(current_url, allowed_hosts)
            response = await self._send_once(
                current_method,
                current_url,
                params=current_params,
                data=current_data,
                content=current_content,
                headers=headers,
            )
            if response.status_code not in self._REDIRECT_CODES:
                return response
            if redirect_count >= self._MAX_REDIRECTS:
                raise FetchError("重定向次数超过安全上限")
            location = response.headers.get("location")
            if not location:
                return response
            target = urljoin(str(response.url), location)
            if sensitive:
                self._validate_authorized_url(target, allowed_hosts)
            current_url = target
            current_params = None
            if response.status_code == 303 or (
                response.status_code in {301, 302} and current_method.upper() == "POST"
            ):
                current_method = "GET"
                current_data = None
                current_content = None
        raise FetchError("重定向次数超过安全上限")

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        data: dict[str, Any] | list[tuple[str, Any]] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
        retries: int = 2,
        authorized_hosts: Collection[str] | None = None,
    ) -> FetchedPage:
        allowed_hosts = self._normalize_authorized_hosts(authorized_hosts)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                started = time.perf_counter()
                response = await self._send_with_redirects(
                    method,
                    url,
                    params=params,
                    data=data,
                    content=content,
                    headers=headers,
                    allowed_hosts=allowed_hosts,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise FetchError(f"HTTP {response.status_code}")
                response.raise_for_status()
                if encoding:
                    response.encoding = encoding
                elif (response.url.host or "").endswith("ccgp.gov.cn"):
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
