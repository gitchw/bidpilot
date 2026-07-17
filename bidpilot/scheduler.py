from __future__ import annotations

from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from bidpilot.models import ScheduleKind, TenderQuerySpec


class SubscriptionScheduler:
    def __init__(self, service):
        self.service = service
        self.timezone = ZoneInfo(service.settings.timezone)
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)

    def start(self) -> None:
        if self.scheduler.running:
            return
        self.scheduler.start()
        for row in self.service.db.list_subscriptions():
            if not row["enabled"]:
                continue
            spec = TenderQuerySpec.model_validate_json(row["spec_json"])
            self.add_subscription(row["id"], spec)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def add_subscription(self, subscription_id: str, spec: TenderQuerySpec) -> None:
        schedule = spec.schedule
        if schedule.kind == ScheduleKind.DAILY:
            trigger = CronTrigger(
                hour=schedule.send_time.hour,
                minute=schedule.send_time.minute,
                timezone=self.timezone,
            )
        elif schedule.kind == ScheduleKind.WEEKLY:
            trigger = CronTrigger(
                day_of_week=schedule.weekday,
                hour=schedule.send_time.hour,
                minute=schedule.send_time.minute,
                timezone=self.timezone,
            )
        elif schedule.kind == ScheduleKind.ONCE and schedule.run_at:
            trigger = DateTrigger(run_date=schedule.run_at)
        else:
            return
        self.scheduler.add_job(
            self.service.run_subscription,
            trigger=trigger,
            args=[subscription_id],
            id=f"subscription:{subscription_id}",
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
        )
