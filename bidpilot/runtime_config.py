from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from bidpilot.config import Settings
from bidpilot.db import Database

SecretField = Literal[
    "llm_api_key",
    "feishu_webhook_url",
    "feishu_webhook_secret",
    "feishu_app_secret",
    "smtp_password",
    "dingtalk_webhook_url",
    "dingtalk_webhook_secret",
    "wecom_webhook_url",
    "generic_webhook_url",
    "generic_webhook_bearer_token",
]

SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "llm_api_key",
        "feishu_webhook_url",
        "feishu_webhook_secret",
        "feishu_app_secret",
        "smtp_password",
        "dingtalk_webhook_url",
        "dingtalk_webhook_secret",
        "wecom_webhook_url",
        "generic_webhook_url",
        "generic_webhook_bearer_token",
    }
)

RUNTIME_FIELDS: frozenset[str] = frozenset(
    {
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_timeout",
        "feishu_webhook_url",
        "feishu_webhook_secret",
        "feishu_app_id",
        "feishu_app_secret",
        "feishu_receive_id",
        "feishu_receive_id_type",
        "public_base_url",
        "smtp_host",
        "smtp_port",
        "smtp_security",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        "smtp_to",
        "smtp_timeout",
        "dingtalk_webhook_url",
        "dingtalk_webhook_secret",
        "wecom_webhook_url",
        "generic_webhook_url",
        "generic_webhook_bearer_token",
        "delivery_webhook_timeout",
    }
)

URL_FIELDS = {
    "llm_base_url",
    "feishu_webhook_url",
    "public_base_url",
    "dingtalk_webhook_url",
    "wecom_webhook_url",
    "generic_webhook_url",
}


class RuntimeConfigUpdate(BaseModel):
    """网页配置中心允许写入的完整白名单。未提交字段保持原值。"""

    model_config = ConfigDict(extra="forbid")

    llm_base_url: str | None = Field(default=None, max_length=2000)
    llm_api_key: SecretStr | None = None
    llm_model: str | None = Field(default=None, max_length=200)
    llm_timeout: float | None = Field(default=None, ge=3, le=120)

    feishu_webhook_url: SecretStr | None = None
    feishu_webhook_secret: SecretStr | None = None
    feishu_app_id: str | None = Field(default=None, max_length=200)
    feishu_app_secret: SecretStr | None = None
    feishu_receive_id: str | None = Field(default=None, max_length=300)
    feishu_receive_id_type: Literal["chat_id", "open_id", "user_id", "union_id", "email"] | None = (
        None
    )
    public_base_url: str | None = Field(default=None, max_length=2000)

    smtp_host: str | None = Field(default=None, max_length=500)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls", "plain"] | None = None
    smtp_username: str | None = Field(default=None, max_length=500)
    smtp_password: SecretStr | None = None
    smtp_from: str | None = Field(default=None, max_length=500)
    smtp_to: str | None = Field(default=None, max_length=2000)
    smtp_timeout: float | None = Field(default=None, ge=3, le=120)

    dingtalk_webhook_url: SecretStr | None = None
    dingtalk_webhook_secret: SecretStr | None = None
    wecom_webhook_url: SecretStr | None = None
    generic_webhook_url: SecretStr | None = None
    generic_webhook_bearer_token: SecretStr | None = None
    delivery_webhook_timeout: float | None = Field(default=None, ge=3, le=120)

    clear_secrets: list[SecretField] = Field(default_factory=list)

    @field_validator(
        "llm_base_url",
        "public_base_url",
    )
    @classmethod
    def validate_public_url(cls, value: str | None) -> str | None:
        return cls._validate_url(value)

    @field_validator(
        "feishu_webhook_url",
        "dingtalk_webhook_url",
        "wecom_webhook_url",
        "generic_webhook_url",
    )
    @classmethod
    def validate_secret_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        plain = cls._validate_url(value.get_secret_value())
        return SecretStr(plain or "")

    @staticmethod
    def _validate_url(value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("地址必须是完整的 http:// 或 https:// URL")
        return value


class SecretState(BaseModel):
    configured: bool = Field(description="是否已经保存该敏感值；接口永不返回原文")


class AIConfigView(BaseModel):
    llm_base_url: str
    llm_model: str
    llm_timeout: float
    llm_api_key: SecretState
    ready: bool


class FeishuConfigView(BaseModel):
    webhook_url: SecretState
    webhook_secret: SecretState
    app_id: str
    app_secret: SecretState
    receive_id: str
    receive_id_type: str
    public_base_url: str
    webhook_ready: bool
    app_ready: bool


class SMTPConfigView(BaseModel):
    host: str
    port: int
    security: str
    username: str
    password: SecretState
    sender: str
    recipients: str
    timeout: float
    ready: bool


class RobotConfigView(BaseModel):
    webhook_url: SecretState
    secret: SecretState | None = None
    ready: bool


class GenericWebhookConfigView(BaseModel):
    webhook_url: SecretState
    bearer_token: SecretState
    timeout: float
    ready: bool


class RuntimeConfigView(BaseModel):
    ai: AIConfigView
    feishu: FeishuConfigView
    email: SMTPConfigView
    dingtalk: RobotConfigView
    wecom: RobotConfigView
    generic_webhook: GenericWebhookConfigView
    security_notice: str


class ConnectionTestResult(BaseModel):
    success: bool
    target: str
    message: str
    latency_ms: int
    preview: str | None = None


class RuntimeConfigError(RuntimeError):
    pass


class LocalSecretVault:
    prefix = "fernet:v1:"

    def __init__(self, key_path: Path):
        self.key_path = key_path

    def _fernet(self, *, create: bool) -> Fernet | None:
        if self.key_path.exists():
            return Fernet(self.key_path.read_bytes().strip())
        if not create:
            return None
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            with self.key_path.open("xb") as handle:
                handle.write(key)
            self.key_path.chmod(0o600)
        except FileExistsError:
            key = self.key_path.read_bytes().strip()
        return Fernet(key)

    def encrypt(self, value: str) -> str:
        cipher = self._fernet(create=True)
        if cipher is None:  # pragma: no cover - create=True always returns a cipher
            raise RuntimeConfigError("无法初始化本机敏感配置密钥")
        return self.prefix + cipher.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> tuple[str, bool]:
        if not value.startswith(self.prefix):
            return value, True
        cipher = self._fernet(create=False)
        if cipher is None:
            raise RuntimeConfigError("敏感配置密钥文件缺失，无法读取已保存凭据")
        try:
            decrypted = cipher.decrypt(value.removeprefix(self.prefix).encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise RuntimeConfigError("敏感配置无法解密，请显式清除后重新填写") from exc
        return decrypted.decode("utf-8"), False


class RuntimeConfiguration:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.vault = LocalSecretVault(settings.data_dir / "secrets" / "runtime_config.key")
        self.load_persisted()

    def load_persisted(self) -> None:
        rows = self.db.get_runtime_config()
        persisted: dict[str, object] = {}
        legacy_secrets: dict[str, tuple[str, bool]] = {}
        for field, row in rows.items():
            if field not in RUNTIME_FIELDS:
                continue
            try:
                raw_value = row["value"]
                was_plaintext = False
                if field in SECRET_FIELDS and row["is_secret"]:
                    raw_value, was_plaintext = self.vault.decrypt(raw_value)
                persisted[field] = json.loads(raw_value)
                if was_plaintext and field in SECRET_FIELDS:
                    legacy_secrets[field] = (self.vault.encrypt(raw_value), True)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if legacy_secrets:
            self.db.set_runtime_config(legacy_secrets)
        if not persisted:
            return
        candidate = Settings.model_validate({**self.settings.model_dump(), **persisted})
        for field in persisted:
            setattr(self.settings, field, getattr(candidate, field))

    def snapshot(self) -> RuntimeConfigView:
        def secret(field: str) -> SecretState:
            return SecretState(configured=bool(getattr(self.settings, field)))

        feishu_app_ready = all(
            (
                self.settings.feishu_app_id,
                self.settings.feishu_app_secret,
                self.settings.feishu_receive_id,
            )
        )
        smtp_recipients = [
            item.strip()
            for item in self.settings.smtp_to.replace(";", ",").split(",")
            if item.strip()
        ]
        smtp_ready = bool(
            self.settings.smtp_host
            and self.settings.smtp_from
            and smtp_recipients
            and (not self.settings.smtp_username or self.settings.smtp_password)
        )
        return RuntimeConfigView(
            ai=AIConfigView(
                llm_base_url=self.settings.llm_base_url,
                llm_model=self.settings.llm_model,
                llm_timeout=self.settings.llm_timeout,
                llm_api_key=secret("llm_api_key"),
                ready=bool(self.settings.llm_base_url and self.settings.llm_model),
            ),
            feishu=FeishuConfigView(
                webhook_url=secret("feishu_webhook_url"),
                webhook_secret=secret("feishu_webhook_secret"),
                app_id=self.settings.feishu_app_id,
                app_secret=secret("feishu_app_secret"),
                receive_id=self.settings.feishu_receive_id,
                receive_id_type=self.settings.feishu_receive_id_type,
                public_base_url=self.settings.public_base_url,
                webhook_ready=bool(self.settings.feishu_webhook_url),
                app_ready=feishu_app_ready,
            ),
            email=SMTPConfigView(
                host=self.settings.smtp_host,
                port=self.settings.smtp_port,
                security=self.settings.smtp_security,
                username=self.settings.smtp_username,
                password=secret("smtp_password"),
                sender=self.settings.smtp_from,
                recipients=self.settings.smtp_to,
                timeout=self.settings.smtp_timeout,
                ready=smtp_ready,
            ),
            dingtalk=RobotConfigView(
                webhook_url=secret("dingtalk_webhook_url"),
                secret=secret("dingtalk_webhook_secret"),
                ready=bool(self.settings.dingtalk_webhook_url),
            ),
            wecom=RobotConfigView(
                webhook_url=secret("wecom_webhook_url"),
                ready=bool(self.settings.wecom_webhook_url),
            ),
            generic_webhook=GenericWebhookConfigView(
                webhook_url=secret("generic_webhook_url"),
                bearer_token=secret("generic_webhook_bearer_token"),
                timeout=self.settings.delivery_webhook_timeout,
                ready=bool(self.settings.generic_webhook_url),
            ),
            security_notice=(
                "敏感值使用本机密钥加密保存且仅显示“已配置”；留空表示保持原值，"
                "清除必须显式勾选。"
                "默认服务只监听本机，公网部署必须增加 TLS、身份认证与访问审计。"
            ),
        )

    def update(self, payload: RuntimeConfigUpdate) -> RuntimeConfigView:
        submitted = payload.model_dump(exclude_unset=True)
        clear_secrets = set(submitted.pop("clear_secrets", []))
        changes: dict[str, object] = {}
        for field, value in submitted.items():
            if field not in RUNTIME_FIELDS or value is None:
                continue
            if isinstance(value, SecretStr):
                value = value.get_secret_value().strip()
                if not value:
                    continue
            elif isinstance(value, str):
                value = value.strip()
            changes[field] = value
        for field in clear_secrets:
            if field in SECRET_FIELDS:
                changes[field] = ""

        if changes:
            candidate = Settings.model_validate({**self.settings.model_dump(), **changes})
            serialized = {
                field: (
                    self.vault.encrypt(json.dumps(getattr(candidate, field), ensure_ascii=False))
                    if field in SECRET_FIELDS
                    else json.dumps(getattr(candidate, field), ensure_ascii=False),
                    field in SECRET_FIELDS,
                )
                for field in changes
            }
            self.db.set_runtime_config(serialized)
            for field in changes:
                setattr(self.settings, field, getattr(candidate, field))
        return self.snapshot()

    async def test_model(self) -> ConnectionTestResult:
        if not (self.settings.llm_base_url and self.settings.llm_model):
            raise RuntimeConfigError("请先填写模型 API 基础地址和模型名称")
        endpoint = self.settings.llm_base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json={
                        "model": self.settings.llm_model,
                        "messages": [
                            {
                                "role": "user",
                                "content": "请只回复：标擎模型连接成功",
                            }
                        ],
                        "temperature": 0,
                        "max_tokens": 32,
                    },
                )
                response.raise_for_status()
                data = response.json()
                content = str(data["choices"][0]["message"]["content"]).strip()
        except httpx.TimeoutException as exc:
            raise RuntimeConfigError("模型连接超时，请检查地址、网络和超时设置") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeConfigError(f"模型服务返回 HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeConfigError("模型服务响应格式不符合 OpenAI-compatible 规范") from exc
        latency = round((time.perf_counter() - started) * 1000)
        return ConnectionTestResult(
            success=True,
            target="ai",
            message="OpenAI-compatible 模型连接成功",
            latency_ms=latency,
            preview=content[:120],
        )


@dataclass(slots=True)
class _TokenRecord:
    digest: str
    expires_at: float


class ConfigEditTokenManager:
    def __init__(self, ttl_seconds: int = 600, max_tokens: int = 20):
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        self._tokens: list[_TokenRecord] = []

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self) -> tuple[str, int]:
        now = time.time()
        self._tokens = [record for record in self._tokens if record.expires_at > now]
        token = secrets.token_urlsafe(32)
        self._tokens.append(_TokenRecord(self._digest(token), now + self.ttl_seconds))
        self._tokens = self._tokens[-self.max_tokens :]
        return token, self.ttl_seconds

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        digest = self._digest(token)
        self._tokens = [record for record in self._tokens if record.expires_at > now]
        return any(secrets.compare_digest(record.digest, digest) for record in self._tokens)
