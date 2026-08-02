from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bidpilot.models import TenderQuerySpec
from bidpilot.private_files import harden_private_path


class IntentSnapshotError(ValueError):
    """The browser-confirmed intent can no longer be trusted for execution."""


class IntentSnapshotSigner:
    VERSION = 1
    MAX_TOKEN_LENGTH = 100_000

    def __init__(self, key_path: Path, *, ttl: timedelta = timedelta(minutes=15)):
        self.key_path = key_path
        self.ttl = ttl

    @staticmethod
    def _normalize_query(query: str) -> str:
        return " ".join(query.split())

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        current = value or datetime.now(UTC)
        return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        try:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, UnicodeEncodeError) as exc:
            raise IntentSnapshotError("意图确认快照格式损坏，请重新解析后再执行") from exc

    def _key(self) -> bytes:
        if not self.key_path.exists():
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            harden_private_path(self.key_path.parent, directory=True)
            candidate = secrets.token_bytes(32)
            try:
                with self.key_path.open("xb") as handle:
                    handle.write(candidate)
            except FileExistsError:
                pass
        harden_private_path(self.key_path, directory=False)
        key = self.key_path.read_bytes()
        if len(key) < 32:
            raise IntentSnapshotError("本机意图签名密钥不可用，请检查 data/secrets 权限")
        return key

    def issue(self, spec: TenderQuerySpec, *, now: datetime | None = None) -> str:
        issued_at = self._utc(now)
        expires_at = issued_at + self.ttl
        payload: dict[str, Any] = {
            "v": self.VERSION,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "query": self._normalize_query(spec.raw_query),
            "spec": spec.model_dump(mode="json", exclude={"confirmation_snapshot"}),
        }
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._key(), raw, hashlib.sha256).digest()
        return f"{self._encode(raw)}.{self._encode(signature)}"

    def verify(
        self,
        query: str,
        token: str,
        *,
        now: datetime | None = None,
    ) -> TenderQuerySpec:
        if not token or len(token) > self.MAX_TOKEN_LENGTH or token.count(".") != 1:
            raise IntentSnapshotError("意图确认快照格式无效，请重新解析后再执行")
        encoded_payload, encoded_signature = token.split(".", 1)
        raw = self._decode(encoded_payload)
        signature = self._decode(encoded_signature)
        expected = hmac.new(self._key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise IntentSnapshotError("意图确认快照签名不匹配，请重新解析后再执行")
        try:
            payload = json.loads(raw)
            version = int(payload["v"])
            issued_at = int(payload["iat"])
            expires_at = int(payload["exp"])
            snapshot_query = str(payload["query"])
            spec_payload = payload["spec"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntentSnapshotError("意图确认快照内容损坏，请重新解析后再执行") from exc
        current = int(self._utc(now).timestamp())
        ttl_seconds = int(self.ttl.total_seconds())
        if (
            version != self.VERSION
            or issued_at > current + 30
            or expires_at - issued_at > ttl_seconds
        ):
            raise IntentSnapshotError("意图确认快照版本或时效无效，请重新解析后再执行")
        if expires_at < current:
            raise IntentSnapshotError("意图确认快照已过期，请重新解析并确认后再执行")
        normalized_query = self._normalize_query(query)
        if snapshot_query != normalized_query:
            raise IntentSnapshotError("问题内容已改变，旧意图确认快照不能继续使用")
        try:
            spec = TenderQuerySpec.model_validate(spec_payload)
        except ValueError as exc:
            raise IntentSnapshotError("意图确认快照中的结构化字段已失效，请重新解析") from exc
        if self._normalize_query(spec.raw_query) != normalized_query:
            raise IntentSnapshotError("意图确认快照与原问题不一致，请重新解析")
        spec.confirmation_snapshot = None
        return spec
