from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bidpilot.intent import IntentParser
from bidpilot.intent_snapshot import IntentSnapshotError, IntentSnapshotSigner


def _spec(now: datetime):
    return IntentParser("Asia/Shanghai").parse(
        "最近1个月深圳充电桩招标信息",
        now=now,
    )


def test_signed_snapshot_round_trip_binds_query_and_structured_intent(tmp_path):
    now = datetime(2026, 7, 24, 8, 30, tzinfo=UTC)
    signer = IntentSnapshotSigner(tmp_path / "secrets" / "intent_snapshot.key")
    original = _spec(now)

    token = signer.issue(original, now=now)
    verified = signer.verify(original.raw_query, token, now=now + timedelta(minutes=5))

    assert verified.topic == original.topic
    assert verified.region == "深圳"
    assert verified.start_date == original.start_date
    assert verified.confirmation_snapshot is None
    assert (tmp_path / "secrets" / "intent_snapshot.key").stat().st_size >= 32


def test_snapshot_rejects_query_change_tampering_and_expiry(tmp_path):
    now = datetime(2026, 7, 24, 8, 30, tzinfo=UTC)
    signer = IntentSnapshotSigner(tmp_path / "secrets" / "intent_snapshot.key")
    original = _spec(now)
    token = signer.issue(original, now=now)

    with pytest.raises(IntentSnapshotError, match="问题内容已改变"):
        signer.verify("最近1个月广州充电桩招标信息", token, now=now)
    payload, signature = token.split(".", 1)
    tampered = payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(IntentSnapshotError, match="签名不匹配"):
        signer.verify(original.raw_query, tampered, now=now)
    with pytest.raises(IntentSnapshotError, match="已过期"):
        signer.verify(original.raw_query, token, now=now + timedelta(minutes=16))
