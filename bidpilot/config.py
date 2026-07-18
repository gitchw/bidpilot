from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from BIDPILOT_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="BIDPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    timezone: str = "Asia/Shanghai"
    embedded_worker: bool = True
    worker_poll_interval: float = Field(default=3.0, ge=0.2, le=300)
    worker_lease_seconds: int = Field(default=900, ge=30, le=7200)
    worker_heartbeat_ttl: int = Field(default=30, ge=5, le=600)

    data_dir: Path = Path("data")
    report_dir: Path = Path("outputs/reports")
    database_path: Path = Path("data/bidpilot.db")

    request_timeout: float = Field(default=20.0, ge=3, le=120)
    request_interval: float = Field(default=0.8, ge=0.1, le=10)
    max_results_per_source: int = Field(default=20, ge=1, le=100)
    ccgp_max_pages: int = Field(default=2, ge=1, le=20)
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "BidPilot/0.3.0"
    )

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    cecbid_cookie: str = ""
    qianlima_cookie: str = ""
    qianlima_cookie_path: Path = Path("data/secrets/qianlima_cookie.txt")

    feishu_webhook_url: str = ""
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

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.qianlima_cookie_path.parent.mkdir(parents=True, exist_ok=True)

    def load_qianlima_cookie(self) -> str:
        if self.qianlima_cookie.strip():
            return self.qianlima_cookie.strip()
        if self.qianlima_cookie_path.exists():
            return self.qianlima_cookie_path.read_text(encoding="utf-8").strip()
        return ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
