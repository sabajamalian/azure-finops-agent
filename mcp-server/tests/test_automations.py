"""Automation models, file loading, and schedule evaluation (time zones, DST, business days)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from azure_finops.automations import (
    AnomalyTrigger,
    Automation,
    AutomationError,
    Schedule,
    ScheduleTrigger,
    automations_dir,
    due_slots,
    fire_times,
    load_automations,
    next_fire_times,
    slot_id,
)
from azure_finops.simulator.synthetic import DEV, PROD, synthetic_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def sched(cron: str, tz: str = "UTC", business_day: str | None = None) -> Schedule:
    return Schedule(natural="test", cron=cron, timezone=tz, business_day=business_day)


def schedule_doc(name: str = "weekly-report", **overrides) -> dict:
    doc = {
        "name": name,
        "description": "weekly",
        "enabled": True,
        "trigger": {
            "type": "schedule",
            "schedule": {"natural": "Mondays 9am", "cron": "0 9 * * 1", "timezone": "America/Los_Angeles"},
        },
        "instructions": "Summarize last week's spend by service.",
        "destinations": ["finops-team"],
    }
    doc.update(overrides)
    return doc


def anomaly_doc(name: str = "big-anomalies", **overrides) -> dict:
    doc = {
        "name": name,
        "trigger": {"type": "anomaly", "filter": {"min_impact": 100, "subscriptions": [PROD]}},
        "instructions": "Investigate anomalies over 100 a day.",
        "destinations": ["platform-team"],
    }
    doc.update(overrides)
    return doc


def write(repo: Path, filename: str, doc) -> Path:
    p = repo / "finops" / "automations" / filename
    p.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc), encoding="utf-8")
    return p


# -- model validation ---------------------------------------------------------------------------


def test_schedule_automation_parses():
    a = Automation.model_validate(schedule_doc())
    assert isinstance(a.trigger, ScheduleTrigger)
    assert a.trigger.schedule.timezone == "America/Los_Angeles" and a.trigger.schedule.business_day is None
    assert a.validate_against(synthetic_config()) == []
    assert a.scoped_subscriptions(synthetic_config()) == [PROD, DEV]


def test_anomaly_automation_parses_with_defaults():
    a = Automation.model_validate(
        {"name": "a", "trigger": {"type": "anomaly"}, "instructions": "x" * 10, "destinations": ["d"]}
    )
    assert isinstance(a.trigger, AnomalyTrigger)
    f = a.trigger.filter
    assert (f.min_impact, f.min_percent, f.subscriptions, f.match) == (0, 0, [], {})
    assert a.enabled is True and a.subscriptions == []


@pytest.mark.parametrize(
    "cron",
    ["0 9 * *", "0 9 * * * *", "61 9 * * *", "0 25 * * *", "0 9 * * 8", "every monday", "", "@daily"],
)
def test_bad_cron_rejected(cron):
    with pytest.raises(ValidationError, match="invalid 5-field cron"):
        sched(cron)


@pytest.mark.parametrize("tz", ["Mars/Olympus", "PST8PDT/x", "", "../etc/passwd"])
def test_bad_timezone_rejected(tz):
    with pytest.raises(ValidationError, match="unknown timezone"):
        sched("0 9 * * *", tz)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"name": "Weekly"}, "name"),
        ({"name": "-weekly"}, "name"),
        ({"name": "weekly_report"}, "name"),
        ({"name": "a" * 64}, "name"),
        ({"instructions": "too short"}, "instructions"),
        ({"destinations": []}, "destinations"),
        ({"subscriptions": ["not-a-guid"]}, "subscriptions"),
        ({"owner": "me"}, "owner"),
        ({"trigger": {"type": "webhook"}}, "trigger"),
        (
            {"trigger": {"type": "schedule", "schedule": {"natural": "x", "cron": "0 9 * * *", "extra": 1}}},
            "extra",
        ),
        (
            {
                "trigger": {
                    "type": "schedule",
                    "schedule": {"natural": "x", "cron": "0 9 * * *", "business_day": "middle"},
                }
            },
            "business_day",
        ),
    ],
)
def test_invalid_automation_fields(overrides, fragment):
    with pytest.raises(ValidationError) as exc:
        Automation.model_validate(schedule_doc(**overrides))
    assert fragment in str(exc.value)


def test_anomaly_filter_validation():
    with pytest.raises(ValidationError):
        Automation.model_validate(anomaly_doc(trigger={"type": "anomaly", "filter": {"min_impact": -1}}))
    with pytest.raises(ValidationError):
        Automation.model_validate(
            anomaly_doc(trigger={"type": "anomaly", "filter": {"subscriptions": ["x"]}})
        )


def test_validate_against_reports_unknown_destinations_and_subscriptions():
    cfg = synthetic_config()
    other = "33333333-3333-3333-3333-333333333333"
    a = Automation.model_validate(
        anomaly_doc(
            subscriptions=[PROD.upper(), other],
            destinations=["finops-team", "nope"],
            trigger={
                "type": "anomaly",
                "filter": {"subscriptions": [DEV, "44444444-4444-4444-4444-444444444444"]},
            },
        )
    )
    errors = a.validate_against(cfg)
    assert errors == [
        "big-anomalies: destination 'nope' is not in finops/config.yaml destinations",
        f"big-anomalies: subscription {other} is not configured in finops/config.yaml",
        "big-anomalies: subscription 44444444-4444-4444-4444-444444444444 is not configured in finops/config.yaml",
    ]
    assert a.scoped_subscriptions(cfg) == [PROD, other]


# -- loading files ------------------------------------------------------------------------------


def test_load_automations(repo):
    write(repo, "weekly-report.yaml", schedule_doc(enabled=False))
    write(repo, "big-anomalies.yml", anomaly_doc())
    write(repo, "_template.yaml", "this: is: not: valid")
    write(repo, "notes.txt", "ignored")
    loaded = load_automations(repo, synthetic_config())
    assert [la.automation.name for la in loaded] == ["big-anomalies", "weekly-report"]
    assert loaded[1].automation.enabled is False  # disabled automations still load
    assert loaded[1].path == automations_dir(repo) / "weekly-report.yaml"


def test_load_automations_missing_dir_or_empty(repo, tmp_path):
    assert load_automations(tmp_path / "nowhere") == []
    assert load_automations(repo) == []


def test_load_automations_collects_all_errors(repo):
    write(repo, "wrong-name.yaml", schedule_doc(name="weekly-report"))
    write(
        repo,
        "bad-cron.yaml",
        schedule_doc(
            name="bad-cron", trigger={"type": "schedule", "schedule": {"natural": "x", "cron": "0 9 * *"}}
        ),
    )
    write(repo, "empty.yaml", "")
    write(repo, "unknown-dest.yaml", schedule_doc(name="unknown-dest", destinations=["nowhere"]))
    with pytest.raises(AutomationError) as exc:
        load_automations(repo, synthetic_config())
    msg = str(exc.value)
    lines = msg.splitlines()
    assert "invalid 5-field cron" in msg.split("bad-cron.yaml:", 1)[1].split("empty.yaml:", 1)[0]
    assert any(line.startswith("empty.yaml:") for line in lines)
    assert "wrong-name.yaml: file name must match automation name 'weekly-report'" in lines
    assert "unknown-dest: destination 'nowhere' is not in finops/config.yaml destinations" in lines


def test_load_automations_without_config_skips_cross_checks(repo):
    write(repo, "unknown-dest.yaml", schedule_doc(name="unknown-dest", destinations=["nowhere"]))
    assert [la.automation.name for la in load_automations(repo)] == ["unknown-dest"]


def test_duplicate_names_rejected(repo):
    write(repo, "dup.yaml", schedule_doc(name="dup"))
    write(repo, "dup.yml", schedule_doc(name="dup"))
    with pytest.raises(AutomationError, match="dup.yml: duplicate automation name 'dup'"):
        load_automations(repo)


def test_repository_sample_automations_are_valid():
    loaded = load_automations(REPO_ROOT)
    names = {la.automation.name for la in loaded}
    assert {"weekly-spend-summary", "investigate-anomalies"} <= names


# -- fire_times ---------------------------------------------------------------------------------


def test_fire_times_window_is_half_open():
    s = sched("0 9 * * *")
    assert fire_times(s, utc(2026, 3, 1, 9), utc(2026, 3, 3, 9)) == [utc(2026, 3, 2, 9), utc(2026, 3, 3, 9)]
    assert fire_times(s, utc(2026, 3, 1, 9), utc(2026, 3, 2, 8, 59)) == []


def test_fire_times_dst_spring_forward_shifts_utc_hour():
    s = sched("0 9 * * *", "America/Los_Angeles")
    assert fire_times(s, utc(2026, 3, 6), utc(2026, 3, 10)) == [
        utc(2026, 3, 6, 17),
        utc(2026, 3, 7, 17),
        utc(2026, 3, 8, 16),  # PDT starts 2026-03-08
        utc(2026, 3, 9, 16),
    ]


def test_fire_times_dst_fall_back_shifts_utc_hour():
    s = sched("0 9 * * *", "America/Los_Angeles")
    assert fire_times(s, utc(2026, 10, 31), utc(2026, 11, 3)) == [
        utc(2026, 10, 31, 16),
        utc(2026, 11, 1, 17),  # PST from 2026-11-01
        utc(2026, 11, 2, 17),
    ]


def test_fire_times_in_spring_forward_gap_fires_once_after_the_gap():
    s = sched("30 2 * * *", "America/Los_Angeles")  # 02:30 doesn't exist on 2026-03-08
    assert fire_times(s, utc(2026, 3, 7), utc(2026, 3, 10)) == [
        utc(2026, 3, 7, 10, 30),
        utc(2026, 3, 8, 10),
        utc(2026, 3, 9, 9, 30),
    ]


def test_fire_times_in_repeated_fall_back_hour_fires_once():
    s = sched("30 1 * * *", "America/Los_Angeles")  # 01:30 happens twice on 2026-11-01
    assert fire_times(s, utc(2026, 10, 31), utc(2026, 11, 3)) == [
        utc(2026, 10, 31, 8, 30),
        utc(2026, 11, 1, 8, 30),
        utc(2026, 11, 2, 9, 30),
    ]
    # a dispatcher window that only covers the second 01:30 must not fire it again
    assert fire_times(s, utc(2026, 11, 1, 9), utc(2026, 11, 1, 10)) == []


@pytest.mark.parametrize("cron", ["0 * * * *", "0 */1 * * *"])
def test_hourly_schedule_keeps_firing_through_repeated_hour(cron):
    s = sched(cron, "America/Los_Angeles")
    assert fire_times(s, utc(2026, 11, 1, 7), utc(2026, 11, 1, 11)) == [
        utc(2026, 11, 1, 8),  # 01:00 PDT
        utc(2026, 11, 1, 9),  # 01:00 PST
        utc(2026, 11, 1, 10),
        utc(2026, 11, 1, 11),
    ]


FIRST_BUSINESS_DAYS_2026 = ["01-01", "02-02", "03-02", "04-01", "05-01", "06-01", "07-01", "08-03"]
LAST_BUSINESS_DAYS_2026 = ["01-30", "02-27", "03-31", "04-30", "05-29", "06-30", "07-31", "08-31"]


@pytest.mark.parametrize(
    ("which", "expected"), [("first", FIRST_BUSINESS_DAYS_2026), ("last", LAST_BUSINESS_DAYS_2026)]
)
@pytest.mark.parametrize("cron", ["0 9 * * 1-5", "0 9 * * *"])
def test_business_day(which, expected, cron):
    s = sched(cron, business_day=which)
    got = fire_times(s, utc(2025, 12, 31, 12), utc(2026, 8, 31, 23))
    assert [t.strftime("%m-%d") for t in got] == expected
    assert all(t.hour == 9 and t.weekday() < 5 for t in got)


def test_business_day_uses_local_date():
    # 08:00 Tokyo on Monday 2026-06-01 is 23:00 UTC on Sunday 2026-05-31
    s = sched("0 8 * * 1-5", "Asia/Tokyo", "first")
    assert fire_times(s, utc(2026, 5, 20), utc(2026, 6, 20)) == [utc(2026, 5, 31, 23)]


def test_business_day_with_weekend_only_cron_never_fires():
    assert fire_times(sched("0 9 * * 6,0", business_day="first"), utc(2026, 1, 1), utc(2026, 4, 1)) == []


def test_slot_id_format():
    assert slot_id("weekly-report", utc(2026, 3, 9, 16, 5, 59)) == "weekly-report@20260309T1605Z"


# -- due_slots / next_fire_times ----------------------------------------------------------------


def _loaded(repo, *docs):
    for d in docs:
        write(repo, f"{d['name']}.yaml", d)
    return load_automations(repo)


def test_due_slots(repo):
    daily = schedule_doc(
        name="daily", trigger={"type": "schedule", "schedule": {"natural": "9am UTC", "cron": "0 9 * * *"}}
    )
    off = schedule_doc(name="off", enabled=False, trigger=daily["trigger"])
    loaded = _loaded(repo, daily, off, anomaly_doc())

    due = due_slots(loaded, utc(2026, 3, 10, 11))
    assert [(la.automation.name, sid, t) for la, sid, t in due] == [
        ("daily", "daily@20260310T0900Z", utc(2026, 3, 10, 9))
    ]
    assert due_slots(loaded, utc(2026, 3, 10, 9)) != []  # fire time == now is due
    assert due_slots(loaded, utc(2026, 3, 10, 12)) == []  # exactly window after: start is exclusive
    assert due_slots(loaded, utc(2026, 3, 10, 8, 59)) == []
    wide = due_slots(loaded, utc(2026, 3, 10, 11), window=timedelta(days=2))
    assert [sid for _, sid, _ in wide] == ["daily@20260309T0900Z", "daily@20260310T0900Z"]


def test_due_slots_across_dst(repo):
    loaded = _loaded(repo, schedule_doc(name="weekly"))  # Mondays 09:00 America/Los_Angeles
    assert [sid for _, sid, _ in due_slots(loaded, utc(2026, 3, 2, 18))] == ["weekly@20260302T1700Z"]
    assert [sid for _, sid, _ in due_slots(loaded, utc(2026, 3, 9, 17))] == ["weekly@20260309T1600Z"]


def test_next_fire_times():
    s = sched("0 9 * * 1", "America/Los_Angeles")
    assert next_fire_times(s, utc(2026, 3, 1)) == [
        utc(2026, 3, 2, 17),
        utc(2026, 3, 9, 16),
        utc(2026, 3, 16, 16),
    ]
    assert next_fire_times(s, utc(2026, 3, 1), count=1) == [utc(2026, 3, 2, 17)]
    monthly = sched("0 9 * * 1-5", business_day="last")
    got = next_fire_times(monthly, utc(2026, 3, 10), count=4)
    assert [t.date() for t in got] == [
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 29),
        date(2026, 6, 30),
    ]
    assert next_fire_times(sched("0 9 1 1 *"), utc(2026, 3, 10), count=1) == [utc(2027, 1, 1, 9)]
    assert next_fire_times(sched("0 9 29 2 *"), utc(2026, 3, 10)) == []  # next Feb 29 is past the horizon


@pytest.mark.parametrize("cron", ["0 9 30 2 *", "0 9 31 4 *", "0 9 31 11 *"])
def test_cron_that_never_fires_is_rejected(cron):
    with pytest.raises(ValidationError, match="never fires"):
        sched(cron)


def test_leap_day_cron_is_accepted():
    assert fire_times(sched("0 9 29 2 *"), utc(2026, 1, 1), utc(2029, 1, 1)) == [utc(2028, 2, 29, 9)]
