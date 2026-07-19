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
        "function channelOptions", 1
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
