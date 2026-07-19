from __future__ import annotations

import httpx
import pytest

from bidpilot.config import Settings
from bidpilot.fetch import FetchError, HttpFetcher


async def test_sensitive_header_requires_explicit_source_allowlist():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok", request=request)

    async with HttpFetcher(
        Settings(request_interval=0.1),
        transport=httpx.MockTransport(handler),
    ) as fetcher:
        with pytest.raises(FetchError, match="必须声明来源域名白名单"):
            await fetcher.get(
                "https://secure.example.com/member",
                headers={"Cookie": "session=secret"},
                retries=0,
            )

    assert calls == 0


async def test_cross_domain_redirect_never_receives_authorization_material():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host or "", request.headers.get("cookie", "")))
        if request.url.host == "secure.example.com":
            return httpx.Response(
                302,
                headers={"Location": "https://evil.example.net/collect"},
                request=request,
            )
        raise AssertionError("跨域重定向目标不应收到请求")

    async with HttpFetcher(
        Settings(request_interval=0.1),
        transport=httpx.MockTransport(handler),
    ) as fetcher:
        with pytest.raises(FetchError, match="域名白名单拒绝"):
            await fetcher.get(
                "https://secure.example.com/member",
                headers={"Cookie": "session=secret"},
                authorized_hosts=("secure.example.com",),
                retries=0,
            )

    assert seen == [("secure.example.com", "session=secret")]


async def test_same_source_https_redirect_can_keep_scoped_cookie():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("cookie", "")))
        if request.url.path == "/member":
            return httpx.Response(
                302,
                headers={"Location": "/member/home"},
                request=request,
            )
        return httpx.Response(200, text="member page", request=request)

    async with HttpFetcher(
        Settings(request_interval=0.1),
        transport=httpx.MockTransport(handler),
    ) as fetcher:
        result = await fetcher.get(
            "https://secure.example.com/member",
            headers={"Cookie": "session=secret"},
            authorized_hosts=("secure.example.com",),
            retries=0,
        )

    assert result.final_url == "https://secure.example.com/member/home"
    assert seen == [
        ("/member", "session=secret"),
        ("/member/home", "session=secret"),
    ]


async def test_authorization_material_rejects_loopback_even_if_allowlisted():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    async with HttpFetcher(
        Settings(request_interval=0.1),
        transport=httpx.MockTransport(handler),
    ) as fetcher:
        with pytest.raises(FetchError, match="回环、私网或保留地址"):
            await fetcher.get(
                "https://127.0.0.1/member",
                headers={"Authorization": "Bearer secret"},
                authorized_hosts=("127.0.0.1",),
                retries=0,
            )

    assert calls == 0
