from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from bidpilot.models import IntentSchedule, ScheduleKind


def next_schedule_time(schedule: IntentSchedule, after: datetime) -> datetime | None:
    """Return the next wall-clock occurrence strictly after ``after``."""
    timezone = ZoneInfo(schedule.timezone)
    local_after = after.astimezone(timezone)
    if schedule.kind == ScheduleKind.ONCE:
        if schedule.run_at and schedule.run_at > local_after:
            return schedule.run_at.astimezone(timezone)
        return None
    if schedule.kind == ScheduleKind.DAILY and schedule.send_time:
        candidate = datetime.combine(local_after.date(), schedule.send_time, tzinfo=timezone)
        if candidate <= local_after:
            candidate += timedelta(days=1)
        return candidate
    if schedule.kind == ScheduleKind.WEEKLY and schedule.send_time and schedule.weekday is not None:
        days_ahead = (schedule.weekday - local_after.weekday()) % 7
        candidate = datetime.combine(
            local_after.date() + timedelta(days=days_ahead),
            schedule.send_time,
            tzinfo=timezone,
        )
        if candidate <= local_after:
            candidate += timedelta(days=7)
        return candidate
    return None


def retry_time(after: datetime, consecutive_failures: int) -> datetime:
    delays = (60, 300, 900, 3600, 10800)
    index = min(max(consecutive_failures, 0), len(delays) - 1)
    return after + timedelta(seconds=delays[index])


async def maintain_subscription_lease(
    db,
    subscription_id: str,
    *,
    worker_id: str,
    timezone: ZoneInfo,
    lease_seconds: float,
) -> None:
    """Keep one owned lease alive until the caller cancels this coroutine."""
    interval = max(0.05, min(float(lease_seconds) / 3, 60.0))
    while True:
        await asyncio.sleep(interval)
        now = datetime.now(timezone)
        renewed = db.renew_subscription_lease(
            subscription_id,
            worker_id=worker_id,
            lease_until=now + timedelta(seconds=lease_seconds),
        )
        if not renewed:
            return


class SubscriptionWorker:
    """Durable SQLite-backed scheduler shared by embedded and standalone modes."""

    def __init__(self, service, *, kind: str = "standalone"):
        self.service = service
        self.settings = service.settings
        self.timezone = ZoneInfo(self.settings.timezone)
        self.kind = kind
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        self.started_at = datetime.now(self.timezone)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_forever(), name="bidpilot-subscription-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self.service.db.remove_worker(self.worker_id)

    def _heartbeat(self) -> None:
        self.service.db.heartbeat_worker(
            worker_id=self.worker_id,
            kind=self.kind,
            started_at=self.started_at,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )

    async def run_once(self) -> bool:
        now = datetime.now(self.timezone)
        row = self.service.db.claim_due_subscription(
            worker_id=self.worker_id,
            now=now,
            lease_until=now + timedelta(seconds=self.settings.worker_lease_seconds),
        )
        if row is None:
            return False
        renewal = asyncio.create_task(
            maintain_subscription_lease(
                self.service.db,
                row["id"],
                worker_id=self.worker_id,
                timezone=self.timezone,
                lease_seconds=self.settings.worker_lease_seconds,
            ),
            name=f"bidpilot-lease:{row['id']}",
        )
        try:
            await self.service.run_subscription(
                row["id"],
                trigger_reason="schedule",
                lease_owner=self.worker_id,
            )
        except Exception:
            # Service persistence contains the actionable failure and retry time.
            pass
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        return True

    async def run_forever(self) -> None:
        self.service.repair_subscription_schedules()
        while not self._stop.is_set():
            self._heartbeat()
            worked = await self.run_once()
            if worked:
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.worker_poll_interval
                )
            except TimeoutError:
                continue


# Backward-compatible name for integrations built against the first prototype.
SubscriptionScheduler = SubscriptionWorker
