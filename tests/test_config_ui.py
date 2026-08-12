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


def test_every_graphical_config_field_has_beginner_help_text():
    document = _document()
    missing = []
    for control in document.select("[data-config]"):
        label = control.find_parent("label")
        help_text = label.select_one("small") if label else None
        minimum_length = 1 if control.get("type") == "password" else 8
        if help_text is None or len(help_text.get_text(" ", strip=True)) < minimum_length:
            missing.append(control.get("data-config"))

    assert missing == []
    assert "范围 0.2～300 秒" in document.select_one(
        '[data-config="worker_poll_interval"]'
    ).find_parent("label").get_text(" ", strip=True)
    assert "过短可能在机器繁忙时误报" in document.select_one(
        '[data-config="worker_heartbeat_ttl"]'
    ).find_parent("label").get_text(" ", strip=True)
    assert "重复风险" in document.select_one(
        '[data-config="delivery_webhook_timeout"]'
    ).find_parent("label").get_text(" ", strip=True)


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
        "publishing",
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


def test_config_save_locks_inputs_and_unready_tests_stay_disabled():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert "function syncConfigInputAvailability()" in script
    assert "const locked = !state.configLoaded || state.configSaving" in script
    assert "输入已暂时锁定" in script
    assert "function savedDeliveryChannelReady(channel)" in script
    assert "!state.configLoaded || !ready || dirtyConfigFields(group).length" in script
    assert "请先完整配置并保存此渠道" in script


def test_network_card_exposes_safe_and_frictionless_lan_modes():
    document = _document()
    access_mode = document.select_one('[data-config="network_access_mode"]')
    policy = document.select_one('[data-config="lan_access_policy"]')

    assert {option.get("value") for option in access_mode.select("option")} == {
        "local",
        "lan",
        "enterprise",
    }
    assert {option.get("value") for option in policy.select("option")} == {
        "admin_token",
        "trusted_lan",
    }
    assert document.select_one('[data-config="lan_trusted_networks"]') is not None
    assert document.select_one('[data-config="trusted_proxy_networks"]') is not None
    assert document.select_one('[data-config="enterprise_allowed_origins"]') is not None
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
    assert "configuration_locked" in script
    assert "NETWORK_SECURITY_FIELDS" in script
    assert "服务器环境锁定" in script
    assert "window.crypto.getRandomValues" in script
    assert "requestLanAdminToken" in script
    assert "window.prompt" not in script


def test_service_port_help_distinguishes_native_python_from_compose():
    document = _document()
    port_control = document.select_one('[data-config="port"]')
    port_help = port_control.find_parent("label").get_text(" ", strip=True)

    assert "原生 Python 服务" in port_help
    assert "Docker Compose" in port_help
    assert "容器内固定监听 8000" in port_help
    assert "BIDPILOT_PORT" in port_help
    assert "这里不会改变端口" in port_help

    readme = Path("README.md").read_text(encoding="utf-8")
    configuration_guide = Path("docs/CONFIGURATION_GUIDE.md").read_text(encoding="utf-8")

    for documentation in (readme, configuration_guide):
        assert "BIDPILOT_PORT=8012" in documentation
        assert "docker compose up -d --force-recreate" in documentation
        assert "docker compose stop" in documentation
        assert "docker compose restart" in documentation
        assert ".venv\\Scripts\\python.exe -m bidpilot restart" in documentation
        assert ".venv\\Scripts\\python.exe -m bidpilot stop" in documentation
        assert ".venv/bin/python -m bidpilot restart" in documentation
        assert ".venv/bin/python -m bidpilot stop" in documentation

    assert "网页“服务端口”只控制原生 Python `bidpilot serve`" in readme
    assert "容器内 Web 服务固定监听 `8000`" in readme
    assert "网页修改“服务端口”不会修改 `.env`、`compose.yaml` 或已创建容器" in (configuration_guide)
    assert "容器内部仍然监听 8000" in configuration_guide


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
    publishing = document.select_one('[data-config-group="publishing"]')
    assert publishing.select_one('[data-config="public_base_url"]')
    assert len(document.select('[data-config="public_base_url"]')) == 1
    assert document.select('[data-open-config-group="publishing"]')


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
    assert script.count("...deliveryPayload(targets)") >= 2
    assert "...deliveryPayload(currentTargets)" in script
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
    assert "retry-run-outbox" in script
    assert "DELIVERY CONTROL TOWER" in script
    assert "当前没有死信" in script
    assert "subscriptionExpandedLogs" in script
    assert ".subscription-editor:not(.hidden), .subscription-log:not(.hidden)" in script
    assert 'row.last_delivery_status === "partial"' in script
    assert 'run.delivery_status === "partial"' in script
    assert "function refreshRunDeliveryStatus" in script
    assert "function scheduleRunDeliveryPolling" in script
    assert "function scheduleSubscriptionLogPolling" in script
    assert "refresh-run-delivery" in script
    assert "refresh-subscription-outbox" in script
    assert "每 4 秒自动刷新" in script
    assert "api(`/api/v1/runs/${encodeURIComponent(runId)}`" in script
    assert "后台任务不会因此丢失" in script


def test_errors_and_subscription_drafts_have_beginner_recovery_guards():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert "function humanFieldPath" in script
    assert "function humanValidationMessage" in script
    assert "humanizeErrorMessage(data.detail)" in script
    assert "subscriptionDrafts: new Set()" in script
    assert "尚未保存；离开或重新打开会丢弃这些草稿" in script
    assert "state.configDirty.size || state.subscriptionDrafts.size" in script
    assert "任务仍保存在后台，请到自动订阅点击“刷新状态”" in script


def test_confirmed_intent_snapshot_follows_run_and_subscription_requests():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert "intent_snapshot: state.spec.confirmation_snapshot" in script
    assert "intent_snapshot: spec.confirmation_snapshot" in script
    assert "function parseScheduledIntentForConfirmation" in script
    assert "确认前不会创建或立即运行" in script
    assert "确认前不会保存或立即运行" in script
    assert "保存前会先展示主题、地域、时间和计划供你确认" in script
    assert "intent_snapshot: confirmedIntent.confirmation_snapshot" in script
    assert "payload.intent_snapshot = confirmedIntent.confirmation_snapshot" in script
    assert "更新订阅规则" in script
    assert "error.status === 409" in script
    assert "创建订阅前请重新解析" in script


def test_primary_action_routes_all_future_and_recurring_intents_to_subscription_creation():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")
    scheduled_block = script.split("const SCHEDULED_INTENT_KINDS", 1)[1].split(";", 1)[0]
    run_block = script.split("async function runQuery()", 1)[1].split(
        "function hasActiveDeliveryRows", 1
    )[0]

    assert '["once", "daily", "weekly", "monthly"]' in scheduled_block
    assert "if (isScheduledIntent(state.spec))" in run_block
    assert "await createSubscriptionFromQuery();" in run_block
    assert run_block.index("await createSubscriptionFromQuery();") < run_block.index(
        'api("/api/v1/runs"'
    )
    assert (
        'label.textContent = isScheduledIntent(spec) ? "创建计划任务" '
        ": PRIMARY_ACTION_DEFAULT_LABEL"
    ) in script
    assert 'isScheduledIntent(spec) ? "正在创建计划任务…" : "多源采集中…"' in script


def test_frontend_cache_key_and_mobile_recovery_contract_are_current():
    document = _document()
    stylesheet = document.select_one('link[rel="stylesheet"]')
    script_asset = document.select_one("script[src]")
    css = Path("bidpilot/static/app.css").read_text(encoding="utf-8")
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert stylesheet.get("href").endswith("?v=0.8.0-20260811")
    assert script_asset.get("src").endswith("?v=0.8.0-20260811")
    assert "min-height:44px" in css
    assert ".checkbox-config > .config-origin { grid-column:1/-1; }" in css
    assert "@media (max-width: 1660px)" in css
    assert ".delivery-outbox-row p" in css
    assert "white-space:normal" in css
    assert "opportunityLoadSequence" in script


def test_primary_navigation_and_home_return_are_keyboard_accessible():
    document = _document()
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")
    stylesheet = Path("bidpilot/static/app.css").read_text(encoding="utf-8")

    navigation = document.select_one('nav[role="tablist"]')
    tabs = navigation.select('[role="tab"][aria-controls]')
    panels = document.select('.tab-panel[role="tabpanel"][aria-labelledby]')
    assert len(tabs) == 7
    assert len(panels) == 7
    assert tabs[0]["aria-selected"] == "true"
    assert all(tab["aria-controls"] == f"tab-{tab['data-tab']}" for tab in tabs)
    assert len(document.select("[data-home-tab]")) == 3
    assert 'keys = ["ArrowLeft", "ArrowRight", "Home", "End"]' in script
    assert 'node.setAttribute("aria-selected", String(active))' in script
    assert "node.tabIndex = active ? 0 : -1" in script
    assert "activeNavigationItem.scrollIntoView" in script
    assert '$("#home-report-count")' in script
    assert '$("#home-opportunity-count")' in script
    assert '$("#home-subscription-count")' in script
    assert "overflow-x:auto" in stylesheet
    assert "scrollbar-width:none" in stylesheet
    assert "overflow-wrap:anywhere" in stylesheet
    assert "scroll-margin-top:92px" in stylesheet
    assert "font-size:12px" in stylesheet
    assert "prefers-reduced-motion: reduce" in stylesheet


def test_results_use_plain_business_language_for_progressive_detail():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert "本轮行动建议" in script
    assert "<h3>情报简报</h3>" in script
    assert "检索与筛选记录" in script
    assert "<b>查询计划</b>" in script
    assert "<b>边界复核</b>" in script
    assert "EVIDENCE-GROUNDED COPILOT" not in script
    assert "AUDITABLE AI RETRIEVAL" not in script


def test_result_cards_disclose_source_names_and_access_level():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert "item.sources?.[i] || `来源 ${i + 1}`" in script
    assert 'item.auth_level === "free_member" ? "免费会员列表可见" : "公开信息"' in script
    assert 'class="result-evidence-id"' in script
    assert 'aria-label="机会分 ${Math.round(item.opportunity_score)} 分"' in script


def test_source_summary_separates_adapter_count_login_and_real_contribution():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")

    assert '["已接入来源", rows.length]' in script
    assert '["登录增强已验证", memberVerified]' in script
    assert '["最近实际有产出", contributed]' in script
    assert '["当前可运行", configured]' not in script


def test_opportunity_workspace_exposes_and_saves_an_explicit_next_action():
    document = _document()
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")
    stylesheet = Path("bidpilot/static/app.css").read_text(encoding="utf-8")

    guide = document.select_one(".opportunity-guide").get_text(" ", strip=True)
    assert "下一步做什么" in guide
    assert "谁负责" in guide
    assert 'class="opportunity-next-action-input"' in script
    assert (
        'next_action: card.querySelector(".opportunity-next-action-input").value.trim()' in script
    )
    assert "待明确具体动作" in script
    assert ".opportunity-next .opportunity-next-action" in stylesheet


def test_main_navigation_resets_scroll_before_and_after_async_panel_load():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")
    reset_block = script.split("function resetPageScroll()", 1)[1].split(
        "function waitForLayout", 1
    )[0]
    wait_block = script.split("function waitForLayout()", 1)[1].split(
        "function revealConfigCard", 1
    )[0]
    activate_block = script.split("async function activateTab(tab)", 1)[1].split(
        '\n}\n\n$$(".nav-link")', 1
    )[0]

    assert 'behavior: "instant"' in reset_block
    assert "window.setTimeout(resolve, 0)" in wait_block
    assert "requestAnimationFrame" not in wait_block
    assert activate_block.count("resetPageScroll();") == 2
    assert "await waitForLayout();" in activate_block
    assert "return false;" in activate_block
    assert "return true;" in activate_block


def test_delivery_config_links_reveal_and_focus_the_requested_card():
    script = Path("bidpilot/static/app.js").read_text(encoding="utf-8")
    reveal_block = script.split("function revealConfigCard(card)", 1)[1].split(
        "async function openDeliveryConfiguration", 1
    )[0]
    open_block = script.split("async function openDeliveryConfiguration(group)", 1)[1].split(
        "function bindDeliveryTargetPicker", 1
    )[0]

    assert "targetTop" in reveal_block
    assert 'behavior: "instant"' in reveal_block
    assert 'card.setAttribute("tabindex", "-1")' in reveal_block
    assert "card.focus({ preventScroll: true });" in reveal_block
    assert 'card.classList.add("attention-card")' in reveal_block
    assert 'const activated = await activateTab("config");' in open_block
    assert "if (!activated) return;" in open_block
    assert "await waitForLayout();" in open_block
    assert "await loadConfig(false);" not in open_block
    assert 'article.config-card[data-config-group="${group}"]' in open_block
    assert "revealConfigCard(card);" in open_block
