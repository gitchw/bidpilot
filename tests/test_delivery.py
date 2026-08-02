from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest

from bidpilot.config import Settings
from bidpilot.delivery import (
    DeliveryError,
    DeliveryManager,
    DeliveryPermanentError,
    DeliveryRetryAfterError,
)


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.logins: list[tuple[str, str]] = []
        self.messages: list[tuple[EmailMessage, str, list[str]]] = []
        self.starttls_calls = 0
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, **kwargs):
        self.starttls_calls += 1
        return 220, b"ready"

    def login(self, username: str, password: str):
        self.logins.append((username, password))
        return 235, b"ok"

    def send_message(self, message: EmailMessage, *, from_addr: str, to_addrs: list[str]):
        self.messages.append((message, from_addr, to_addrs))
        return {}


class FakeWebhookResponse:
    def __init__(
        self,
        payload: dict | None = None,
        status_code: int = 200,
        *,
        text: str = "ok",
        headers: dict | None = None,
    ):
        self.payload = payload or {"errcode": 0}
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://secret.example.com/hook?token=do-not-leak")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    def json(self):
        return self.payload


class FakeWebhookClient:
    calls: list[tuple[str, dict]] = []
    response = FakeWebhookResponse()

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url: str, **kwargs):
        self.__class__.calls.append((url, kwargs))
        return self.__class__.response


def email_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "report_dir": tmp_path / "reports",
        "database_path": tmp_path / "data" / "test.db",
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_username": "robot@example.com",
        "smtp_password": "secret",
        "smtp_from": "robot@example.com",
        "smtp_to": "owner@example.com, sales@example.com",
    }
    values.update(overrides)
    return Settings(**values)


async def test_smtp_delivery_sends_word_attachment(tmp_path: Path, monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr("bidpilot.delivery.smtplib.SMTP_SSL", FakeSMTP)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04test")
    manager = DeliveryManager(email_settings(tmp_path))

    receipt = await manager.deliver(
        path,
        "email",
        new_count=3,
        subscription_name="重点服务器日报",
    )

    assert receipt.success is True
    assert receipt.channel == "email"
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 465)
    assert smtp.logins == [("robot@example.com", "secret")]
    message, sender, recipients = smtp.messages[0]
    assert sender == "robot@example.com"
    assert recipients == ["owner@example.com", "sales@example.com"]
    assert "新增 3 条" in message["Subject"]
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "report.docx"
    assert attachment.get_payload(decode=True) == b"PK\x03\x04test"


async def test_smtp_starttls_sends_no_change_receipt_without_attachment(
    tmp_path: Path, monkeypatch
):
    FakeSMTP.instances.clear()
    monkeypatch.setattr("bidpilot.delivery.smtplib.SMTP", FakeSMTP)
    manager = DeliveryManager(
        email_settings(
            tmp_path,
            smtp_port=587,
            smtp_security="starttls",
            smtp_username="",
            smtp_password="",
        )
    )

    receipt = await manager.deliver(
        None,
        "email",
        new_count=0,
        subscription_name="每日标讯",
    )

    assert receipt.success is True
    smtp = FakeSMTP.instances[0]
    assert smtp.starttls_calls == 1
    assert smtp.logins == []
    message = smtp.messages[0][0]
    assert list(message.iter_attachments()) == []
    assert "没有新增" in message.get_body(preferencelist=("plain",)).get_content()


def test_email_channel_is_only_available_when_required_fields_are_configured(tmp_path: Path):
    unconfigured = DeliveryManager(Settings()).channel_status()
    assert next(item for item in unconfigured if item["id"] == "email")["configured"] is False
    telegram = next(item for item in unconfigured if item["id"] == "telegram_bot")
    assert telegram["supports_file"] is True
    assert telegram["supports_link"] is True
    assert "50 MB" in telegram["message"]
    assert "报告根地址" in telegram["message"]
    assert "死信" in telegram["message"]

    configured = DeliveryManager(email_settings(tmp_path)).channel_status()
    email = next(item for item in configured if item["id"] == "email")
    assert email["configured"] is True
    assert email["push_capable"] is True

    missing_password = DeliveryManager(email_settings(tmp_path, smtp_password="")).channel_status()
    assert next(item for item in missing_password if item["id"] == "email")["configured"] is False


def test_robot_and_generic_channels_are_reported_from_runtime_settings():
    manager = DeliveryManager(
        Settings(
            dingtalk_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            wecom_webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            generic_webhook_url="https://automation.example.com/bidpilot",
        )
    )
    status = {item["id"]: item for item in manager.channel_status()}

    assert status["dingtalk_webhook"]["configured"] is True
    assert status["wecom_webhook"]["configured"] is True
    assert status["generic_webhook"]["configured"] is True
    assert all(status[channel]["push_capable"] for channel in status if channel != "local")


@pytest.mark.parametrize(
    ("channel", "settings_values", "expected_payload_type"),
    [
        (
            "dingtalk_webhook",
            {
                "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=test",
                "dingtalk_webhook_secret": "signing-secret",
            },
            "markdown",
        ),
        (
            "wecom_webhook",
            {"wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"},
            "markdown",
        ),
        (
            "generic_webhook",
            {
                "generic_webhook_url": "https://automation.example.com/bidpilot",
                "generic_webhook_bearer_token": "bearer-secret",
            },
            "bidpilot.run.no_change",
        ),
    ],
)
async def test_webhook_channels_send_standard_payloads(
    monkeypatch, channel, settings_values, expected_payload_type
):
    FakeWebhookClient.calls.clear()
    FakeWebhookClient.response = FakeWebhookResponse()
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    manager = DeliveryManager(Settings(**settings_values))

    receipt = await manager.deliver(
        None,
        channel,
        new_count=0,
        subscription_name="连接测试",
    )

    assert receipt.success is True
    url, request = FakeWebhookClient.calls[0]
    payload = request["json"]
    if channel == "generic_webhook":
        assert payload["event"] == expected_payload_type
        assert request["headers"]["Authorization"] == "Bearer bearer-secret"
    else:
        assert payload["msgtype"] == expected_payload_type
    if channel == "dingtalk_webhook":
        assert "timestamp=" in url and "sign=" in url


async def test_webhook_http_errors_never_echo_secret_url(monkeypatch):
    FakeWebhookClient.calls.clear()
    FakeWebhookClient.response = FakeWebhookResponse(status_code=403)
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    manager = DeliveryManager(
        Settings(generic_webhook_url="https://secret.example.com/hook?token=do-not-leak")
    )

    with pytest.raises(DeliveryError) as error:
        await manager.deliver(None, "generic_webhook")

    assert "do-not-leak" not in str(error.value)
    assert "HTTP 403" in str(error.value)


async def test_telegram_sends_docx_through_official_bot_api(tmp_path: Path, monkeypatch):
    FakeWebhookClient.calls.clear()
    FakeWebhookClient.response = FakeWebhookResponse(
        {"ok": True, "result": {"message_id": 7, "chat": {"id": -100123}}}
    )
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04telegram")
    manager = DeliveryManager(
        Settings(
            telegram_bot_token="123456:TEST_TOKEN",
            telegram_chat_id="-100123",
            telegram_message_thread_id=42,
            telegram_disable_notification=True,
            telegram_protect_content=True,
        )
    )

    receipt = await manager.deliver(
        path,
        "telegram_bot",
        new_count=3,
        subscription_name="服务器日报",
        delivery_key="run:outbox",
    )

    assert receipt.external_id == "-100123:7"
    url, request = FakeWebhookClient.calls[0]
    assert url == "https://api.telegram.org/bot123456:TEST_TOKEN/sendDocument"
    assert request["data"]["chat_id"] == "-100123"
    assert request["data"]["message_thread_id"] == "42"
    assert request["data"]["disable_notification"] == "true"
    assert request["files"]["document"][0] == "report.docx"
    assert "run:outbox" in request["data"]["caption"]


async def test_telegram_text_and_retry_after_are_structured(monkeypatch):
    FakeWebhookClient.calls.clear()
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    manager = DeliveryManager(
        Settings(
            telegram_bot_token="123456:TEST_TOKEN",
            telegram_chat_id="@bidpilot_test",
        )
    )
    FakeWebhookClient.response = FakeWebhookResponse(
        {"ok": True, "result": {"message_id": 0, "chat": {"id": 99}}}
    )

    receipt = await manager.deliver(None, "telegram_bot", delivery_key="run:zero")
    assert receipt.external_id == "99:0"
    assert FakeWebhookClient.calls[0][1]["json"]["chat_id"] == "@bidpilot_test"

    FakeWebhookClient.response = FakeWebhookResponse(
        {
            "ok": False,
            "error_code": 429,
            "parameters": {"retry_after": 120},
        },
        status_code=429,
    )
    with pytest.raises(DeliveryRetryAfterError) as error:
        await manager.deliver(None, "telegram_bot")
    assert error.value.retry_after_seconds == 120
    assert "TEST_TOKEN" not in str(error.value)

    FakeWebhookClient.response = FakeWebhookResponse(
        {"ok": False, "error_code": 429},
        status_code=429,
        headers={"Retry-After": "75"},
    )
    with pytest.raises(DeliveryRetryAfterError) as header_retry:
        await manager.deliver(None, "telegram_bot")
    assert header_retry.value.retry_after_seconds == 75


async def test_telegram_permanent_error_never_echoes_token(monkeypatch):
    FakeWebhookClient.calls.clear()
    FakeWebhookClient.response = FakeWebhookResponse(
        {"ok": False, "error_code": 403},
        status_code=403,
    )
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    manager = DeliveryManager(
        Settings(telegram_bot_token="123456:TOP_SECRET", telegram_chat_id="123")
    )

    with pytest.raises(DeliveryPermanentError) as error:
        await manager.deliver(None, "telegram_bot")
    assert "TOP_SECRET" not in str(error.value)
    assert "403" in str(error.value)

    FakeWebhookClient.response = FakeWebhookResponse(
        {"ok": False, "error_code": 413},
        status_code=413,
    )
    with pytest.raises(DeliveryPermanentError, match="413"):
        await manager.deliver(None, "telegram_bot")


async def test_telegram_rejects_malformed_success_and_non_json_auth_failure(monkeypatch):
    FakeWebhookClient.calls.clear()
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    manager = DeliveryManager(
        Settings(telegram_bot_token="123456:TOP_SECRET", telegram_chat_id="123")
    )
    FakeWebhookClient.response = FakeWebhookResponse({"ok": True, "result": {}})
    with pytest.raises(DeliveryError, match="消息编号或会话对象"):
        await manager.deliver(None, "telegram_bot")

    FakeWebhookClient.response = FakeWebhookResponse(["unexpected"])
    with pytest.raises(DeliveryError, match="无法识别"):
        await manager.deliver(None, "telegram_bot")

    class NonJsonAuthFailure(FakeWebhookResponse):
        def json(self):
            raise ValueError("not json")

    FakeWebhookClient.response = NonJsonAuthFailure(status_code=401)
    with pytest.raises(DeliveryPermanentError) as error:
        await manager.deliver(None, "telegram_bot")
    assert "TOP_SECRET" not in str(error.value)
    assert "401" in str(error.value)


async def test_slack_incoming_webhook_sends_link_not_fake_attachment(tmp_path: Path, monkeypatch):
    FakeWebhookClient.calls.clear()
    FakeWebhookClient.response = FakeWebhookResponse(text="ok")
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04slack")
    manager = DeliveryManager(
        Settings(
            slack_webhook_url=("https://hooks.slack.com/services/T00000000/B00000000/TESTSECRET"),
            public_base_url="https://reports.example.com",
        )
    )

    receipt = await manager.deliver(
        path,
        "slack_webhook",
        new_count=2,
        delivery_key="run:slack",
    )

    assert receipt.success is True
    url, request = FakeWebhookClient.calls[0]
    assert url.startswith("https://hooks.slack.com/services/")
    assert "files" not in request
    assert "https://reports.example.com/api/v1/reports/report.docx" in request["json"]["text"]
    assert "run:slack" in request["json"]["text"]


async def test_slack_escapes_user_controlled_mentions(monkeypatch):
    FakeWebhookClient.calls.clear()
    FakeWebhookClient.response = FakeWebhookResponse(text="ok")
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    manager = DeliveryManager(
        Settings(
            slack_webhook_url=("https://hooks.slack.com/services/T00000000/B00000000/TESTSECRET")
        )
    )

    await manager.deliver(
        None,
        "slack_webhook",
        subscription_name="<!channel> <@U123> 研发&销售",
    )

    text = FakeWebhookClient.calls[0][1]["json"]["text"]
    assert "<!channel>" not in text
    assert "<@U123>" not in text
    assert "&lt;!channel&gt;" in text
    assert "&lt;@U123&gt;" in text
    assert "研发&amp;销售" in text


async def test_slack_rate_limit_and_permanent_error_are_classified(monkeypatch):
    FakeWebhookClient.calls.clear()
    monkeypatch.setattr("bidpilot.delivery.httpx.AsyncClient", FakeWebhookClient)
    manager = DeliveryManager(
        Settings(
            slack_webhook_url=("https://hooks.slack.com/services/T00000000/B00000000/TESTSECRET")
        )
    )
    FakeWebhookClient.response = FakeWebhookResponse(
        status_code=429,
        headers={"Retry-After": "90"},
    )
    with pytest.raises(DeliveryRetryAfterError) as retry:
        await manager.deliver(None, "slack_webhook")
    assert retry.value.retry_after_seconds == 90

    FakeWebhookClient.response = FakeWebhookResponse(status_code=403, text="invalid_token")
    with pytest.raises(DeliveryPermanentError):
        await manager.deliver(None, "slack_webhook")


def test_slack_webhook_rejects_non_official_or_non_https_urls():
    with pytest.raises(ValueError):
        Settings(slack_webhook_url="https://evil.example.com/services/T/B/C")
    with pytest.raises(ValueError):
        Settings(slack_webhook_url="http://hooks.slack.com/services/T/B/C")
    with pytest.raises(ValueError):
        Settings(slack_webhook_url="https://hooks.slack.com/services/")
    with pytest.raises(ValueError):
        Settings(slack_webhook_url="https://hooks.slack.com/services/T/B/C/extra")
    with pytest.raises(ValueError):
        Settings(slack_webhook_url="https://hooks.slack.com/services/T//B/C")


def test_optional_telegram_thread_env_value_accepts_blank_and_validates_identity():
    settings = Settings(
        _env_file=None,
        telegram_message_thread_id="",
        telegram_bot_token=" 123456:TEST_TOKEN ",
        telegram_chat_id=" -100123 ",
    )
    assert settings.telegram_message_thread_id is None
    assert settings.telegram_bot_token == "123456:TEST_TOKEN"
    assert settings.telegram_chat_id == "-100123"

    with pytest.raises(ValueError):
        Settings(_env_file=None, telegram_chat_id="not a chat")
    with pytest.raises(ValueError) as token_error:
        Settings(_env_file=None, telegram_bot_token="not-a-real-token")
    assert "not-a-real-token" not in str(token_error.value)
    secret_url = "https://hooks.slack.com/services/T/B/DO_NOT_LEAK?x=1"
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None, slack_webhook_url=secret_url)
    assert secret_url not in str(error.value)


def test_retry_after_does_not_truncate_platform_delay():
    assert DeliveryRetryAfterError("limited", 172_800).retry_after_seconds == 172_800
