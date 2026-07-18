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
        return [
            {
                "id": "local",
                "name": "报告中心",
                "configured": True,
                "push_capable": False,
                "message": "报告保存在本机；不会向外部应用主动提醒。",
            },
            {
                "id": "feishu_webhook",
                "name": "飞书群机器人",
                "configured": bool(self.settings.feishu_webhook_url),
                "push_capable": True,
                "message": "可推送卡片；配置公网地址后卡片可直接下载报告。",
            },
            {
                "id": "feishu_app",
                "name": "飞书应用",
                "configured": app_ready,
                "push_capable": True,
                "message": "可直接发送 Word 文件与无新增回执。",
            },
            {
                "id": "email",
                "name": "电子邮件",
                "configured": email_ready,
                "push_capable": True,
                "message": "通过标准 SMTP 发送 Word 附件与无新增回执。",
            },
            {
                "id": "dingtalk_webhook",
                "name": "钉钉群机器人",
                "configured": bool(self.settings.dingtalk_webhook_url),
                "push_capable": True,
                "message": "通过钉钉自定义机器人标准 Webhook 发送 Markdown 回执。",
            },
            {
                "id": "wecom_webhook",
                "name": "企业微信群机器人",
                "configured": bool(self.settings.wecom_webhook_url),
                "push_capable": True,
                "message": "通过企业微信群机器人标准 Webhook 发送 Markdown 回执。",
            },
            {
                "id": "generic_webhook",
                "name": "通用 Webhook",
                "configured": bool(self.settings.generic_webhook_url),
                "push_capable": True,
                "message": "向用户配置的 HTTP(S) 地址发送结构化 JSON 事件。",
            },
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
    ) -> DeliveryReceipt:
        try:
            return await self._deliver_impl(
                path,
                channel,
                new_count=new_count,
                subscription_name=subscription_name,
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
        headers = {"Content-Type": "application/json", "User-Agent": "BidPilot/0.5.0"}
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
