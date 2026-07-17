from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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


class DeliveryManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def deliver(self, path: Path, channel: str = "local") -> DeliveryReceipt:
        if channel == "local":
            return DeliveryReceipt("local", True, f"报告已保存：{path}")
        if channel in {"feishu", "feishu_webhook"} and self.settings.feishu_webhook_url:
            return await self._send_webhook(path)
        if channel in {"feishu", "feishu_app"} and self.settings.feishu_app_id:
            return await self._send_feishu_file(path)
        raise DeliveryError(f"投递通道 {channel} 未完成配置")

    async def _send_webhook(self, path: Path) -> DeliveryReceipt:
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
                            "content": f"报告 **{path.name}** 已生成。\n本地路径：`{path}`",
                        },
                    }
                ],
            },
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.settings.feishu_webhook_url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("code", data.get("StatusCode", 0)) not in {0, None}:
            raise DeliveryError(f"飞书 Webhook 返回失败：{data}")
        return DeliveryReceipt("feishu_webhook", True, "飞书卡片推送成功")

    async def _send_feishu_file(self, path: Path) -> DeliveryReceipt:
        async with httpx.AsyncClient(timeout=30) as client:
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
