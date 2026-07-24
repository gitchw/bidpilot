from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from bidpilot.runtime_config import RUNTIME_FIELDS, SECRET_FIELDS


def _document() -> BeautifulSoup:
    return BeautifulSoup(
        Path("bidpilot/static/index.html").read_text(encoding="utf-8"),
        "lxml",
    )


def test_graphical_config_fields_match_backend_allowlist_exactly():
    document = _document()
    controls = document.select("[data-config]")
    fields = [control.get("data-config") for control in controls]
    clear_fields = [control.get("data-clear") for control in document.select("[data-clear]")]

    assert len(fields) == len(set(fields))
    assert set(fields) == RUNTIME_FIELDS
    assert len(clear_fields) == len(set(clear_fields))
    assert set(clear_fields) == SECRET_FIELDS


def test_config_cards_have_scoped_save_and_safe_initial_state():
    document = _document()
    groups = {card.get("data-config-group") for card in document.select("[data-config-group]")}
    save_groups = {
        button.get("data-config-save-group")
        for button in document.select("[data-config-save-group]")
    }

    assert groups == {
        "ai",
        "retrieval",
        "system",
        "feishu",
        "email",
        "dingtalk",
        "wecom",
        "generic",
        "telegram",
        "slack",
    }
    assert save_groups == groups
    assert document.select_one("#save-config").has_attr("disabled")
    assert all(button.has_attr("disabled") for button in document.select(".save-config-card"))
    assert all(button.has_attr("disabled") for button in document.select(".channel-test"))


def test_connection_tests_do_not_implicitly_save_other_drafts():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")
    model_block = script.split("async function testModelConnection()", 1)[1].split(
        "function channelTestGroup", 1
    )[0]
    channel_block = script.split("async function testDeliveryChannel(button)", 1)[1].split(
        "function deliveryTargetSummaryMarkup", 1
    )[0]

    assert "saveConfig(" not in model_block
    assert "saveConfig(" not in channel_block
    assert "有未保存修改" in model_block
    assert "有未保存修改" in channel_block


def test_network_card_exposes_safe_and_frictionless_lan_modes():
    document = _document()
    policy = document.select_one('[data-config="lan_access_policy"]')

    assert {option.get("value") for option in policy.select("option")} == {
        "admin_token",
        "trusted_lan",
    }
    assert document.select_one('[data-config="lan_trusted_networks"]') is not None
    assert document.select_one("#generate-lan-token") is not None
    assert document.select_one("#network-effective-state") is not None
    assert document.select_one("#lan-unlock-token") is not None
    assert document.select_one("#lan-unlock-submit") is not None
    system_text = document.select_one('[data-config-group="system"]').get_text(" ", strip=True)
    assert "登录窗口会在运行 BidPilot 的电脑上打开" in system_text
    assert "不要把 8000 端口直接暴露到公网" in system_text

    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")
    assert "effective_access_policy" in script
    assert "pending_restart" in script
    assert "window.crypto.getRandomValues" in script
    assert "requestLanAdminToken" in script
    assert "window.prompt" not in script


def test_telegram_and_slack_cards_expose_masked_scoped_configuration():
    document = _document()
    telegram = document.select_one('[data-config-group="telegram"]')
    slack = document.select_one('[data-config-group="slack"]')

    assert telegram is not None
    assert slack is not None
    assert telegram.select_one('[data-config="telegram_bot_token"][type="password"]')
    assert telegram.select_one('[data-config="telegram_chat_id"]')
    assert telegram.select_one('[data-config="telegram_message_thread_id"][type="number"]')
    assert telegram.select_one('[data-config="telegram_disable_notification"][type="checkbox"]')
    assert telegram.select_one('[data-config="telegram_protect_content"][type="checkbox"]')
    assert telegram.select_one('[data-clear="telegram_bot_token"]')
    assert telegram.select_one('[data-test-channel="telegram_bot"]')
    assert slack.select_one('[data-config="slack_webhook_url"][type="password"]')
    assert slack.select_one('[data-clear="slack_webhook_url"]')
    assert slack.select_one('[data-test-channel="slack_webhook"]')
    assert "超过 50 MB" in telegram.get_text(" ", strip=True)
    assert "不能上传 Word" in slack.get_text(" ", strip=True)


def test_multitarget_ui_covers_all_creation_and_editing_flows():
    document = _document()
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    picker = document.select_one("#run-delivery-targets")
    assert picker is not None
    assert picker.select_one('#delivery-channel[type="hidden"]') is not None
    assert "function deliveryTargetPickerMarkup" in script
    assert "supports_text" in script
    assert "supports_file" in script
    assert "supports_link" in script
    assert "configuration_group" in script
    assert script.count("...deliveryPayload(targets)") >= 3
    assert "Object.assign(payload, deliveryPayload(targets))" in script
    assert 'root.id === "run-delivery-targets" ? "delivery-channel"' in script
    assert "buyer-monitor-targets" in script
    assert "subscription-target-editor" in script
    assert "row.delivery_targets" in script
    assert "delivery_channel: event.target.value" not in script
    assert "至少需要保留一个交付目标" in script
    assert "去配置" in script


def test_delivery_receipts_outbox_and_dead_letter_recovery_are_visible():
    document = _document()
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert document.select_one("#delivery-receipts") is not None
    assert "run?.delivery_receipts" in script
    assert "/delivery-outbox?limit=100" in script
    assert "/api/v1/delivery-outbox/${encodeURIComponent(outboxId)}/retry" in script
    assert "只重试此渠道" in script
    assert "subscriptionExpandedLogs" in script
    assert ".subscription-editor:not(.hidden), .subscription-log:not(.hidden)" in script
    assert 'row.last_status === "partial"' in script


def test_frontend_cache_key_and_mobile_recovery_contract_are_current():
    document = _document()
    stylesheet = document.select_one('link[rel="stylesheet"]')
    script_asset = document.select_one("script[src]")
    css = Path("bidpilot/static/app.css").read_text(encoding="utf-8")
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert stylesheet.get("href").endswith("?v=0.8.0")
    assert script_asset.get("src").endswith("?v=0.8.0")
    assert "min-height:44px" in css
    assert ".delivery-outbox-row p" in css
    assert "white-space:normal" in css
    assert "opportunityLoadSequence" in script
