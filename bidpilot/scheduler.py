from __future__ import annotations

import asyncio
import calendar
import os
import secrets
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
    if (
        schedule.kind == ScheduleKind.MONTHLY
        and schedule.send_time
        and schedule.day_of_month is not None
    ):
        year, month = local_after.year, local_after.month
        for _ in range(2):
            day = min(schedule.day_of_month, calendar.monthrange(year, month)[1])
            candidate = datetime.combine(
                local_after.date().replace(year=year, month=month, day=day),
                schedule.send_time,
                tzinfo=timezone,
            )
            if candidate > local_after:
                return candidate
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
    return None


def retry_time(
    after: datetime,
    consecutive_failures: int,
    *,
    random_fraction: float | None = None,
) -> datetime:
    """Return capped exponential backoff with a bounded positive jitter.

    Positive-only jitter avoids retrying earlier than the documented base delay;
    callers that also receive a platform Retry-After continue to take the later time.
    """
    delays = (60, 300, 900, 3600, 10800)
    index = min(max(consecutive_failures, 0), len(delays) - 1)
    base_delay = delays[index]
    fraction = (
        secrets.randbelow(1001) / 1000
        if random_fraction is None
        else min(max(float(random_fraction), 0.0), 1.0)
    )
    jitter = min(base_delay * 0.1, 60.0) * fraction
    return after + timedelta(seconds=base_delay + jitter)


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
        self._outbox_streak = 0

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
        checked_outbox = False
        if self._outbox_streak < 5:
            checked_outbox = True
            if await self.service.process_due_delivery_outbox(worker_id=self.worker_id):
                self._outbox_streak += 1
                return True
        now = datetime.now(self.timezone)
        row = self.service.db.claim_due_subscription(
            worker_id=self.worker_id,
            now=now,
            lease_until=now + timedelta(seconds=self.settings.worker_lease_seconds),
        )
        if row is None:
            self._outbox_streak = 0
            if checked_outbox:
                return False
            return await self.service.process_due_delivery_outbox(worker_id=self.worker_id)
        self._outbox_streak = 0
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
        execution = asyncio.create_task(
            self.service.run_subscription(
                row["id"],
                trigger_reason="schedule",
                lease_owner=self.worker_id,
            ),
            name=f"bidpilot-subscription-run:{row['id']}",
        )
        try:
            done, _ = await asyncio.wait(
                {execution, renewal},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal in done and not execution.done():
                lease_error = renewal.exception()
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
                if lease_error is not None:
                    raise lease_error
                return True
            try:
                await execution
            except Exception:
                # Service persistence contains the actionable failure and retry time.
                pass
        finally:
            if not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
            if not renewal.done():
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
