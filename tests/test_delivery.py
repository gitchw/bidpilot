from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from bidpilot.config import Settings
from bidpilot.delivery import DeliveryManager


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

    configured = DeliveryManager(email_settings(tmp_path)).channel_status()
    email = next(item for item in configured if item["id"] == "email")
    assert email["configured"] is True
    assert email["push_capable"] is True

    missing_password = DeliveryManager(email_settings(tmp_path, smtp_password="")).channel_status()
    assert next(item for item in missing_password if item["id"] == "email")["configured"] is False
