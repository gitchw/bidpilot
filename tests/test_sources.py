import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bidpilot.clean import parse_datetime
from bidpilot.config import Settings
from bidpilot.fetch import FetchedPage
from bidpilot.intent import IntentParser
from bidpilot.models import EventType, ScheduleKind, SourceSearchResult, SourceStatus
from bidpilot.pipeline import TenderPipeline
from bidpilot.sources.base import SourceAdapter
from bidpilot.sources.ccgp import CCGPSource
from bidpilot.sources.cebpubservice import CEBPubServiceSource
from bidpilot.sources.cecbid import CECBidSource
from bidpilot.sources.gdgpo import GDGPOSource
from bidpilot.sources.ggzy import GGZYSource
from bidpilot.sources.mofcom import MofcomSource
from bidpilot.sources.plap import PLAPSource
from bidpilot.sources.qianlima import QianlimaSource
from bidpilot.sources.szggzy import SZGGZYSource
from bidpilot.sources.zycg import ZYCGSource

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_cecbid_search_parser_extracts_evidence_and_event_type():
    items = CECBidSource.parse_search_page(fixture("cecbid_search.html"))
    assert len(items) == 2
    first = items[0]
    assert first.title == "安徽省某算力中心 GPU 服务器采购项目招标公告"
    assert first.region == "安徽"
    assert first.project_id == "AH-2026-001"
    assert first.event_type == EventType.TENDER
    assert first.evidence[0].source_url.endswith("ABC123?wd=服务器")
    assert items[1].event_type == EventType.AWARD


def test_ccgp_list_and_detail_parser_extract_required_fields():
    page_url = "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/index.htm"
    items = CCGPSource.parse_list_page(fixture("ccgp_list.html"), page_url, EventType.TENDER)
    assert len(items) == 2
    first = CCGPSource.parse_detail_page(fixture("ccgp_detail.html"), items[0])
    assert first.region == "安徽"
    assert first.buyer == "安徽大学"
    assert first.project_id == "AH-2026-001"
    assert "1200 万元" in first.body
    assert first.attachments[0].url == "https://www.ccgp.gov.cn/files/AH-2026-001.docx"
    assert first.evidence


def test_qianlima_public_feed_parser_is_structured():
    items = QianlimaSource.parse_search_page(fixture("qianlima_search.html"))
    assert len(items) == 1
    assert items[0].auth_level == "public_snippet"
    assert items[0].project_id == "QLM-2026-9"
    assert items[0].source_url == "https://wap.qianlima.com/zb/detail/QLM001.html"


def test_qianlima_foreground_first_page_rows_are_free_member_list_evidence():
    rows = [
        {
            "title": "高性能服务器（8卡服务器）",
            "href": "//www.qianlima.com/bid-612802321.html",
            "published_at": "2026-07-10",
            "event_label": "公告 - 招标公告",
            "region": "北京-北京",
            "category": "货物",
        },
        {
            "title": "缺失日期的行不会导入",
            "href": "//www.qianlima.com/bid-invalid.html",
            "published_at": "",
            "event_label": "公告 - 招标公告",
            "region": "北京-北京",
            "category": "货物",
        },
    ]

    items = QianlimaSource.parse_foreground_rows(rows)

    assert len(items) == 1
    assert items[0].title == "高性能服务器(8卡服务器)"
    assert items[0].source_url == "https://www.qianlima.com/bid-612802321.html"
    assert items[0].event_type == EventType.TENDER
    assert items[0].auth_level == "free_member"
    assert items[0].source_metadata["coverage"] == "bounded_free_member_list"
    assert items[0].source_metadata["detail_access"] == "blocked_by_adapter"


def test_qianlima_member_profile_persists_until_real_session_health_fails(tmp_path):
    source = QianlimaSource(Settings(data_dir=tmp_path))

    source.mark_profile("authorized")

    assert source.profile_available() is True
    assert source._read_profile_state()["expires_at"] == ""

    source.mark_profile(
        "authorized",
        expires_at=datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC")),
    )
    assert source.profile_available() is True

    source.mark_profile("expired")
    assert source.profile_available() is False


def test_qianlima_member_parser_mechanically_rejects_non_list_detail_urls():
    base = {
        "title": "服务器采购公告",
        "published_at": "2026-08-03",
        "event_label": "招标公告",
        "region": "广东",
        "category": "货物",
    }

    items = QianlimaSource.parse_foreground_rows(
        [
            {**base, "href": "https://www.qianlima.com/bid-612802321.html"},
            {**base, "href": "https://vip.qianlima.com/paid/detail/123"},
            {**base, "href": "https://evil.example/bid-612802322.html"},
        ]
    )

    assert [item.source_url for item in items] == ["https://www.qianlima.com/bid-612802321.html"]


def test_qianlima_member_budget_and_cooldown_persist_in_profile(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        qianlima_member_cooldown_seconds=30,
        qianlima_member_daily_query_budget=2,
    )
    first_process = QianlimaSource(settings)
    accepted, _, state = first_process._reserve_member_query(
        query="服务器",
        schedule_kind=ScheduleKind.IMMEDIATE,
    )
    assert accepted is True
    assert state["queries_used"] == 1
    assert state["last_query_hash"] != "服务器"

    restarted_process = QianlimaSource(settings)
    accepted, message, state = restarted_process._reserve_member_query(
        query="服务器",
        schedule_kind=ScheduleKind.IMMEDIATE,
    )

    assert accepted is False
    assert "冷却时间" in message
    assert state["queries_used"] == 1


def test_qianlima_profile_lock_serializes_web_worker_and_clear(tmp_path):
    first = QianlimaSource(Settings(data_dir=tmp_path))
    second = QianlimaSource(Settings(data_dir=tmp_path))

    first._acquire_profile_lock(wait_seconds=0)
    try:
        with pytest.raises(RuntimeError, match="另一个 Web、worker 或授权进程"):
            second._acquire_profile_lock(wait_seconds=0.05)
    finally:
        first._release_profile_lock()

    second._acquire_profile_lock(wait_seconds=0)
    second._release_profile_lock()


async def test_qianlima_monitoring_requires_explicit_server_opt_in(sample_spec, tmp_path):
    source = QianlimaSource(
        Settings(
            data_dir=tmp_path,
            qianlima_member_monitoring_enabled=False,
        )
    )
    source.mark_profile("authorized")
    sample_spec.schedule.kind = ScheduleKind.DAILY

    result = await source.search_foreground(sample_spec)

    assert result.status == SourceStatus.SKIPPED
    assert "管理员显式开启" in result.message
    assert not source.usage_state_path.exists()


def test_qianlima_monitoring_requires_an_authorization_record_reference(tmp_path):
    with pytest.raises(ValueError, match="书面授权或官方 API"):
        Settings(
            data_dir=tmp_path,
            qianlima_member_monitoring_enabled=True,
        )

    settings = Settings(
        data_dir=tmp_path,
        qianlima_member_monitoring_enabled=True,
        qianlima_member_monitoring_authorization_reference="QLM-API-2026-001",
    )
    assert settings.qianlima_member_monitoring_enabled is True
    assert settings.qianlima_member_monitoring_authorization_reference == "QLM-API-2026-001"


async def test_qianlima_member_search_reads_bounded_real_pager_and_records_audit(
    sample_spec,
    tmp_path,
):
    class CountLocator:
        async def count(self):
            return 1

    class SearchBox(CountLocator):
        async def fill(self, value):
            assert value == "服务器"

        async def press(self, key):
            assert key == "Enter"

    class PageLink:
        def __init__(self, page, number):
            self.page = page
            self.number = number

        async def count(self):
            return 1 if self.number == "2" else 0

        async def click(self):
            self.page.current_page = int(self.number)

    class Pager:
        def __init__(self, page):
            self.page = page

        def locator(self, selector):
            assert selector in {"a", "a.activP-d"}
            return self

        def filter(self, *, has_text):
            return PageLink(self.page, has_text.pattern.strip("^$"))

    class FakePage:
        url = "https://search.vip.qianlima.com/index.html#?isSearchWord=1"

        def __init__(self):
            self.current_page = 1

        def locator(self, selector):
            if selector == 'input[placeholder="请输入您要搜索的内容"]':
                return SearchBox()
            if selector == 'a[href*="vip.qianlima.com/index.html"]':
                return CountLocator()
            if selector == "#dataListPager .pagingUl":
                return Pager(self)
            raise AssertionError(f"unexpected selector: {selector}")

        def get_by_text(self, text, *, exact=False):
            assert text == "会员中心"
            assert exact is True
            return CountLocator()

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def wait_for_function(self, _expression, page_number=None, **_kwargs):
            if page_number is not None:
                assert self.current_page == page_number

        async def evaluate(self, _expression, payload):
            page_number = payload["pageNumber"]
            return [
                {
                    "title": f"第{page_number}页服务器采购公告",
                    "href": f"//www.qianlima.com/bid-61280232{page_number}.html",
                    "published_at": "2026-07-10",
                    "event_label": "公告 - 招标公告",
                    "region": "广东-深圳",
                    "category": "货物",
                    "page": str(page_number),
                }
            ]

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]
            self.closed = False

        async def close(self):
            self.closed = True

    class FakePlaywright:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    settings = Settings(
        data_dir=tmp_path,
        qianlima_member_max_pages=2,
        qianlima_member_max_results=40,
        qianlima_member_cooldown_seconds=30,
    )
    source = QianlimaSource(settings)
    source.mark_profile("authorized")
    context = FakeContext()
    playwright = FakePlaywright()

    async def launch():
        return playwright, None, context

    source.launch_search_browser = launch  # type: ignore[method-assign]

    result = await source.search_foreground(sample_spec)

    assert result.status == SourceStatus.PARTIAL
    assert result.scanned_count == 2
    assert [item.source_metadata["page"] for item in result.items] == [1, 2]
    assert all(
        item.source_metadata["paid_detail_navigation"] == "mechanically_disabled"
        for item in result.items
    )
    assert "读取 2 页、2 条" in result.message
    assert context.closed is True
    assert playwright.stopped is True
    usage = source._read_usage_state()
    assert usage["queries_used"] == 1
    assert usage["last_outcome"] == "passed"
    assert usage["last_pages_read"] == 2


def test_qianlima_auto_browser_mode_is_headless_on_linux_without_display(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    source = QianlimaSource(Settings(data_dir=tmp_path, qianlima_browser_mode="auto"))

    assert source.search_browser_headless() is True


def test_qianlima_auto_browser_mode_stays_visible_on_desktop_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")

    source = QianlimaSource(Settings(data_dir=tmp_path, qianlima_browser_mode="auto"))

    assert source.search_browser_headless() is False


def test_qianlima_visible_mode_fails_closed_without_linux_display(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    source = QianlimaSource(Settings(data_dir=tmp_path, qianlima_browser_mode="visible"))

    try:
        source.search_browser_headless()
    except RuntimeError as exc:
        assert "DISPLAY/WAYLAND_DISPLAY" in str(exc)
    else:
        raise AssertionError("visible mode must reject a Linux service without a display")


async def test_qianlima_reuses_live_browser_and_selects_the_authorized_search_page(tmp_path):
    class FakeLocator:
        def __init__(self, count):
            self.value = count

        async def count(self):
            return self.value

    class FakePage:
        def __init__(self, url, *, search_count=0, member_count=0):
            self.url = url
            self.search_count = search_count
            self.member_count = member_count
            self.goto_calls = []

        def locator(self, _selector):
            return FakeLocator(self.search_count)

        def get_by_text(self, _text, *, exact=False):
            assert exact is True
            return FakeLocator(self.member_count)

        async def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))

    class FakeContext:
        def __init__(self, pages):
            self.pages = pages

        async def new_page(self):
            raise AssertionError("已有页面时不应新建页面")

    first = FakePage("https://vip.qianlima.com/")
    authorized = FakePage(
        "https://search.vip.qianlima.com/index.html#?keywords=服务器",
        search_count=1,
        member_count=1,
    )
    context = FakeContext([first, authorized])
    source = QianlimaSource(Settings(data_dir=tmp_path))
    playwright = object()
    source.adopt_live_context(playwright, context)

    selected = await source._authorized_search_page(context)
    reused_playwright, browser, reused_context = await source.launch_authorization_browser()

    assert selected is authorized
    assert reused_playwright is playwright
    assert browser is None
    assert reused_context is context
    assert first.goto_calls == [
        (
            source.authorization_url,
            {"wait_until": "domcontentloaded", "timeout": 30_000},
        )
    ]


async def test_qianlima_public_search_never_replays_member_cookie(sample_spec, tmp_path):
    settings = Settings(max_results_per_source=1, data_dir=tmp_path)
    source = QianlimaSource(settings)
    sample_spec.event_types = [EventType.TENDER]

    class PublicFetcher:
        calls: list[tuple[str, dict]] = []

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            assert "headers" not in kwargs
            return FetchedPage(
                requested_url=url,
                final_url=url,
                status_code=200,
                text=fixture("qianlima_search.html"),
                elapsed_ms=1,
                content_type="text/html",
            )

    fetcher = PublicFetcher()
    result = await source.search(sample_spec, fetcher)

    assert result.status == SourceStatus.PARTIAL
    assert len(result.items) == 1
    assert result.items[0].auth_level == "public_snippet"
    assert fetcher.calls[0][0] == "https://wap.qianlima.com/zbgg/"
    assert "不会保存或重放会员 Cookie" in result.message


def test_cecbid_current_search_contract_and_whitespace_member_gate(sample_spec):
    source = CECBidSource(Settings())
    params = source._params(sample_spec, time_value="one_month")

    assert ("index_all", "1") in params
    assert ("index[]", "tenders") not in params
    assert ("time", "one_month") in params
    assert CECBidSource._detail_requires_auth(
        "<main>内容仅对 <strong>会员</strong> 开放 <a>立即登录</a></main>"
    )
    assert CECBidSource._is_login_page(
        '<form action="/login"><input type="password"></form>',
        "https://www.cecbid.org.cn/login",
    )


async def test_cecbid_verified_cookie_is_scoped_to_search_and_detail(sample_spec):
    settings = Settings(
        max_results_per_source=1,
        cecbid_cookie="member_session=secret",
    )
    source = CECBidSource(settings)
    sample_spec.topic = "购买"
    sample_spec.keywords = ["购买"]

    class AuthorizedFetcher:
        calls: list[tuple[str, dict]] = []

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            assert kwargs["headers"]["Cookie"] == "member_session=secret"
            assert kwargs["authorized_hosts"] == (
                "www.cecbid.org.cn",
                "cecbid.org.cn",
            )
            if url.endswith("/search"):
                assert ("index_all", "1") in kwargs["params"]
                assert ("index[]", "tenders") not in kwargs["params"]
                return FetchedPage(
                    requested_url=url,
                    final_url="https://www.cecbid.org.cn/search?wd=购买",
                    status_code=200,
                    text=fixture("cecbid_search.html"),
                    elapsed_ms=1,
                    content_type="text/html",
                )
            return FetchedPage(
                requested_url=url,
                final_url=url,
                status_code=200,
                text=(
                    "<html><article><p>"
                    + "真实会员正文，包含采购需求、资格条件、时间安排与联系人信息。" * 12
                    + "</p><a href='/files/spec.pdf'>采购文件</a></article></html>"
                ),
                elapsed_ms=1,
                content_type="text/html",
            )

    fetcher = AuthorizedFetcher()
    result = await source.search(sample_spec, fetcher)

    assert result.status == SourceStatus.OK
    assert len(result.items) == 1
    assert result.items[0].auth_level == "free_member"
    assert len(fetcher.calls) == 2


async def test_cecbid_member_gate_marks_verified_session_as_auth_required(sample_spec):
    settings = Settings(max_results_per_source=1, cecbid_cookie="member_session=expired")
    source = CECBidSource(settings)

    class GatedFetcher:
        async def get(self, url, **kwargs):
            if url.endswith("/search"):
                return FetchedPage(
                    requested_url=url,
                    final_url=url,
                    status_code=200,
                    text=fixture("cecbid_search.html"),
                    elapsed_ms=1,
                    content_type="text/html",
                )
            return FetchedPage(
                requested_url=url,
                final_url=url,
                status_code=200,
                text="<main>内容仅对 <b>会员</b> 开放，请立即登录后查看</main>",
                elapsed_ms=1,
                content_type="text/html",
            )

    result = await source.search(sample_spec, GatedFetcher())

    assert result.status == SourceStatus.AUTH_REQUIRED
    assert result.items[0].auth_level == "public_snippet"


def test_malformed_source_date_uses_old_sentinel_instead_of_today():
    assert parse_datetime("网页没有发布日期") == datetime(1970, 1, 1)


async def test_pipeline_preserves_all_skipped_status(sample_spec):
    class SkippedSource(SourceAdapter):
        source_id = "skipped_fixture"
        name = "地域跳过来源"

        async def search(self, spec, fetcher):
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                message="本轮地域不适用",
            )

    result = await TenderPipeline(
        Settings(retrieval_llm_mode="off", request_interval=0.1),
        [SkippedSource()],
    ).run(sample_spec)

    assert result.diagnostics[0].status == SourceStatus.SKIPPED


def test_ccgp_prefilter_respects_region_topic_and_date(sample_spec):
    page_url = "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/index.htm"
    items = CCGPSource.parse_list_page(fixture("ccgp_list.html"), page_url, EventType.TENDER)
    assert CCGPSource._matches_spec(items[0], sample_spec)
    assert not CCGPSource._matches_spec(items[1], sample_spec)


def test_city_query_keeps_province_labeled_candidate_for_detail_filtering():
    page_url = "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/index.htm"
    item = CCGPSource.parse_list_page(fixture("ccgp_list.html"), page_url, EventType.TENDER)[
        0
    ].model_copy(
        update={
            "region": "广东",
            "title": "深圳市公共充电桩建设项目招标公告",
        }
    )
    spec = IntentParser().parse(
        "最近1个月深圳充电桩招标信息",
        now=datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert spec.region_level == "city"
    assert CCGPSource._matches_spec(item, spec)


def test_mofcom_json_and_detail_parsers_preserve_official_evidence():
    payload = {
        "rows": [
            {
                "name": "合肥先进光源服务器设备 - 国际招标公告",
                "publishTime": "2026-07-17",
                "areaName": "中国安徽省",
                "filePath": "/bidding/bulletin/202607/abc.html",
                "digest": "招标项目编号: 0729-264OIT321025，采购服务器2台",
                "industryName": "计算机设备",
                "capitalSourceName": "现汇",
                "fdid": "abc",
            }
        ]
    }
    item = MofcomSource.parse_search_response(payload, EventType.TENDER)[0]
    assert item.region == "安徽"
    assert item.source_url.endswith("/bidDetail/bidding/bulletin/202607/abc.html")
    detail = """
    <div class="article"><p>招标项目编号:0729-264OIT321025<br>
    招标人:合肥先进光源研究院<br>投标截止时间:2026-08-07 10:00</p>
    <a href="/files/spec.xls">附件</a></div>
    """
    item = MofcomSource.parse_detail_page(detail, item)
    assert item.buyer == "合肥先进光源研究院"
    assert item.attachments[0].url.endswith("/files/spec.xls")
    assert item.evidence


def test_ggzy_home_feed_and_detail_parsers_are_honest_about_region():
    home = """
    <div class="main_list_on"><h4>交易公告</h4><ul><li>
    <a href="/information/deal/html/a/340000/0201/20260717/abc.html">
    安徽大学 GPU 服务器采购公告</a><span>2026-07-17</span>
    </li></ul></div>
    <div class="main_list_on main_list_tw"><h4>成交公示</h4><ul><li>
    <a href="/information/deal/html/a/310000/0202/20260717/def.html">
    上海服务器采购成交公告</a><span>2026-07-17</span>
    </li></ul></div>
    """
    items = GGZYSource.parse_home_feed(home)
    assert items[0].region == "安徽"
    assert items[0].event_type == EventType.TENDER
    assert items[1].event_type == EventType.AWARD
    detail = """
    <div class="detail"><h4 class="h4_o">安徽大学 GPU 服务器采购公告</h4>
    <div class="detail_content"><p>项目编号：AH-2026-001。预算金额：1200万元。</p></div></div>
    """
    parsed = GGZYSource.parse_detail_page(detail, items[0])
    assert parsed.project_id == "AH-2026-001"
    assert "预算金额" in parsed.body


def test_zycg_public_json_parser_preserves_central_procurement_evidence():
    payload = json.loads(fixture("zycg_search.json"))
    item = ZYCGSource.parse_response(payload)[0]
    assert item.source == "中央政府采购网"
    assert item.project_id == "GC-HGX260001"
    assert item.buyer == "中国科学院某研究所"
    assert item.event_type == EventType.AWARD
    assert item.attachments[0].url == "https://www.zycg.gov.cn/files/zycg-001.pdf"
    assert item.source_url.endswith("zycg-001.html?id=zycg-001")


def test_plap_public_api_parser_extracts_full_content_without_login():
    payload = json.loads(fixture("plap_search.json"))
    item = PLAPSource.parse_response(payload)[0]
    assert item.source == "军队采购网"
    assert item.region == "北京"
    assert item.project_id == "2026-JQ06-W3077"
    assert item.event_type == EventType.INTENTION
    assert item.buyer == "某单位"
    assert "45万元" in item.body
    assert item.attachments[0].url == "https://www.plap.mil.cn/files/plap-001.docx"


def test_ceb_public_list_parser_keeps_bulletin_id_region_and_publisher():
    items = CEBPubServiceSource.parse_list_page(
        fixture("cebpubservice_search.html"), EventType.TENDER
    )
    assert len(items) == 2
    first = items[0]
    assert first.region == "北京"
    assert first.event_type == EventType.TENDER
    assert "uuid=ceb-uuid-001" in first.source_url
    assert first.source_metadata["industry"] == "信息电子"
    assert "中国电信阳光采购网" in first.body
    assert items[1].event_type == EventType.AWARD


def test_szggzy_public_api_parser_and_detail_preserve_local_evidence():
    page = json.loads(fixture("szggzy_page.json"))
    item = SZGGZYSource.parse_page_response(page)[0]
    assert item.source == "深圳公共资源交易中心"
    assert item.region == "深圳"
    assert item.buyer == "深圳市住房和建设局"
    assert item.project_id == "SZZXDL-2026-00668"
    assert item.event_type == EventType.AWARD
    assert "contentId=20359755" in item.source_url

    detailed = SZGGZYSource.apply_detail(
        item,
        json.loads(fixture("szggzy_detail.json")),
    )
    assert "成交供应商" in detailed.body
    assert detailed.source_metadata["detail_loaded"] is True
    assert detailed.attachments[0].url == "https://www.szggzy.com/upload/result.pdf"


def test_szggzy_replaces_retired_deep_link_with_current_public_detail_route():
    payload = json.loads(fixture("szggzy_page.json"))
    payload["data"]["content"][0]["linkTo"] = "http://zfcg.szggzy.com:8081/gsgg/retired-entry.html"

    item = SZGGZYSource.parse_page_response(payload)[0]

    assert item.source_url == (
        "https://www.szggzy.com/jygg/details.html?contentId=20359755&channelId=2850"
    )


def test_gdgpo_public_fulltext_parser_and_detail_preserve_official_evidence():
    payload = json.loads(fixture("gdgpo_search.json"))
    item = GDGPOSource.parse_search_response(payload)[0]

    assert item.source == "广东省政府采购网"
    assert item.title == "惠州市算力中心服务器采购项目结果公告"
    assert item.buyer == "惠州市政务服务和数据管理局"
    assert item.project_id == "HZBY-2026A002"
    assert item.event_type == EventType.AWARD
    assert item.source_url.startswith(
        "https://gdgpo.czt.gd.gov.cn/gpcms/rest/web/v2/info/getInfoById?"
    )
    assert "id=19249ba9-5d33-48e4-b9cb-def134be8824" in item.source_url

    detailed = GDGPOSource.apply_detail(item, json.loads(fixture("gdgpo_detail.json")))
    assert "计算服务器、存储设备" in detailed.body
    assert detailed.source_metadata["detail_loaded"] is True
    assert detailed.attachments[0].name == "报价明细附件.pdf"
    assert detailed.attachments[0].url.endswith("server-result.pdf")


def test_gdgpo_region_router_only_runs_for_guangdong_scope(sample_spec):
    sample_spec.region = "北京"
    assert GDGPOSource._is_relevant_region(sample_spec) is False
    sample_spec.region = "广东"
    assert GDGPOSource._is_relevant_region(sample_spec) is True
    sample_spec.region = "深圳"
    assert GDGPOSource._is_relevant_region(sample_spec) is True
    assert GDGPOSource._region_code(sample_spec) == "440300"


async def test_gdgpo_skips_unrelated_region_without_network(sample_spec):
    sample_spec.region = "北京"

    class NoNetwork:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("地域跳过不应发出网络请求")

    result = await GDGPOSource(Settings()).search(sample_spec, NoNetwork())

    assert result.status.value == "skipped"
    assert result.scanned_count == 0
    assert "地域路由跳过" in result.message
