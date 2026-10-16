"""MCP-facing helpers for automations: draft (validate + preview) and list."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from .automations import (
    AnomalyTrigger,
    Automation,
    AutomationError,
    ScheduleTrigger,
    automations_dir,
    load_automations,
    next_fire_times,
)
from .runtime import Runtime


def draft_automation(
    rt: Runtime,
    name: str,
    instructions: str,
    destinations: list[str],
    trigger_type: str,
    description: str = "",
    natural_schedule: str | None = None,
    cron: str | None = None,
    timezone: str | None = None,
    business_day: str | None = None,
    natural_filter: str | None = None,
    min_impact: float = 0.0,
    min_percent: float = 0.0,
    filter_subscriptions: list[str] | None = None,
    match: dict[str, list[str]] | None = None,
    subscriptions: list[str] | None = None,
) -> dict:
    trigger: dict
    if trigger_type == "schedule":
        trigger = {
            "type": "schedule",
            "schedule": {
                "natural": natural_schedule or "",
                "cron": cron or "",
                "timezone": timezone or rt.config.timezone,
                "business_day": business_day,
            },
        }
    elif trigger_type == "anomaly":
        trigger = {
            "type": "anomaly",
            "filter": {
                "natural": natural_filter,
                "min_impact": min_impact,
                "min_percent": min_percent,
                "subscriptions": filter_subscriptions or [],
                "match": match or {},
            },
        }
    else:
        return {"status": "invalid", "errors": ["trigger_type must be 'schedule' or 'anomaly'"]}
    doc = {
        "name": name,
        "description": description,
        "enabled": True,
        "trigger": trigger,
        "instructions": instructions,
        "subscriptions": subscriptions or [],
        "destinations": destinations,
    }
    try:
        a = Automation.model_validate(doc)
    except ValidationError as exc:
        return {
            "status": "invalid",
            "errors": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()],
        }
    errors = a.validate_against(rt.config)
    if errors:
        return {"status": "invalid", "errors": errors}
    clean = a.model_dump(mode="json", exclude_none=True)
    if isinstance(a.trigger, AnomalyTrigger):
        clean["trigger"]["filter"] = {
            k: v for k, v in clean["trigger"]["filter"].items() if v not in ([], {}, None)
        }
    text = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=100)
    path = automations_dir(rt.root) / f"{a.name}.yaml"
    out = {
        "status": "valid",
        "path": f"finops/automations/{a.name}.yaml",
        "exists": path.exists(),
        "yaml": text,
        "activation": "Nothing is active yet. Show this to the user and confirm the schedule. Once approved, write "
        "the file and open a pull request; merging it to the default branch is the authorization.",
    }
    if isinstance(a.trigger, ScheduleTrigger):
        sch = a.trigger.schedule
        tz = ZoneInfo(sch.timezone)
        runs = next_fire_times(sch, datetime.now(UTC), 3)
        out["next_runs"] = [
            {"local": t.astimezone(tz).strftime("%a %Y-%m-%d %H:%M %Z"), "utc": t.strftime("%Y-%m-%dT%H:%MZ")}
            for t in runs
        ]
        out["schedule_note"] = (
            "GitHub Actions checks hourly and cron can be delayed, so runs start within about an hour of these times."
        )
    return out


def list_automations(rt: Runtime) -> dict:
    try:
        items = load_automations(rt.root, rt.config)
        errors: list[str] = []
    except AutomationError as exc:
        items, errors = [], str(exc).splitlines()
    out = []
    for la in items:
        a = la.automation
        row = {
            "name": a.name,
            "enabled": a.enabled,
            "trigger": a.trigger.type,
            "description": a.description,
            "destinations": a.destinations,
            "file": f"finops/automations/{la.path.name}",
        }
        if isinstance(a.trigger, ScheduleTrigger):
            row["schedule"] = a.trigger.schedule.natural
            row["cron"] = f"{a.trigger.schedule.cron} ({a.trigger.schedule.timezone})"
        else:
            row["filter"] = a.trigger.filter.model_dump(exclude_none=True)
        out.append(row)
    return {
        "status": "ok" if not errors else "invalid",
        "automations": out,
        "errors": errors,
        "note": "Run history lives in GitHub Actions; `finops automations list` shows last runs.",
    }
