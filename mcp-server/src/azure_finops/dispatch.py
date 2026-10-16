"""Dispatchers: turn due schedules and detected anomalies into finops-run.yml runs (deduped, capped)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .automations import AnomalyTrigger, LoadedAutomation, due_slots, load_automations
from .config import Config
from .github import GitHub
from .post import BASE_LABEL, fp_marker

RUN_WORKFLOW = "finops-run.yml"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recent_titles(gh: GitHub, since: datetime) -> list[str]:
    return [
        r.get("display_title") or r.get("name") or "" for r in gh.workflow_runs(RUN_WORKFLOW, _iso(since))
    ]


def _key_seen(titles: list[str], key: str) -> bool:
    return any(t.split()[-1:] == [key] for t in titles)


def dispatch_schedules(
    config: Config,
    root: Path,
    gh: GitHub,
    ref: str,
    now: datetime | None = None,
    window: timedelta = timedelta(hours=3),
    dry_run: bool = False,
) -> list[dict]:
    """Dispatch every enabled schedule slot that fired within `window` and hasn't run yet."""
    now = now or datetime.now(UTC)
    due = due_slots(load_automations(root, config), now, window)
    if not due:
        return []
    titles = _recent_titles(gh, now - window - timedelta(hours=1))
    out = []
    for la, slot, fired in due:
        rec = {"automation": la.automation.name, "slot": slot, "fired_at": _iso(fired)}
        if _key_seen(titles, slot):
            out.append(rec | {"action": "skipped", "reason": "already dispatched"})
            continue
        if not dry_run:
            gh.dispatch_workflow(
                RUN_WORKFLOW,
                ref,
                {"kind": "schedule", "automation": la.automation.name, "slot": slot, "dedupe_key": slot},
            )
        titles.append(f"finops schedule {slot}")
        out.append(rec | {"action": "would_dispatch" if dry_run else "dispatched"})
    return out


def matches(la: LoadedAutomation, anomaly: dict, config: Config) -> bool:
    a = la.automation
    if not a.enabled or not isinstance(a.trigger, AnomalyTrigger):
        return False
    f = a.trigger.filter
    sub = anomaly["subscription_id"].lower()
    if sub not in a.scoped_subscriptions(config):
        return False
    if f.subscriptions and sub not in {s.lower() for s in f.subscriptions}:
        return False
    if anomaly["daily_impact"] < f.min_impact:
        return False
    pct = anomaly.get("pct_change")
    if pct is not None and pct < f.min_percent:
        return False
    dims = {k.lower(): str(v or "").lower() for k, v in (anomaly.get("dimensions") or {}).items()}
    for key, allowed in f.match.items():
        if dims.get(key.lower()) not in {x.lower() for x in allowed}:
            return False
    return True


def anomaly_dispatch(
    config: Config,
    root: Path,
    gh: GitHub,
    ref: str,
    detection: dict,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Start one investigation per new anomaly that matches an anomaly automation, up to the daily cap.

    An anomaly keeps its fingerprint while it continues (it's keyed on the first flagged day), so an
    existing issue or recent run for that fingerprint means it was already investigated.
    """
    now = now or datetime.now(UTC)
    autos = sorted(load_automations(root, config), key=lambda la: la.automation.name)
    anomalies = sorted(detection.get("anomalies", []), key=lambda x: -float(x.get("total_impact") or 0))
    out: list[dict] = []
    candidates = []
    for an in anomalies:
        la = next((x for x in autos if matches(x, an, config)), None)
        if la is None:
            out.append(
                {"fingerprint": an["fingerprint"], "action": "skipped", "reason": "no matching automation"}
            )
        else:
            candidates.append((an, la))
    if not candidates:
        return out

    since = (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bodies = [i.get("body") or "" for i in gh.issues_with_label(BASE_LABEL, state="all", since=since)]
    titles = _recent_titles(gh, now - timedelta(days=3))
    today = now.date().isoformat()
    today_runs = [
        r
        for r in gh.workflow_runs(RUN_WORKFLOW, f"{today}T00:00:00Z")
        if (r.get("display_title") or "").startswith("finops anomaly ")
    ]
    budget = config.anomaly.max_investigations_per_day - len(today_runs)

    for an, la in candidates:
        key = f"anomaly-{an['fingerprint']}"
        rec = {"fingerprint": an["fingerprint"], "automation": la.automation.name, "title": an.get("title")}
        if any(fp_marker(key) in b for b in bodies):
            out.append(rec | {"action": "skipped", "reason": "issue already exists"})
            continue
        if _key_seen(titles, key):
            out.append(rec | {"action": "skipped", "reason": "investigation already dispatched"})
            continue
        if budget <= 0:
            out.append(rec | {"action": "skipped", "reason": "max_investigations_per_day reached"})
            continue
        payload = json.dumps({k: an.get(k) for k in _PAYLOAD_KEYS if k in an}, separators=(",", ":"))
        if not dry_run:
            gh.dispatch_workflow(
                RUN_WORKFLOW,
                ref,
                {"kind": "anomaly", "automation": la.automation.name, "anomaly": payload, "dedupe_key": key},
            )
        titles.append(f"finops anomaly {key}")
        budget -= 1
        out.append(rec | {"action": "would_dispatch" if dry_run else "dispatched"})
    return out


_PAYLOAD_KEYS = (
    "fingerprint",
    "subscription_id",
    "subscription_name",
    "dimensions",
    "currency",
    "kind",
    "start_date",
    "end_date",
    "days",
    "expected_daily",
    "actual_daily",
    "daily_impact",
    "total_impact",
    "pct_change",
    "z_score",
    "title",
)
