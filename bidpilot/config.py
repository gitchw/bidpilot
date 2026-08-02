from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bidpilot.network_access import parse_trusted_networks


class Settings(BaseSettings):
    """Runtime configuration loaded from BIDPILOT_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="BIDPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    env: str = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    network_access_mode: Literal["local", "lan"] = "local"
    lan_access_policy: Literal["admin_token", "trusted_lan"] = "admin_token"
    lan_trusted_networks: str = "auto"
    lan_admin_token: str = ""
    timezone: str = "Asia/Shanghai"
    embedded_worker: bool = True
    worker_poll_interval: float = Field(default=3.0, ge=0.2, le=300)
    worker_lease_seconds: int = Field(default=900, ge=30, le=7200)
    worker_heartbeat_ttl: int = Field(default=30, ge=5, le=600)

    data_dir: Path = Path("data")
    control_dir: Path = Path("data")
    report_dir: Path = Path("outputs/reports")
    database_path: Path = Path("data/bidpilot.db")

    request_timeout: float = Field(default=20.0, ge=3, le=120)
    request_interval: float = Field(default=0.8, ge=0.1, le=10)
    max_results_per_source: int = Field(default=20, ge=1, le=100)
    ccgp_max_pages: int = Field(default=2, ge=1, le=20)
    qianlima_browser_mode: Literal["auto", "visible", "headless"] = "auto"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "BidPilot/0.8.0"
    )

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = Field(default=30.0, ge=3, le=120)
    intent_llm_mode: Literal["off", "auto", "always"] = "auto"
    intent_llm_confidence_threshold: float = Field(default=0.85, ge=0.5, le=0.99)
    retrieval_llm_mode: Literal["off", "auto"] = "auto"
    retrieval_max_rounds: int = Field(default=2, ge=1, le=2)
    retrieval_query_budget_per_source: int = Field(default=2, ge=1, le=5)
    retrieval_semantic_review: bool = True
    retrieval_semantic_threshold: float = Field(default=0.82, ge=0.5, le=0.99)
    retrieval_semantic_candidate_limit: int = Field(default=12, ge=1, le=30)
    record_summary_mode: Literal["off", "auto"] = "auto"
    record_summary_max_records: int = Field(default=8, ge=0, le=30)
    record_summary_concurrency: int = Field(default=3, ge=1, le=8)
    record_summary_max_chars: int = Field(default=5000, ge=500, le=12000)
    intelligence_brief_mode: Literal["off", "auto"] = "auto"
    intelligence_brief_max_records: int = Field(default=12, ge=3, le=25)
    decision_assessment_mode: Literal["off", "auto"] = "auto"
    decision_assessment_max_records: int = Field(default=15, ge=3, le=25)

    cecbid_cookie: str = ""

    feishu_webhook_url: str = ""
    feishu_webhook_secret: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_receive_id: str = ""
    feishu_receive_id_type: str = "chat_id"
    public_base_url: str = ""

    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls", "plain"] = "ssl"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_timeout: float = Field(default=30.0, ge=3, le=120)

    dingtalk_webhook_url: str = ""
    dingtalk_webhook_secret: str = ""
    wecom_webhook_url: str = ""
    generic_webhook_url: str = ""
    generic_webhook_bearer_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_thread_id: int | None = Field(default=None, ge=1)
    telegram_disable_notification: bool = False
    telegram_protect_content: bool = False
    slack_webhook_url: str = ""
    delivery_webhook_timeout: float = Field(default=20.0, ge=3, le=120)

    @field_validator("telegram_message_thread_id", mode="before")
    @classmethod
    def normalize_optional_telegram_thread_id(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_telegram_bot_token(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{10,}", cleaned):
            raise ValueError("Telegram Bot Token 格式不正确")
        return cleaned

    @field_validator("telegram_chat_id")
    @classmethod
    def validate_telegram_chat_id(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not re.fullmatch(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{3,}", cleaned):
            raise ValueError("Telegram 会话 ID 必须是整数或 @username")
        return cleaned

    @field_validator("slack_webhook_url")
    @classmethod
    def validate_slack_webhook_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        parsed = urlparse(cleaned)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in {"hooks.slack.com", "hooks.slack-gov.com"}
            or not re.fullmatch(r"/services/[^/]+/[^/]+/[^/]+", parsed.path)
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Slack Webhook 必须是官方 HTTPS /services/ 地址")
        return cleaned

    @field_validator("lan_trusted_networks")
    @classmethod
    def validate_lan_trusted_networks(cls, value: str) -> str:
        cleaned = value.strip()
        parse_trusted_networks(cleaned)
        return cleaned

    @model_validator(mode="after")
    def validate_lan_access(self) -> Settings:
        if (
            self.network_access_mode == "lan"
            and self.lan_access_policy == "admin_token"
            and len(self.lan_admin_token.strip()) < 16
        ):
            raise ValueError("开启局域网管理前必须设置至少 16 个字符的管理员令牌")
        return self

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
