"""Automation definitions (finops/automations/*.yaml) and schedule evaluation."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from croniter import CroniterBadDateError, croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import GUID_RE, Config, repo_root


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Schedule(_Strict):
    natural: str = Field(description="The schedule as the user said it, for example 'every Monday at 9 AM'")
    cron: str = Field(description="5-field cron expression evaluated in `timezone`")
    timezone: str = "UTC"
    business_day: Literal["first", "last"] | None = Field(
        default=None,
        description="Only fire on the first or last business day (Mon-Fri) of the month. "
        "Use with a weekday cron such as '0 9 * * 1-5'.",
    )

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        if len(v.split()) != 5 or not croniter.is_valid(v):
            raise ValueError(f"invalid 5-field cron expression: {v!r}")
        try:
            croniter(v, datetime(2000, 1, 1)).get_next(datetime)
        except CroniterBadDateError as exc:
            raise ValueError(f"cron expression never fires (impossible date): {v!r}") from exc
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class ScheduleTrigger(_Strict):
    type: Literal["schedule"]
    schedule: Schedule


class AnomalyFilter(_Strict):
    natural: str | None = Field(default=None, description="The filter as the user said it")
    min_impact: float = Field(default=0, ge=0, description="Minimum daily increase over expected")
    min_percent: float = Field(default=0, ge=0)
    subscriptions: list[Annotated[str, Field(pattern=GUID_RE)]] = Field(default_factory=list)
    match: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Dimension -> allowed values (case-insensitive), e.g. {ServiceName: [Virtual Machines]}",
    )


class AnomalyTrigger(_Strict):
    type: Literal["anomaly"]
    filter: AnomalyFilter = Field(default_factory=AnomalyFilter)


class Automation(_Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = ""
    enabled: bool = True
    trigger: ScheduleTrigger | AnomalyTrigger = Field(discriminator="type")
    instructions: str = Field(min_length=10)
    subscriptions: list[Annotated[str, Field(pattern=GUID_RE)]] = Field(
        default_factory=list, description="Subset of configured subscriptions; empty means all configured"
    )
    destinations: list[str] = Field(min_length=1, description="Destination ids from finops/config.yaml")

    def validate_against(self, config: Config) -> list[str]:
        errors: list[str] = []
        for d in self.destinations:
            if config.destination(d) is None:
                errors.append(f"{self.name}: destination {d!r} is not in finops/config.yaml destinations")
        known = set(config.subscription_ids())
        subs = list(self.subscriptions)
        if isinstance(self.trigger, AnomalyTrigger):
            subs += self.trigger.filter.subscriptions
        for s in subs:
            if s.lower() not in known:
                errors.append(f"{self.name}: subscription {s} is not configured in finops/config.yaml")
        return errors

    def scoped_subscriptions(self, config: Config) -> list[str]:
        return [s.lower() for s in self.subscriptions] or config.subscription_ids()


@dataclass
class LoadedAutomation:
    automation: Automation
    path: Path


class AutomationError(Exception):
    pass


def automations_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "finops" / "automations"


def load_automations(root: Path | None = None, config: Config | None = None) -> list[LoadedAutomation]:
    out: list[LoadedAutomation] = []
    errors: list[str] = []
    names: set[str] = set()
    d = automations_dir(root)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.y*ml")):
        if p.name.startswith("_"):
            continue
        try:
            a = Automation.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{p.name}: {exc}")
            continue
        if p.stem != a.name:
            errors.append(f"{p.name}: file name must match automation name {a.name!r}")
        if a.name in names:
            errors.append(f"{p.name}: duplicate automation name {a.name!r}")
        names.add(a.name)
        if config is not None:
            errors.extend(a.validate_against(config))
        out.append(LoadedAutomation(a, p))
    if errors:
        raise AutomationError("\n".join(errors))
    return out


def _business_day_ok(d: date, which: str | None) -> bool:
    if which is None:
        return True
    if d.weekday() >= 5:
        return False
    days_in_month = calendar.monthrange(d.year, d.month)[1]
    weekdays = [x for x in range(1, days_in_month + 1) if date(d.year, d.month, x).weekday() < 5]
    return d.day == (weekdays[0] if which == "first" else weekdays[-1])


def fire_times(schedule: Schedule, start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Fire times (UTC) in the half-open window (start_utc, end_utc]."""
    tz = ZoneInfo(schedule.timezone)
    it = croniter(schedule.cron, start_utc.astimezone(tz))
    # Like cron: fixed-hour jobs fire once when a DST fall-back repeats their wall-clock time;
    # jobs with a wildcard hour (hourly, */n) keep firing every real hour.
    fixed_hour = "*" not in schedule.cron.split()[1]
    out: list[datetime] = []
    for _ in range(10_000):
        t: datetime = it.get_next(datetime)
        if t.astimezone(UTC) > end_utc:
            break
        local = t.astimezone(tz)
        if fixed_hour and local.fold == 1 and local.replace(fold=0).utcoffset() != local.utcoffset():
            continue
        if _business_day_ok(t.date(), schedule.business_day):
            out.append(t.astimezone(UTC))
    return out


def slot_id(name: str, fire_utc: datetime) -> str:
    return f"{name}@{fire_utc.strftime('%Y%m%dT%H%MZ')}"


def due_slots(
    automations: list[LoadedAutomation], now_utc: datetime, window: timedelta = timedelta(hours=3)
) -> list[tuple[LoadedAutomation, str, datetime]]:
    """Schedule slots that fired within `window` before `now_utc`.

    The window is wider than the hourly dispatcher interval because Actions cron can be delayed;
    callers must dedupe by slot id.
    """
    out = []
    for la in automations:
        a = la.automation
        if not a.enabled or not isinstance(a.trigger, ScheduleTrigger):
            continue
        for t in fire_times(a.trigger.schedule, now_utc - window, now_utc):
            out.append((la, slot_id(a.name, t), t))
    return out


def next_fire_times(schedule: Schedule, now_utc: datetime, count: int = 3) -> list[datetime]:
    out: list[datetime] = []
    start = now_utc
    horizon = now_utc + timedelta(days=400)
    while len(out) < count and start < horizon:
        chunk = fire_times(schedule, start, start + timedelta(days=31))
        out.extend(chunk[: count - len(out)])
        start = start + timedelta(days=31)
    return out
