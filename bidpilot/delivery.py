from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx

from bidpilot.config import Settings


class DeliveryError(RuntimeError):
    pass


class DeliveryRetryAfterError(DeliveryError):
    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class DeliveryPermanentError(DeliveryError):
    pass


@dataclass(slots=True)
class DeliveryReceipt:
    channel: str
    success: bool
    message: str
    external_id: str | None = None
    skipped: bool = False


class DeliveryManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _email_recipients(self) -> list[str]:
        return [
            item.strip()
            for item in self.settings.smtp_to.replace(";", ",").split(",")
            if item.strip()
        ]

    def _email_is_configured(self) -> bool:
        return bool(
            self.settings.smtp_host
            and self.settings.smtp_from
            and self._email_recipients()
            and (not self.settings.smtp_username or self.settings.smtp_password)
        )

    def channel_status(self) -> list[dict]:
        app_ready = all(
            (
                self.settings.feishu_app_id,
                self.settings.feishu_app_secret,
                self.settings.feishu_receive_id,
            )
        )
        email_ready = self._email_is_configured()
        telegram_ready = bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

        def item(
            channel_id: str,
            name: str,
            *,
            configured: bool,
            push_capable: bool,
            supports_text: bool,
            supports_file: bool,
            supports_link: bool,
            configuration_group: str,
            message: str,
        ) -> dict:
            return {
                "id": channel_id,
                "name": name,
                "configured": configured,
                "push_capable": push_capable,
                "supports_text": supports_text,
                "supports_file": supports_file,
                "supports_link": supports_link,
                "configuration_group": configuration_group,
                "delivery_semantics": (
                    "local_persistence" if channel_id == "local" else "at_least_once"
                ),
                "message": message,
            }

        return [
            item(
                "local",
                "报告中心",
                configured=True,
                push_capable=False,
                supports_text=False,
                supports_file=True,
                supports_link=True,
                configuration_group="system",
                message="报告保存在 BidPilot 报告中心；不会向外部应用主动提醒。",
            ),
            item(
                "feishu_webhook",
                "飞书群机器人",
                configured=bool(self.settings.feishu_webhook_url),
                push_capable=True,
                supports_text=True,
                supports_file=False,
                supports_link=True,
                configuration_group="feishu",
                message="发送卡片和报告链接；群机器人不能直接上传 Word。",
            ),
            item(
                "feishu_app",
                "飞书应用",
                configured=app_ready,
                push_capable=True,
                supports_text=True,
                supports_file=True,
                supports_link=False,
                configuration_group="feishu",
                message="使用官方应用接口直接发送 Word 文件和运行回执。",
            ),
            item(
                "email",
                "电子邮件",
                configured=email_ready,
                push_capable=True,
                supports_text=True,
                supports_file=True,
                supports_link=False,
                configuration_group="email",
                message="通过标准 SMTP 发送 Word 附件和无新增回执。",
            ),
            item(
                "dingtalk_webhook",
                "钉钉群机器人",
                configured=bool(self.settings.dingtalk_webhook_url),
                push_capable=True,
                supports_text=True,
                supports_file=False,
                supports_link=True,
                configuration_group="dingtalk",
                message="通过官方自定义机器人 Webhook 发送 Markdown 和报告链接。",
            ),
            item(
                "wecom_webhook",
                "企业微信群机器人",
                configured=bool(self.settings.wecom_webhook_url),
                push_capable=True,
                supports_text=True,
                supports_file=False,
                supports_link=True,
                configuration_group="wecom",
                message="通过官方群机器人 Webhook 发送 Markdown 和报告链接。",
            ),
            item(
                "generic_webhook",
                "通用 Webhook",
                configured=bool(self.settings.generic_webhook_url),
                push_capable=True,
                supports_text=False,
                supports_file=False,
                supports_link=True,
                configuration_group="generic",
                message="向用户配置的 HTTP(S) 地址发送结构化 JSON 事件。",
            ),
            item(
                "telegram_bot",
                "Telegram Bot",
                configured=telegram_ready,
                push_capable=True,
                supports_text=True,
                supports_file=True,
                supports_link=True,
                configuration_group="telegram",
                message=(
                    "通过官方 Bot API 发送 Word 文件或纯文本回执；文件最大 50 MB，超限时仅在已配置"
                    "可访问的报告根地址后改发链接，否则进入死信并提示处理。"
                ),
            ),
            item(
                "slack_webhook",
                "Slack Incoming Webhook",
                configured=bool(self.settings.slack_webhook_url),
                push_capable=True,
                supports_text=True,
                supports_file=False,
                supports_link=True,
                configuration_group="slack",
                message="发送消息和报告链接；Incoming Webhook 本身不能上传 Word。",
            ),
        ]

    def _resolve_channel(self, channel: str) -> str:
        if channel != "feishu":
            return channel
        app = next(item for item in self.channel_status() if item["id"] == "feishu_app")
        if app["configured"]:
            return "feishu_app"
        return "feishu_webhook"

    async def deliver(
        self,
        path: Path | None,
        channel: str = "local",
        *,
        new_count: int | None = None,
        subscription_name: str | None = None,
        delivery_key: str | None = None,
    ) -> DeliveryReceipt:
        try:
            return await self._deliver_impl(
                path,
                channel,
                new_count=new_count,
                subscription_name=subscription_name,
                delivery_key=delivery_key,
            )
        except DeliveryError:
            raise
        except httpx.TimeoutException as exc:
            raise DeliveryError(f"投递通道 {channel} 连接超时") from exc
        except httpx.HTTPStatusError as exc:
            raise DeliveryError(f"投递通道 {channel} 返回 HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise DeliveryError(f"投递通道 {channel} 网络连接失败") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"投递通道 {channel} 连接或认证失败") from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DeliveryError(f"投递通道 {channel} 返回了无法识别的响应") from exc

    async def _deliver_impl(
        self,
        path: Path | None,
        channel: str = "local",
        *,
        new_count: int | None = None,
        subscription_name: str | None = None,
        delivery_key: str | None = None,
    ) -> DeliveryReceipt:
        channel = self._resolve_channel(channel)
        if channel == "local":
            message = f"报告已保存：{path}" if path else "本轮无新增，运行回执已记录。"
            return DeliveryReceipt("local", True, message)
        if channel == "feishu_webhook" and self.settings.feishu_webhook_url:
            return await self._send_feishu_webhook(path, new_count, subscription_name)
        if channel == "feishu_app" and all(
            (
                self.settings.feishu_app_id,
                self.settings.feishu_app_secret,
                self.settings.feishu_receive_id,
            )
        ):
            if path:
                return await self._send_feishu_file(path)
            return await self._send_feishu_text(new_count, subscription_name)
        if channel == "email" and self._email_is_configured():
            return await asyncio.to_thread(
                self._send_email,
                path,
                new_count,
                subscription_name,
            )
        if channel == "dingtalk_webhook" and self.settings.dingtalk_webhook_url:
            return await self._send_dingtalk(path, new_count, subscription_name)
        if channel == "wecom_webhook" and self.settings.wecom_webhook_url:
            return await self._send_wecom(path, new_count, subscription_name)
        if channel == "generic_webhook" and self.settings.generic_webhook_url:
            return await self._send_generic_webhook(path, new_count, subscription_name)
        if channel == "telegram_bot" and all(
            (self.settings.telegram_bot_token, self.settings.telegram_chat_id)
        ):
            return await self._send_telegram(
                path,
                new_count,
                subscription_name,
                delivery_key,
            )
        if channel == "slack_webhook" and self.settings.slack_webhook_url:
            return await self._send_slack_webhook(
                path,
                new_count,
                subscription_name,
                delivery_key,
            )
        raise DeliveryError(f"投递通道 {channel} 未完成配置；请在系统状态页检查所需凭据。")

    async def _send_feishu_webhook(
        self, path: Path | None, new_count: int | None, subscription_name: str | None
    ) -> DeliveryReceipt:
        title = subscription_name or "招投标情报订阅"
        if path:
            content = f"**{title}** 本轮发现 **{new_count or 0}** 条新增。\n报告：**{path.name}**"
            if self.settings.public_base_url:
                base = self.settings.public_base_url.rstrip("/")
                content += f"\n[下载 Word 报告]({base}/api/v1/reports/{quote(path.name)})"
            else:
                content += "\n报告已保存在部署主机；配置 `PUBLIC_BASE_URL` 后可在卡片中下载。"
        else:
            content = f"**{title}** 已按时完成，本轮没有新增匹配情报。"
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": "标擎 BidPilot · 新情报报告"},
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": content,
                        },
                    }
                ],
            },
        }
        if self.settings.feishu_webhook_secret:
            timestamp = str(int(time.time()))
            sign_key = f"{timestamp}\n{self.settings.feishu_webhook_secret}".encode()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(
                hmac.new(sign_key, digestmod=hashlib.sha256).digest()
            ).decode()
        async with httpx.AsyncClient(timeout=self.settings.delivery_webhook_timeout) as client:
            response = await client.post(self.settings.feishu_webhook_url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("code", data.get("StatusCode", 0)) not in {0, None}:
            raise DeliveryError(f"飞书 Webhook 返回失败：{data}")
        return DeliveryReceipt("feishu_webhook", True, "飞书运行卡片推送成功")

    def _markdown_content(
        self,
        path: Path | None,
        new_count: int | None,
        subscription_name: str | None,
    ) -> str:
        title = subscription_name or "招投标情报订阅"
        if not path:
            return f"### 标擎 BidPilot\n\n**{title}** 已按时完成，本轮没有新增匹配情报。"
        content = f"### 标擎 BidPilot\n\n**{title}** 本轮发现 **{new_count or 0}** 条新增。"
        if self.settings.public_base_url:
            base = self.settings.public_base_url.rstrip("/")
            content += f"\n\n[下载 Word 报告]({base}/api/v1/reports/{quote(path.name)})"
        else:
            content += f"\n\n报告 **{path.name}** 已保存在部署主机。"
        return content

    async def _send_dingtalk(
        self, path: Path | None, new_count: int | None, subscription_name: str | None
    ) -> DeliveryReceipt:
        url = self.settings.dingtalk_webhook_url
        if self.settings.dingtalk_webhook_secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{self.settings.dingtalk_webhook_secret}".encode()
            signature = hmac.new(
                self.settings.dingtalk_webhook_secret.encode(),
                string_to_sign,
                digestmod=hashlib.sha256,
            ).digest()
            separator = "&" if "?" in url else "?"
            url = (
                f"{url}{separator}timestamp={timestamp}"
                f"&sign={quote_plus(base64.b64encode(signature).decode())}"
            )
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "标擎 BidPilot 情报回执",
                "text": self._markdown_content(path, new_count, subscription_name),
            },
        }
        async with httpx.AsyncClient(timeout=self.settings.delivery_webhook_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("errcode", 0) != 0:
            raise DeliveryError(f"钉钉机器人返回失败（错误码 {data.get('errcode')}）")
        return DeliveryReceipt("dingtalk_webhook", True, "钉钉机器人推送成功")

    async def _send_wecom(
        self, path: Path | None, new_count: int | None, subscription_name: str | None
    ) -> DeliveryReceipt:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": self._markdown_content(path, new_count, subscription_name)},
        }
        async with httpx.AsyncClient(timeout=self.settings.delivery_webhook_timeout) as client:
            response = await client.post(self.settings.wecom_webhook_url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("errcode", 0) != 0:
            raise DeliveryError(f"企业微信机器人返回失败（错误码 {data.get('errcode')}）")
        return DeliveryReceipt("wecom_webhook", True, "企业微信机器人推送成功")

    async def _send_generic_webhook(
        self, path: Path | None, new_count: int | None, subscription_name: str | None
    ) -> DeliveryReceipt:
        report_url = None
        if path and self.settings.public_base_url:
            report_url = (
                f"{self.settings.public_base_url.rstrip('/')}/api/v1/reports/{quote(path.name)}"
            )
        payload = {
            "event": "bidpilot.report.ready" if path else "bidpilot.run.no_change",
            "occurred_at": datetime.now(UTC).isoformat(),
            "subscription": {"name": subscription_name or "招投标情报订阅"},
            "result": {
                "new_count": new_count or 0,
                "report_filename": path.name if path else None,
                "report_url": report_url,
            },
        }
        headers = {"Content-Type": "application/json", "User-Agent": "BidPilot/0.8.0"}
        if self.settings.generic_webhook_bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.generic_webhook_bearer_token}"
        async with httpx.AsyncClient(timeout=self.settings.delivery_webhook_timeout) as client:
            response = await client.post(
                self.settings.generic_webhook_url,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        return DeliveryReceipt("generic_webhook", True, "通用 Webhook 投递成功")

    def _report_url(self, path: Path | None) -> str | None:
        if not path or not self.settings.public_base_url:
            return None
        return f"{self.settings.public_base_url.rstrip('/')}/api/v1/reports/{quote(path.name)}"

    @staticmethod
    def _telegram_chat_id(value: str) -> int | str:
        cleaned = value.strip()
        if cleaned.lstrip("-").isdigit():
            return int(cleaned)
        if cleaned.startswith("@") and len(cleaned) >= 5:
            return cleaned
        raise DeliveryPermanentError("Telegram 会话 ID 必须是整数或 @username")

    def _telegram_common_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "chat_id": self._telegram_chat_id(self.settings.telegram_chat_id),
            "disable_notification": self.settings.telegram_disable_notification,
            "protect_content": self.settings.telegram_protect_content,
        }
        if self.settings.telegram_message_thread_id is not None:
            payload["message_thread_id"] = self.settings.telegram_message_thread_id
        return payload

    @staticmethod
    def _parse_telegram_response(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if response.status_code in {400, 401, 403, 404, 413}:
                raise DeliveryPermanentError(
                    f"Telegram 拒绝请求（HTTP {response.status_code}）"
                ) from exc
            if response.status_code >= 500:
                response.raise_for_status()
            raise DeliveryError("Telegram 返回了无法识别的响应") from exc
        if not isinstance(data, dict):
            raise DeliveryError("Telegram 返回了无法识别的响应")
        parameters = data.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        retry_after = parameters.get("retry_after")
        error_code = data.get("error_code")
        if response.status_code == 429 or error_code == 429 or retry_after is not None:
            retry_after = retry_after or response.headers.get("Retry-After") or 60
            try:
                retry_after_seconds = int(retry_after)
            except (TypeError, ValueError):
                retry_after_seconds = 60
            raise DeliveryRetryAfterError(
                "Telegram 触发平台限流，已按官方 retry_after 延后重试",
                retry_after_seconds,
            )
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400 or data.get("ok") is not True:
            if parameters.get("migrate_to_chat_id") is not None:
                raise DeliveryPermanentError(
                    "Telegram 群组已迁移，请在网页更新会话 ID 后手动重试死信"
                )
            message = f"Telegram 拒绝请求（错误码 {error_code or response.status_code}）"
            if response.status_code in {400, 401, 403, 404, 413} or error_code in {
                400,
                401,
                403,
                404,
                413,
            }:
                raise DeliveryPermanentError(message)
            raise DeliveryError(message)
        result = data.get("result")
        if not isinstance(result, dict):
            raise DeliveryError("Telegram 成功响应缺少消息对象")
        chat = result.get("chat")
        if not isinstance(result.get("message_id"), int) or not isinstance(chat, dict):
            raise DeliveryError("Telegram 成功响应缺少消息编号或会话对象")
        if not isinstance(chat.get("id"), int):
            raise DeliveryError("Telegram 成功响应缺少有效会话编号")
        return result

    async def _send_telegram(
        self,
        path: Path | None,
        new_count: int | None,
        subscription_name: str | None,
        delivery_key: str | None,
    ) -> DeliveryReceipt:
        title = subscription_name or "招投标情报订阅"
        reference = f"\nBidPilot 投递编号：{delivery_key}" if delivery_key else ""
        token = self.settings.telegram_bot_token
        method = "sendMessage"
        common = self._telegram_common_payload()
        report_url = self._report_url(path)
        request: dict[str, object]
        if path and path.stat().st_size <= 50_000_000:
            method = "sendDocument"
            caption = (f"标擎 BidPilot｜{title}\n本轮新增 {new_count or 0} 条。{reference}")[:1024]
            data = {
                key: str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in common.items()
            }
            data["caption"] = caption
            with path.open("rb") as handle:
                async with httpx.AsyncClient(
                    timeout=self.settings.delivery_webhook_timeout,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/{method}",
                        data=data,
                        files={
                            "document": (
                                path.name,
                                handle,
                                "application/vnd.openxmlformats-officedocument."
                                "wordprocessingml.document",
                            )
                        },
                    )
        else:
            if path and not report_url:
                raise DeliveryPermanentError("Telegram 报告超过 50 MB，且未配置可访问的报告地址")
            text = (
                f"标擎 BidPilot｜{title}\n"
                + (
                    f"本轮新增 {new_count or 0} 条。\n报告：{report_url}"
                    if path
                    else "本轮已完成，没有新增匹配情报。"
                )
                + reference
            )[:4096]
            request = {**common, "text": text}
            async with httpx.AsyncClient(
                timeout=self.settings.delivery_webhook_timeout,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/{method}",
                    json=request,
                )
        result = self._parse_telegram_response(response)
        chat = result.get("chat") if isinstance(result.get("chat"), dict) else {}
        external_id = f"{chat.get('id', self.settings.telegram_chat_id)}:{result.get('message_id')}"
        return DeliveryReceipt(
            "telegram_bot",
            True,
            "Telegram 文件发送成功" if method == "sendDocument" else "Telegram 回执发送成功",
            external_id=external_id,
        )

    async def _send_slack_webhook(
        self,
        path: Path | None,
        new_count: int | None,
        subscription_name: str | None,
        delivery_key: str | None,
    ) -> DeliveryReceipt:
        def slack_text(value: object) -> str:
            return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        title = slack_text(subscription_name or "招投标情报订阅")
        report_url = self._report_url(path)
        lines = ["标擎 BidPilot", f"{title}｜本轮新增 {new_count or 0} 条。"]
        if path and report_url:
            lines.append(f"下载 Word 报告：{slack_text(report_url)}")
        elif path:
            lines.append(f"报告 {slack_text(path.name)} 已保存在 BidPilot 部署主机的报告中心。")
        else:
            lines[1] = f"{title}｜本轮已完成，没有新增匹配情报。"
        if delivery_key:
            lines.append(f"BidPilot 投递编号：{slack_text(delivery_key)}")
        async with httpx.AsyncClient(
            timeout=self.settings.delivery_webhook_timeout,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                self.settings.slack_webhook_url,
                json={"text": "\n".join(lines)[:4000]},
            )
        if response.status_code == 429:
            try:
                retry_after = int(response.headers.get("Retry-After", "60"))
            except (TypeError, ValueError):
                retry_after = 60
            raise DeliveryRetryAfterError("Slack 触发平台限流，已延后重试", retry_after)
        if 400 <= response.status_code < 500:
            raise DeliveryPermanentError(f"Slack Webhook 返回 HTTP {response.status_code}")
        response.raise_for_status()
        if response.text.strip().lower() != "ok":
            raise DeliveryError("Slack Webhook 返回了无法识别的响应")
        return DeliveryReceipt("slack_webhook", True, "Slack 消息发送成功")

    async def _tenant_token(self, client: httpx.AsyncClient) -> str:
        token_response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            },
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        token = token_data.get("tenant_access_token")
        if not token:
            raise DeliveryError(f"无法获取飞书 tenant token：{token_data}")
        return token

    async def _send_feishu_file(self, path: Path) -> DeliveryReceipt:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._tenant_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            with path.open("rb") as handle:
                upload = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/files",
                    headers=headers,
                    data={"file_type": "stream", "file_name": path.name},
                    files={
                        "file": (
                            path.name,
                            handle,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                    },
                )
            upload.raise_for_status()
            upload_data = upload.json()
            file_key = upload_data.get("data", {}).get("file_key")
            if not file_key:
                raise DeliveryError(f"飞书文件上传失败：{upload_data}")
            send = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                headers=headers,
                params={"receive_id_type": self.settings.feishu_receive_id_type},
                json={
                    "receive_id": self.settings.feishu_receive_id,
                    "msg_type": "file",
                    "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
                },
            )
            send.raise_for_status()
            send_data = send.json()
            if send_data.get("code") != 0:
                raise DeliveryError(f"飞书文件发送失败：{send_data}")
        return DeliveryReceipt(
            "feishu_app",
            True,
            "飞书 Word 文件发送成功",
            external_id=send_data.get("data", {}).get("message_id"),
        )

    async def _send_feishu_text(
        self, new_count: int | None, subscription_name: str | None
    ) -> DeliveryReceipt:
        content = (
            f"标擎 BidPilot｜{subscription_name or '招投标情报订阅'}已按时完成，"
            f"本轮新增 {new_count or 0} 条。"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._tenant_token(client)
            response = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": self.settings.feishu_receive_id_type},
                json={
                    "receive_id": self.settings.feishu_receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": content}, ensure_ascii=False),
                },
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise DeliveryError(f"飞书运行回执发送失败：{data}")
        return DeliveryReceipt(
            "feishu_app",
            True,
            "飞书无新增回执发送成功",
            external_id=data.get("data", {}).get("message_id"),
        )

    def _send_email(
        self,
        path: Path | None,
        new_count: int | None,
        subscription_name: str | None,
    ) -> DeliveryReceipt:
        title = subscription_name or "招投标情报订阅"
        message = EmailMessage()
        message["Subject"] = (
            f"标擎 BidPilot｜{title}｜新增 {new_count or 0} 条"
            if path
            else f"标擎 BidPilot｜{title}｜本轮无新增"
        )
        message["From"] = self.settings.smtp_from
        recipients = self._email_recipients()
        message["To"] = ", ".join(recipients)
        if path:
            message.set_content(
                f"{title} 已完成，本轮发现 {new_count or 0} 条新增。\nWord 报告已作为附件发送。"
            )
            message.add_attachment(
                path.read_bytes(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=path.name,
            )
        else:
            message.set_content(f"{title} 已按时完成，本轮没有新增匹配情报。")

        context = ssl.create_default_context()
        if self.settings.smtp_security == "ssl":
            client = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout,
            )
        with client:
            client.ehlo()
            if self.settings.smtp_security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if self.settings.smtp_username:
                client.login(self.settings.smtp_username, self.settings.smtp_password)
            client.send_message(message, from_addr=self.settings.smtp_from, to_addrs=recipients)
        return DeliveryReceipt("email", True, "邮件投递成功")
