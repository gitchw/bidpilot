from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.private_files import harden_private_path

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
    "lan_admin_token",
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
        "lan_admin_token",
    }
)

RUNTIME_FIELDS: frozenset[str] = frozenset(
    {
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_timeout",
        "intent_llm_mode",
        "intent_llm_confidence_threshold",
        "retrieval_llm_mode",
        "retrieval_max_rounds",
        "retrieval_query_budget_per_source",
        "retrieval_semantic_review",
        "retrieval_semantic_threshold",
        "retrieval_semantic_candidate_limit",
        "record_summary_mode",
        "record_summary_max_records",
        "record_summary_concurrency",
        "record_summary_max_chars",
        "intelligence_brief_mode",
        "intelligence_brief_max_records",
        "decision_assessment_mode",
        "decision_assessment_max_records",
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
        "request_timeout",
        "request_interval",
        "max_results_per_source",
        "ccgp_max_pages",
        "worker_poll_interval",
        "worker_lease_seconds",
        "worker_heartbeat_ttl",
        "network_access_mode",
        "lan_access_policy",
        "lan_trusted_networks",
        "port",
        "lan_admin_token",
    }
)

RESETTABLE_FIELDS: frozenset[str] = RUNTIME_FIELDS - SECRET_FIELDS
RESTART_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "network_access_mode",
        "lan_access_policy",
        "lan_trusted_networks",
        "lan_admin_token",
        "port",
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

    revision: int = Field(ge=0, description="读取配置时返回的版本号；旧版本写入会返回 409")

    llm_base_url: str | None = Field(default=None, max_length=2000)
    llm_api_key: SecretStr | None = None
    llm_model: str | None = Field(default=None, max_length=200)
    llm_timeout: float | None = Field(default=None, ge=3, le=120)
    intent_llm_mode: Literal["off", "auto", "always"] | None = None
    intent_llm_confidence_threshold: float | None = Field(default=None, ge=0.5, le=0.99)
    retrieval_llm_mode: Literal["off", "auto"] | None = None
    retrieval_max_rounds: int | None = Field(default=None, ge=1, le=2)
    retrieval_query_budget_per_source: int | None = Field(default=None, ge=1, le=5)
    retrieval_semantic_review: bool | None = None
    retrieval_semantic_threshold: float | None = Field(default=None, ge=0.5, le=0.99)
    retrieval_semantic_candidate_limit: int | None = Field(default=None, ge=1, le=30)
    record_summary_mode: Literal["off", "auto"] | None = None
    record_summary_max_records: int | None = Field(default=None, ge=0, le=30)
    record_summary_concurrency: int | None = Field(default=None, ge=1, le=8)
    record_summary_max_chars: int | None = Field(default=None, ge=500, le=12000)
    intelligence_brief_mode: Literal["off", "auto"] | None = None
    intelligence_brief_max_records: int | None = Field(default=None, ge=3, le=25)
    decision_assessment_mode: Literal["off", "auto"] | None = Field(
        default=None,
        description="关闭或启用企业画像语义复核；关闭后仍使用本地计分和推荐",
    )
    decision_assessment_max_records: int | None = Field(
        default=None,
        ge=3,
        le=25,
        description="单轮最多发送给模型做语义复核的证据数；其余结果仍由本地规则逐条判断",
    )

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

    request_timeout: float | None = Field(default=None, ge=3, le=120)
    request_interval: float | None = Field(default=None, ge=0.1, le=10)
    max_results_per_source: int | None = Field(default=None, ge=1, le=100)
    ccgp_max_pages: int | None = Field(default=None, ge=1, le=20)
    worker_poll_interval: float | None = Field(default=None, ge=0.2, le=300)
    worker_lease_seconds: int | None = Field(default=None, ge=30, le=7200)
    worker_heartbeat_ttl: int | None = Field(default=None, ge=5, le=600)
    network_access_mode: Literal["local", "lan"] | None = Field(
        default=None,
        description="重启后的监听范围：local 仅本机，lan 允许局域网设备连接",
    )
    lan_access_policy: Literal["admin_token", "trusted_lan"] | None = Field(
        default=None,
        description="LAN 写操作保护：管理员令牌，或仅对可信私有网段免令牌",
    )
    lan_trusted_networks: str | None = Field(
        default=None,
        max_length=1000,
        description="可信私网 CIDR，逗号分隔；auto 使用内置私网、链路本地和组网范围",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="重启后的 Web 监听端口；默认值为 8000",
    )
    lan_admin_token: SecretStr | None = Field(
        default=None,
        description="LAN 管理员令牌，仅写入不回显；令牌模式至少 16 个字符",
    )

    clear_secrets: list[SecretField] = Field(default_factory=list)
    reset_fields: list[str] = Field(default_factory=list, max_length=len(RESETTABLE_FIELDS))

    @field_validator("reset_fields")
    @classmethod
    def validate_reset_fields(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        unknown = sorted(set(cleaned) - RESETTABLE_FIELDS)
        if unknown:
            raise ValueError(f"未知或不可恢复的配置字段：{', '.join(unknown)}")
        return cleaned

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
    intent_llm_mode: Literal["off", "auto", "always"]
    intent_llm_confidence_threshold: float
    retrieval_llm_mode: Literal["off", "auto"]
    retrieval_max_rounds: int
    retrieval_query_budget_per_source: int
    retrieval_semantic_review: bool
    retrieval_semantic_threshold: float
    retrieval_semantic_candidate_limit: int
    record_summary_mode: Literal["off", "auto"]
    record_summary_max_records: int
    record_summary_concurrency: int
    record_summary_max_chars: int
    intelligence_brief_mode: Literal["off", "auto"]
    intelligence_brief_max_records: int
    decision_assessment_mode: Literal["off", "auto"] = Field(
        description="企业画像 AI 语义复核开关；本地计分始终生效"
    )
    decision_assessment_max_records: int = Field(
        description="最多送入模型复核的证据数，不是最终判断条数上限"
    )
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


class RetrievalConfigView(BaseModel):
    request_timeout: float
    request_interval: float
    max_results_per_source: int
    ccgp_max_pages: int


class WorkerConfigView(BaseModel):
    poll_interval: float
    lease_seconds: int
    heartbeat_ttl: int


class NetworkConfigView(BaseModel):
    access_mode: Literal["local", "lan"] = Field(description="已保存、重启后生效的访问范围")
    access_policy: Literal["admin_token", "trusted_lan"] = Field(
        description="已保存、重启后生效的 LAN 写操作保护方式"
    )
    trusted_networks: str = Field(description="已保存、重启后生效的可信私网范围")
    port: int = Field(description="已保存、重启后生效的服务端口")
    bind_host_after_restart: str = Field(description="按已保存范围计算的重启后监听地址")
    effective_access_mode: Literal["local", "lan"] = Field(description="当前进程实际使用的访问范围")
    effective_access_policy: Literal["admin_token", "trusted_lan"] = Field(
        description="当前进程实际使用的 LAN 写操作保护方式"
    )
    effective_trusted_networks: str = Field(description="当前进程实际使用的可信私网范围")
    effective_port: int = Field(description="当前进程实际使用的端口")
    effective_bind_host: str = Field(description="当前进程实际使用的监听地址")
    lan_admin_token: SecretState = Field(description="已保存令牌是否存在，不返回原文")
    effective_lan_admin_token: SecretState = Field(
        description="当前进程使用的令牌是否存在，不返回原文"
    )
    restart_required: bool = Field(description="网络类字段是否要求重启")
    pending_restart: bool = Field(description="已保存值是否与当前进程实际值不同")


class ConfigFieldMetadata(BaseModel):
    source: Literal["web", "environment", "default"]
    updated_at: str | None = None
    restart_required: bool = False
    resettable: bool = True


class RuntimeConfigView(BaseModel):
    revision: int
    ai: AIConfigView
    retrieval: RetrievalConfigView
    worker: WorkerConfigView
    network: NetworkConfigView
    feishu: FeishuConfigView
    email: SMTPConfigView
    dingtalk: RobotConfigView
    wecom: RobotConfigView
    generic_webhook: GenericWebhookConfigView
    field_metadata: dict[str, ConfigFieldMetadata]
    updated_at: str | None = None
    security_notice: str


class ConnectionTestResult(BaseModel):
    success: bool
    target: str
    message: str
    latency_ms: int
    preview: str | None = None


class RuntimeConfigError(RuntimeError):
    pass


class RuntimeConfigConflict(RuntimeConfigError):
    def __init__(self, current_revision: int):
        super().__init__("配置已被另一个页面或进程更新，请重新读取后再保存")
        self.current_revision = current_revision


class LocalSecretVault:
    prefix = "fernet:v1:"

    def __init__(self, key_path: Path):
        self.key_path = key_path

    def _fernet(self, *, create: bool) -> Fernet | None:
        if self.key_path.exists():
            harden_private_path(self.key_path, directory=False)
            return Fernet(self.key_path.read_bytes().strip())
        if not create:
            return None
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        harden_private_path(self.key_path.parent, directory=True)
        key = Fernet.generate_key()
        try:
            with self.key_path.open("xb") as handle:
                handle.write(key)
        except FileExistsError:
            key = self.key_path.read_bytes().strip()
        harden_private_path(self.key_path, directory=False)
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
        self.base_values = settings.model_dump()
        self.default_values = Settings(_env_file=None).model_dump()
        self.vault = LocalSecretVault(settings.data_dir / "secrets" / "runtime_config.key")
        self.load_persisted()
        self._effective_restart_values = {
            field: getattr(settings, field) for field in RESTART_REQUIRED_FIELDS
        }

    def effective_restart_value(self, field: str) -> object:
        if field not in RESTART_REQUIRED_FIELDS:
            raise KeyError(f"{field} 不是需重启生效的配置")
        return self._effective_restart_values[field]

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

        rows = self.db.get_runtime_config()
        metadata: dict[str, ConfigFieldMetadata] = {}
        for field in sorted(RUNTIME_FIELDS):
            row = rows.get(field)
            if row is not None:
                source: Literal["web", "environment", "default"] = "web"
                updated_at = row.get("updated_at")
            else:
                environment_key = f"BIDPILOT_{field.upper()}"
                source = (
                    "environment"
                    if environment_key in os.environ
                    or self.base_values.get(field) != self.default_values.get(field)
                    else "default"
                )
                updated_at = None
            metadata[field] = ConfigFieldMetadata(
                source=source,
                updated_at=updated_at,
                restart_required=field in RESTART_REQUIRED_FIELDS,
                resettable=field in RESETTABLE_FIELDS,
            )
        updated_values = [
            str(row.get("updated_at"))
            for field, row in rows.items()
            if field != "_revision" and row.get("updated_at")
        ]

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
            revision=self.db.get_runtime_config_revision(),
            ai=AIConfigView(
                llm_base_url=self.settings.llm_base_url,
                llm_model=self.settings.llm_model,
                llm_timeout=self.settings.llm_timeout,
                intent_llm_mode=self.settings.intent_llm_mode,
                intent_llm_confidence_threshold=(self.settings.intent_llm_confidence_threshold),
                retrieval_llm_mode=self.settings.retrieval_llm_mode,
                retrieval_max_rounds=self.settings.retrieval_max_rounds,
                retrieval_query_budget_per_source=(self.settings.retrieval_query_budget_per_source),
                retrieval_semantic_review=self.settings.retrieval_semantic_review,
                retrieval_semantic_threshold=self.settings.retrieval_semantic_threshold,
                retrieval_semantic_candidate_limit=(
                    self.settings.retrieval_semantic_candidate_limit
                ),
                record_summary_mode=self.settings.record_summary_mode,
                record_summary_max_records=self.settings.record_summary_max_records,
                record_summary_concurrency=self.settings.record_summary_concurrency,
                record_summary_max_chars=self.settings.record_summary_max_chars,
                intelligence_brief_mode=self.settings.intelligence_brief_mode,
                intelligence_brief_max_records=self.settings.intelligence_brief_max_records,
                decision_assessment_mode=self.settings.decision_assessment_mode,
                decision_assessment_max_records=self.settings.decision_assessment_max_records,
                llm_api_key=secret("llm_api_key"),
                ready=bool(self.settings.llm_base_url and self.settings.llm_model),
            ),
            retrieval=RetrievalConfigView(
                request_timeout=self.settings.request_timeout,
                request_interval=self.settings.request_interval,
                max_results_per_source=self.settings.max_results_per_source,
                ccgp_max_pages=self.settings.ccgp_max_pages,
            ),
            worker=WorkerConfigView(
                poll_interval=self.settings.worker_poll_interval,
                lease_seconds=self.settings.worker_lease_seconds,
                heartbeat_ttl=self.settings.worker_heartbeat_ttl,
            ),
            network=NetworkConfigView(
                access_mode=self.settings.network_access_mode,
                access_policy=self.settings.lan_access_policy,
                trusted_networks=self.settings.lan_trusted_networks,
                port=self.settings.port,
                bind_host_after_restart=(
                    "0.0.0.0" if self.settings.network_access_mode == "lan" else "127.0.0.1"
                ),
                effective_access_mode=self.effective_restart_value("network_access_mode"),
                effective_access_policy=self.effective_restart_value("lan_access_policy"),
                effective_trusted_networks=self.effective_restart_value("lan_trusted_networks"),
                effective_port=self.effective_restart_value("port"),
                effective_bind_host=(
                    "0.0.0.0"
                    if self.effective_restart_value("network_access_mode") == "lan"
                    else "127.0.0.1"
                ),
                lan_admin_token=secret("lan_admin_token"),
                effective_lan_admin_token=SecretState(
                    configured=bool(self.effective_restart_value("lan_admin_token"))
                ),
                restart_required=True,
                pending_restart=any(
                    getattr(self.settings, field) != self.effective_restart_value(field)
                    for field in RESTART_REQUIRED_FIELDS
                ),
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
            field_metadata=metadata,
            updated_at=max(updated_values, default=None),
            security_notice=(
                "敏感值使用本机密钥加密保存且仅显示“已配置”；留空表示保持原值，"
                "清除必须显式勾选。"
                "默认服务只监听本机；主动开启局域网模式后，其他设备的写操作必须携带"
                "独立管理员令牌，或明确选择仅对可信私有网段免令牌。网络配置统一在"
                "重启后生效；局域网 HTTP 不加密，也不等于公网安全方案。"
            ),
        )

    def update(self, payload: RuntimeConfigUpdate) -> RuntimeConfigView:
        submitted = payload.model_dump(exclude_unset=True)
        expected_revision = int(submitted.pop("revision"))
        clear_secrets = set(submitted.pop("clear_secrets", []))
        reset_fields = set(submitted.pop("reset_fields", []))
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
        conflicts = (set(changes) & clear_secrets) | (set(changes) & reset_fields)
        if conflicts:
            raise RuntimeConfigError(
                f"同一字段不能同时填写、清除或恢复来源值：{', '.join(sorted(conflicts))}"
            )
        for field in clear_secrets:
            if field in SECRET_FIELDS:
                changes[field] = ""
        restored = {field: self.base_values[field] for field in reset_fields}
        try:
            candidate = Settings.model_validate(
                {**self.settings.model_dump(), **restored, **changes}
            )
        except ValidationError as exc:
            message = str(exc.errors()[0].get("msg") or "配置值未通过校验")
            raise RuntimeConfigError(message.removeprefix("Value error, ")) from exc
        serialized = {
            field: (
                self.vault.encrypt(json.dumps(getattr(candidate, field), ensure_ascii=False))
                if field in SECRET_FIELDS
                else json.dumps(getattr(candidate, field), ensure_ascii=False),
                field in SECRET_FIELDS,
            )
            for field in changes
        }
        revision = self.db.set_runtime_config(
            serialized,
            delete_fields=reset_fields,
            expected_revision=expected_revision,
        )
        if revision is None:
            raise RuntimeConfigConflict(self.db.get_runtime_config_revision())
        for field in set(changes) | reset_fields:
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
