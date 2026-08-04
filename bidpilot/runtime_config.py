from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
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
    "telegram_bot_token",
    "slack_webhook_url",
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
        "telegram_bot_token",
        "slack_webhook_url",
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
        "telegram_bot_token",
        "telegram_chat_id",
        "telegram_message_thread_id",
        "telegram_disable_notification",
        "telegram_protect_content",
        "slack_webhook_url",
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
        "trusted_proxy_networks",
        "enterprise_allowed_origins",
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
        "trusted_proxy_networks",
        "enterprise_allowed_origins",
        "lan_admin_token",
        "port",
    }
)
ENTERPRISE_ENV_LOCKED_FIELDS: frozenset[str] = RESTART_REQUIRED_FIELDS

URL_FIELDS = {
    "llm_base_url",
    "feishu_webhook_url",
    "public_base_url",
    "dingtalk_webhook_url",
    "wecom_webhook_url",
    "generic_webhook_url",
    "slack_webhook_url",
}


class RuntimeConfigUpdate(BaseModel):
    """网页配置中心允许写入的完整白名单。未提交字段保持原值。"""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0, description="读取配置时返回的版本号；旧版本写入会返回 409")

    llm_base_url: str | None = Field(
        default=None,
        max_length=2000,
        description="OpenAI-compatible 服务根地址，例如 http://127.0.0.1:8045/v1；普通字段，保存后立即生效",
    )
    llm_api_key: SecretStr | None = Field(
        default=None,
        description="模型服务 API Key；只写入本机加密存储，读取、日志和错误均不回显",
    )
    llm_model: str | None = Field(
        default=None,
        max_length=200,
        description="发送给兼容接口的模型 ID；需与服务端实际开放名称完全一致，保存后立即生效",
    )
    llm_timeout: float | None = Field(
        default=None,
        ge=3,
        le=120,
        description="单次模型请求最长等待秒数，范围 3～120；越长越能容忍慢模型，也会延长页面等待",
    )
    intent_llm_mode: Literal["off", "auto", "always"] | None = Field(
        default=None,
        description="意图 LLM 模式：off 只用规则，auto 仅低置信/冲突时调用，always 每次都复核；可能产生模型费用",
    )
    intent_llm_confidence_threshold: float | None = Field(
        default=None,
        ge=0.5,
        le=0.99,
        description="auto 模式触发阈值，范围 0.50～0.99；规则字段最低置信度低于该值才请求模型",
    )
    retrieval_llm_mode: Literal["off", "auto"] | None = Field(
        default=None,
        description="检索规划模型开关：auto 可提议发现词，off 只用本地受控扩展；模型词不会直接成为可信命中词",
    )
    retrieval_max_rounds: int | None = Field(
        default=None,
        ge=1,
        le=2,
        description="单次任务最多检索轮数，范围 1～2；第二轮仅在覆盖缺口明确时触发，会增加网络请求",
    )
    retrieval_query_budget_per_source: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="每个来源每轮最多使用的查询变体数，范围 1～5；增大可能提升召回，也会增加耗时和站点负载",
    )
    retrieval_semantic_review: bool | None = Field(
        default=None,
        description="是否让模型复核通过地域/日期等硬过滤后的主题边界候选；关闭后只使用确定性字面规则",
    )
    retrieval_semantic_threshold: float | None = Field(
        default=None,
        ge=0.5,
        le=0.99,
        description="语义复核最低接受分，范围 0.50～0.99；分数只是辅助，仍必须提供候选正文中的逐字证据引句",
    )
    retrieval_semantic_candidate_limit: int | None = Field(
        default=None,
        ge=1,
        le=30,
        description="单轮最多送模型复核的边界候选数，范围 1～30；超过预算的候选不会被模型放行",
    )
    record_summary_mode: Literal["off", "auto"] | None = Field(
        default=None,
        description="逐公告摘要角色：auto 在模型可用时生成受证据约束的短摘要，off 使用本地结构化摘要",
    )
    record_summary_max_records: int | None = Field(
        default=None,
        ge=0,
        le=30,
        description="单轮最多调用模型摘要的公告数，范围 0～30；0 等同不调用，可直接控制费用上限",
    )
    record_summary_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=8,
        description="逐公告模型摘要并发数，范围 1～8；过高可能触发模型服务限流或占满本地推理资源",
    )
    record_summary_max_chars: int | None = Field(
        default=None,
        ge=500,
        le=12000,
        description="每条公告送入摘要模型的最大正文字符数，范围 500～12000；截断状态会在结果中诚实披露",
    )
    intelligence_brief_mode: Literal["off", "auto"] | None = Field(
        default=None,
        description="情报简报模型开关：auto 从本轮固定证据生成需求、风险和行动建议，off 使用确定性回退",
    )
    intelligence_brief_max_records: int | None = Field(
        default=None,
        ge=3,
        le=25,
        description="情报简报最多使用的高优先证据数，范围 3～25；不会改变最终检索保留数量",
    )
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

    feishu_webhook_url: SecretStr | None = Field(
        default=None,
        description="飞书群机器人 Webhook 完整地址；加密保存且不回显，用于发送文字卡片和安全报告链接",
    )
    feishu_webhook_secret: SecretStr | None = Field(
        default=None,
        description="飞书群机器人可选签名密钥；配置后每次请求附加时间戳签名，只写不回显",
    )
    feishu_app_id: str | None = Field(
        default=None,
        max_length=200,
        description="飞书自建应用 App ID；与 App Secret、接收 ID 同时配置后可上传并发送 Word 文件",
    )
    feishu_app_secret: SecretStr | None = Field(
        default=None,
        description="飞书自建应用 App Secret；仅用于换取 tenant token，本机加密保存且永不回显",
    )
    feishu_receive_id: str | None = Field(
        default=None,
        max_length=300,
        description="飞书应用消息接收者 ID，其格式必须与 receive_id_type 对应，例如 chat_id 或 open_id",
    )
    feishu_receive_id_type: Literal["chat_id", "open_id", "user_id", "union_id", "email"] | None = (
        Field(
            default=None,
            description="飞书接收 ID 类型：群聊 chat_id，或 open_id/user_id/union_id/email；填错会导致平台拒绝",
        )
    )
    public_base_url: str | None = Field(
        default=None,
        max_length=2000,
        description="已有 TLS 与访问控制的报告公开根地址；供只支持链接的渠道或超大文件使用，留空绝不虚构公网下载",
    )

    smtp_host: str | None = Field(
        default=None,
        max_length=500,
        description="SMTP 服务器主机名，例如 smtp.example.com；与端口和安全模式共同决定邮件连接方式",
    )
    smtp_port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="SMTP 端口，范围 1～65535；常见 SSL 为 465、STARTTLS 为 587，以邮件服务商文档为准",
    )
    smtp_security: Literal["ssl", "starttls", "plain"] | None = Field(
        default=None,
        description="SMTP 加密方式：ssl 建连即加密，starttls 建连后升级，plain 不加密且只建议受控内网",
    )
    smtp_username: str | None = Field(
        default=None,
        max_length=500,
        description="SMTP 登录账号，通常是完整邮箱地址；某些服务商要求专用账号名",
    )
    smtp_password: SecretStr | None = Field(
        default=None,
        description="SMTP 密码或应用专用授权码；本机加密保存，读取和错误均不回显",
    )
    smtp_from: str | None = Field(
        default=None,
        max_length=500,
        description="邮件 From 发件地址；需符合服务商授权，否则可能被拒绝或改写",
    )
    smtp_to: str | None = Field(
        default=None,
        max_length=2000,
        description="收件人列表，可用逗号、分号或换行分隔；每轮会向全部解析成功的地址发送同一报告",
    )
    smtp_timeout: float | None = Field(
        default=None,
        ge=3,
        le=120,
        description="SMTP 建连、登录和发送的最长等待秒数，范围 3～120；超时会进入该目标的有限重试",
    )

    dingtalk_webhook_url: SecretStr | None = Field(
        default=None,
        description="钉钉群机器人 Webhook 完整地址；本机加密保存，用于 Markdown 消息和报告链接",
    )
    dingtalk_webhook_secret: SecretStr | None = Field(
        default=None,
        description="钉钉机器人可选加签密钥；配置后生成官方时间戳签名，只写不回显",
    )
    wecom_webhook_url: SecretStr | None = Field(
        default=None,
        description="企业微信群机器人 Webhook 完整地址；加密保存，用于 Markdown 消息和安全报告链接",
    )
    generic_webhook_url: SecretStr | None = Field(
        default=None,
        description="用户自有自动化平台的 HTTP/HTTPS Webhook；发送结构化 JSON，必须由用户确认接收方可信",
    )
    generic_webhook_bearer_token: SecretStr | None = Field(
        default=None,
        description="通用 Webhook 可选 Bearer Token；仅放入 Authorization 请求头，加密保存且错误中脱敏",
    )
    telegram_bot_token: SecretStr | None = Field(
        default=None,
        description="Telegram 官方 BotFather 签发的 Bot Token；仅加密保存且永不回显",
    )
    telegram_chat_id: str | None = Field(
        default=None,
        max_length=200,
        description="接收消息的整数 chat_id，或公开频道的 @username",
    )
    telegram_message_thread_id: int | None = Field(
        default=None,
        ge=1,
        description="可选的话题 message_thread_id；留空表示发送到会话主区域",
    )
    telegram_disable_notification: bool | None = Field(
        default=None,
        description="是否静默发送 Telegram 消息，不触发接收端声音提醒",
    )
    telegram_protect_content: bool | None = Field(
        default=None,
        description="是否请求 Telegram 保护消息内容，限制转发与保存",
    )
    slack_webhook_url: SecretStr | None = Field(
        default=None,
        description="Slack 官方 Incoming Webhook HTTPS 地址；只支持消息和报告链接",
    )
    delivery_webhook_timeout: float | None = Field(
        default=None,
        ge=3,
        le=120,
        description="飞书/钉钉/企微/通用/Telegram/Slack 等 HTTP 投递最长等待秒数，范围 3～120；保存后立即生效",
    )

    request_timeout: float | None = Field(
        default=None,
        ge=3,
        le=120,
        description="单个来源 HTTP 请求最长等待秒数，范围 3～120；超时只隔离该来源，不让其他来源一起失败",
    )
    request_interval: float | None = Field(
        default=None,
        ge=0.1,
        le=10,
        description="同一来源相邻请求的最小礼貌间隔秒数，范围 0.1～10；调小会增加被限流风险",
    )
    max_results_per_source: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="每个来源进入统一过滤前的候选上限，范围 1～100；不是最终可信结果数量",
    )
    ccgp_max_pages: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="中国政府采购网单次查询最多读取页数，范围 1～20；页数越多越慢且请求量越大",
    )
    worker_poll_interval: float | None = Field(
        default=None,
        ge=0.2,
        le=300,
        description="长期 worker 空闲时检查到期订阅和 Outbox 的间隔秒数，范围 0.2～300；保存后立即生效",
    )
    worker_lease_seconds: int | None = Field(
        default=None,
        ge=30,
        le=7200,
        description="worker 单次领取订阅的租约秒数，范围 30～7200；运行中会续租，过短会增加误接管风险",
    )
    worker_heartbeat_ttl: int | None = Field(
        default=None,
        ge=5,
        le=600,
        description="网页判断 worker 在线的心跳有效秒数，范围 5～600；只影响状态判断，不删除任务",
    )
    network_access_mode: Literal["local", "lan", "enterprise"] | None = Field(
        default=None,
        description="重启后的监听范围：local 仅本机，lan 为兼容局域网，enterprise 为 HTTPS 企业内网",
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
    trusted_proxy_networks: str | None = Field(
        default=None,
        max_length=1000,
        description="受信反向代理 CIDR；仅这些直连节点提交的转发头会被解析，空值表示完全不信任代理头",
    )
    enterprise_allowed_origins: str | None = Field(
        default=None,
        max_length=2000,
        description="企业浏览器允许的 HTTPS Origin，逗号分隔且必须精确匹配，不支持通配符",
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

    clear_secrets: list[SecretField] = Field(
        default_factory=list,
        description="要显式清除的敏感字段名；清除不可恢复，留空或省略敏感输入表示保持原值",
    )
    reset_fields: list[str] = Field(
        default_factory=list,
        max_length=len(RESETTABLE_FIELDS),
        description="要恢复到环境变量或程序默认值的普通字段名；敏感字段不允许通过此列表恢复",
    )

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
        "slack_webhook_url",
    )
    @classmethod
    def validate_secret_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        plain = cls._validate_url(value.get_secret_value())
        return SecretStr(plain or "")

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_telegram_bot_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        plain = value.get_secret_value().strip()
        if plain and not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{10,}", plain):
            raise ValueError("Telegram Bot Token 格式不正确")
        return SecretStr(plain)

    @field_validator("telegram_chat_id")
    @classmethod
    def validate_telegram_chat_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if cleaned and not re.fullmatch(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{3,}", cleaned):
            raise ValueError("Telegram 会话 ID 必须是整数或 @username")
        return cleaned

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


class TelegramConfigView(BaseModel):
    bot_token: SecretState = Field(description="Bot Token 是否已经加密保存；永不返回原文")
    chat_id: str = Field(description="当前接收会话 ID；普通配置字段可在网页恢复来源值")
    message_thread_id: int | None = Field(description="可选话题 ID；空值表示会话主区域")
    disable_notification: bool = Field(description="是否静默发送，不触发声音通知")
    protect_content: bool = Field(description="是否请求平台限制消息转发和保存")
    ready: bool = Field(description="Token 与会话 ID 是否均已通过配置校验")


class SlackConfigView(BaseModel):
    webhook_url: SecretState = Field(
        description="官方 Incoming Webhook 是否已经加密保存；永不返回完整地址"
    )
    ready: bool = Field(description="官方 HTTPS /services 路径是否已完成配置")


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
    access_mode: Literal["local", "lan", "enterprise"] = Field(
        description="已保存、重启后生效的访问范围"
    )
    access_policy: Literal["admin_token", "trusted_lan"] = Field(
        description="已保存、重启后生效的 LAN 写操作保护方式"
    )
    trusted_networks: str = Field(description="已保存、重启后生效的可信私网范围")
    trusted_proxy_networks: str = Field(description="已保存、重启后生效的受信代理网段")
    enterprise_allowed_origins: str = Field(description="已保存的企业 HTTPS 浏览器来源")
    port: int = Field(description="已保存、重启后生效的服务端口")
    bind_host_after_restart: str = Field(description="按已保存范围计算的重启后监听地址")
    effective_access_mode: Literal["local", "lan", "enterprise"] = Field(
        description="当前进程实际使用的访问范围"
    )
    effective_access_policy: Literal["admin_token", "trusted_lan"] = Field(
        description="当前进程实际使用的 LAN 写操作保护方式"
    )
    effective_trusted_networks: str = Field(description="当前进程实际使用的可信私网范围")
    effective_trusted_proxy_networks: str = Field(description="当前进程实际信任的代理网段")
    effective_enterprise_allowed_origins: str = Field(description="当前进程允许的 HTTPS Origin")
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
    telegram: TelegramConfigView
    slack: SlackConfigView
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
        self.environment_locked_fields = (
            ENTERPRISE_ENV_LOCKED_FIELDS
            if settings.network_access_mode == "enterprise"
            else frozenset()
        )
        self.vault = LocalSecretVault(settings.data_dir / "secrets" / "runtime_config.key")
        self._persisted_fields: set[str] = set()
        self.load_persisted()
        self._effective_restart_values = {
            field: getattr(settings, field) for field in RESTART_REQUIRED_FIELDS
        }
        self._effective_bind_host = settings.host

    def effective_restart_value(self, field: str) -> object:
        if field not in RESTART_REQUIRED_FIELDS:
            raise KeyError(f"{field} 不是需重启生效的配置")
        return self._effective_restart_values[field]

    def set_effective_endpoint(
        self,
        *,
        host: str,
        port: int,
        access_mode: Literal["local", "lan", "enterprise"] | None = None,
    ) -> None:
        """Record the endpoint this process actually bound, including CLI overrides."""
        normalized = host.strip("[]").lower()
        try:
            loopback = normalized == "localhost" or ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            loopback = False
        self._effective_restart_values["network_access_mode"] = access_mode or (
            "local" if loopback else "lan"
        )
        self._effective_restart_values["port"] = int(port)
        self._effective_bind_host = host

    def load_persisted(self) -> None:
        rows = self.db.get_runtime_config()
        persisted: dict[str, object] = {}
        legacy_secrets: dict[str, tuple[str, bool]] = {}
        for field, row in rows.items():
            if field not in RUNTIME_FIELDS or field in self.environment_locked_fields:
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
        active_fields = set(persisted)
        removed_fields = self._persisted_fields - active_fields
        if not active_fields and not removed_fields:
            return
        candidate = Settings.model_validate(
            {
                **self.settings.model_dump(),
                **{field: self.base_values[field] for field in removed_fields},
                **persisted,
            }
        )
        for field in active_fields | removed_fields:
            setattr(self.settings, field, getattr(candidate, field))
        self._persisted_fields = active_fields

    def snapshot(self) -> RuntimeConfigView:
        def secret(field: str) -> SecretState:
            return SecretState(configured=bool(getattr(self.settings, field)))

        rows = self.db.get_runtime_config()
        metadata: dict[str, ConfigFieldMetadata] = {}
        for field in sorted(RUNTIME_FIELDS):
            row = rows.get(field)
            if field in self.environment_locked_fields:
                source = "environment"
                updated_at = None
            elif row is not None:
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
                trusted_proxy_networks=self.settings.trusted_proxy_networks,
                enterprise_allowed_origins=self.settings.enterprise_allowed_origins,
                port=self.settings.port,
                bind_host_after_restart=(
                    "0.0.0.0"
                    if self.settings.network_access_mode == "lan"
                    else (
                        self.settings.host
                        if self.settings.network_access_mode == "enterprise"
                        else "127.0.0.1"
                    )
                ),
                effective_access_mode=self.effective_restart_value("network_access_mode"),
                effective_access_policy=self.effective_restart_value("lan_access_policy"),
                effective_trusted_networks=self.effective_restart_value("lan_trusted_networks"),
                effective_trusted_proxy_networks=self.effective_restart_value(
                    "trusted_proxy_networks"
                ),
                effective_enterprise_allowed_origins=self.effective_restart_value(
                    "enterprise_allowed_origins"
                ),
                effective_port=self.effective_restart_value("port"),
                effective_bind_host=self._effective_bind_host,
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
            telegram=TelegramConfigView(
                bot_token=secret("telegram_bot_token"),
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=self.settings.telegram_message_thread_id,
                disable_notification=self.settings.telegram_disable_notification,
                protect_content=self.settings.telegram_protect_content,
                ready=bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id),
            ),
            slack=SlackConfigView(
                webhook_url=secret("slack_webhook_url"),
                ready=bool(self.settings.slack_webhook_url),
            ),
            field_metadata=metadata,
            updated_at=max(updated_values, default=None),
            security_notice=(
                "敏感值使用本机密钥加密保存且仅显示“已配置”；留空表示保持原值，"
                "清除必须显式勾选。"
                "默认服务只监听本机；主动开启局域网模式后，其他设备的写操作必须携带"
                "独立管理员令牌，或明确选择仅对可信私有网段免令牌。网络配置统一在"
                "重启后生效；局域网 HTTP 不加密，也不等于公网安全方案。企业模式要求"
                "HTTPS、受信客户端/代理、精确 Origin 和所有业务 API 的管理员令牌。"
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
        locked_changes = (
            set(changes) | clear_secrets | reset_fields
        ) & self.environment_locked_fields
        if locked_changes:
            raise RuntimeConfigError(
                "企业模式的网络安全边界由服务器环境强制锁定，不能通过网页覆盖："
                + ", ".join(sorted(locked_changes))
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
        self._persisted_fields = (self._persisted_fields | set(changes)) - reset_fields
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
