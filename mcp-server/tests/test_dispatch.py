import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fake_github import FakeGitHub

from azure_finops.anomaly import run_detection
from azure_finops.automations import AutomationError, load_automations
from azure_finops.config import AnomalySettings
from azure_finops.dispatch import RUN_WORKFLOW, anomaly_dispatch, dispatch_schedules, matches
from azure_finops.post import fp_marker
from azure_finops.simulator.synthetic import DEV, PROD, synthetic_config
from azure_finops.task import TaskInputs, prepare

CONFIG = synthetic_config()
REF = "refs/heads/main"
MONDAY_1030 = datetime(2026, 3, 16, 10, 30, tzinfo=UTC)  # 2026-03-16 is a Monday
WEDNESDAY = datetime(2026, 3, 18, 14, 0, tzinfo=UTC)


def write_automation(root: Path, name: str, trigger: dict, **kw) -> None:
    data = {
        "name": name,
        "enabled": kw.pop("enabled", True),
        "trigger": trigger,
        "instructions": "Investigate and report on spend.",
        "destinations": ["finops-team"],
        **kw,
    }
    (root / "finops" / "automations" / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def schedule(cron: str = "0 9 * * 1", tz: str = "UTC") -> dict:
    return {"type": "schedule", "schedule": {"natural": "weekly", "cron": cron, "timezone": tz}}


def anomaly_trigger(**flt) -> dict:
    return {"type": "anomaly", "filter": flt}


def anomaly(fp: str = "0123456789ab", **kw) -> dict:
    return {
        "fingerprint": fp,
        "subscription_id": PROD,
        "subscription_name": "contoso-prod",
        "dimensions": {"ResourceGroupName": "rg-ml", "ServiceName": "Virtual Machines"},
        "currency": "USD",
        "kind": "new_spend",
        "start_date": "2026-03-12",
        "end_date": "2026-03-16",
        "days": 5,
        "expected_daily": 0.0,
        "actual_daily": 1180.0,
        "daily_impact": 1180.0,
        "total_impact": 5900.0,
        "pct_change": None,
        "z_score": None,
        "title": f"New spend {fp}",
        "evidence_id": "ev-001",
    } | kw


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# -- schedules -------------------------------------------------------------------------------------


def test_dispatch_schedules_nothing_due_makes_no_calls(repo):
    write_automation(repo, "weekly", schedule())
    fake = FakeGitHub(now=MONDAY_1030)
    assert (
        dispatch_schedules(CONFIG, repo, fake.client(), REF, now=datetime(2026, 3, 17, 10, tzinfo=UTC)) == []
    )
    assert fake.requests == []


def test_dispatch_schedules_without_automations(repo):
    fake = FakeGitHub()
    assert dispatch_schedules(CONFIG, repo, fake.client(), REF, now=MONDAY_1030) == []
    assert fake.requests == []


def test_dispatch_schedules_dispatches_due_slot_once(repo):
    write_automation(repo, "weekly", schedule())
    fake = FakeGitHub(now=MONDAY_1030)
    out = dispatch_schedules(CONFIG, repo, fake.client(), REF, now=MONDAY_1030)
    assert out == [
        {
            "automation": "weekly",
            "slot": "weekly@20260316T0900Z",
            "fired_at": "2026-03-16T09:00:00Z",
            "action": "dispatched",
        }
    ]
    assert fake.dispatches == [
        {
            "workflow": RUN_WORKFLOW,
            "ref": REF,
            "inputs": {
                "kind": "schedule",
                "automation": "weekly",
                "slot": "weekly@20260316T0900Z",
                "dedupe_key": "weekly@20260316T0900Z",
            },
        }
    ]
    runs_query = next(r for r in fake.requests if r.url.path.endswith("/runs"))
    # Looks back over the window plus an hour of slack.
    assert runs_query.url.params["created"] == ">=2026-03-16T06:30:00Z"

    # The next hourly dispatcher sees the run (its title ends with the slot) and skips it.
    later = MONDAY_1030 + timedelta(hours=1)
    again = dispatch_schedules(CONFIG, repo, fake.client(), REF, now=later)
    assert again[0]["action"] == "skipped" and again[0]["reason"] == "already dispatched"
    assert len(fake.dispatches) == 1


@pytest.mark.parametrize(
    ("title", "seen"),
    [
        ("finops schedule weekly weekly@20260316T0900Z", True),
        ("finops manual weekly weekly@20260316T0900Z", True),
        ("weekly@20260316T0900Z", True),
        ("finops schedule weekly weekly@20260316T0900Z (retry)", False),
        ("finops schedule weekly weekly@20260309T0900Z", False),
        ("finops schedule weekly WEEKLY@20260316T0900Z", False),
        ("", False),
    ],
)
def test_dispatch_schedules_dedupes_on_last_word_of_run_title(repo, title, seen):
    write_automation(repo, "weekly", schedule())
    fake = FakeGitHub(now=MONDAY_1030)
    fake.add_run(title, created_at="2026-03-16T09:05:00Z")
    out = dispatch_schedules(CONFIG, repo, fake.client(), REF, now=MONDAY_1030)
    assert out[0]["action"] == ("skipped" if seen else "dispatched")
    assert len(fake.dispatches) == (0 if seen else 1)


def test_dispatch_schedules_falls_back_to_run_name(repo):
    write_automation(repo, "weekly", schedule())
    fake = FakeGitHub(now=MONDAY_1030)
    run = fake.add_run(
        "", created_at="2026-03-16T09:05:00Z", name="finops schedule weekly weekly@20260316T0900Z"
    )
    run["display_title"] = None
    assert dispatch_schedules(CONFIG, repo, fake.client(), REF, now=MONDAY_1030)[0]["action"] == "skipped"


def test_dispatch_schedules_ignores_runs_before_the_lookback(repo):
    write_automation(repo, "weekly", schedule())
    fake = FakeGitHub(now=MONDAY_1030)
    fake.add_run("finops schedule weekly weekly@20260316T0900Z", created_at="2026-03-16T06:00:00Z")
    assert dispatch_schedules(CONFIG, repo, fake.client(), REF, now=MONDAY_1030)[0]["action"] == "dispatched"


def test_dispatch_schedules_window(repo):
    write_automation(repo, "weekly", schedule())
    fake = FakeGitHub(now=MONDAY_1030)
    now = datetime(2026, 3, 16, 13, 0, tzinfo=UTC)  # 4 hours after the slot fired
    assert dispatch_schedules(CONFIG, repo, fake.client(), REF, now=now) == []
    out = dispatch_schedules(CONFIG, repo, fake.client(), REF, now=now, window=timedelta(hours=5))
    assert [o["slot"] for o in out] == ["weekly@20260316T0900Z"]
    # The window is half-open: a slot firing exactly at now - window is excluded, at now included.
    assert dispatch_schedules(CONFIG, repo, fake.client(), REF, now=now, window=timedelta(hours=4)) == []
    exact = datetime(2026, 3, 16, 9, 0, tzinfo=UTC)
    assert [o["slot"] for o in dispatch_schedules(CONFIG, repo, FakeGitHub().client(), REF, now=exact)] == [
        "weekly@20260316T0900Z"
    ]


def test_dispatch_schedules_multiple_slots_and_timezones(repo):
    write_automation(repo, "hourly", schedule("0 * * * *"))
    write_automation(repo, "pacific", schedule("0 9 * * 1", "America/Los_Angeles"))
    fake = FakeGitHub(now=MONDAY_1030)
    now = datetime(2026, 3, 16, 16, 30, tzinfo=UTC)
    out = dispatch_schedules(CONFIG, repo, fake.client(), REF, now=now)
    assert [o["slot"] for o in out] == [
        "hourly@20260316T1400Z",
        "hourly@20260316T1500Z",
        "hourly@20260316T1600Z",
        "pacific@20260316T1600Z",  # 9 AM PDT
    ]
    assert [d["inputs"]["slot"] for d in fake.dispatches] == [o["slot"] for o in out]


def test_dispatch_schedules_skips_disabled_and_anomaly_automations(repo):
    write_automation(repo, "weekly", schedule(), enabled=False)
    write_automation(repo, "spikes", anomaly_trigger())
    fake = FakeGitHub(now=MONDAY_1030)
    assert dispatch_schedules(CONFIG, repo, fake.client(), REF, now=MONDAY_1030) == []
    assert fake.requests == []


def test_dispatch_schedules_dry_run(repo):
    write_automation(repo, "weekly", schedule())
    fake = FakeGitHub(now=MONDAY_1030)
    out = dispatch_schedules(CONFIG, repo, fake.client(), REF, now=MONDAY_1030, dry_run=True)
    assert out[0]["action"] == "would_dispatch"
    assert fake.dispatches == [] and fake.writes == []


def test_dispatch_schedules_rejects_invalid_automations(repo):
    write_automation(repo, "weekly", schedule(), destinations=["nope"])
    with pytest.raises(AutomationError, match="destination 'nope'"):
        dispatch_schedules(CONFIG, repo, FakeGitHub().client(), REF, now=MONDAY_1030)


# -- matches ---------------------------------------------------------------------------------------


def loaded(repo, name: str):
    return next(la for la in load_automations(repo, CONFIG) if la.automation.name == name)


@pytest.mark.parametrize(
    ("trigger_filter", "kw", "anomaly_kw", "expected"),
    [
        ({}, {}, {}, True),
        ({}, {"enabled": False}, {}, False),
        ({}, {"subscriptions": [DEV]}, {}, False),
        ({}, {"subscriptions": [PROD.upper()]}, {"subscription_id": PROD.upper()}, True),
        ({"subscriptions": [DEV]}, {}, {}, False),
        ({"subscriptions": [PROD]}, {}, {}, True),
        ({"min_impact": 1180}, {}, {}, True),
        ({"min_impact": 1181}, {}, {}, False),
        ({"min_percent": 30}, {}, {"pct_change": 29.9}, False),
        ({"min_percent": 30}, {}, {"pct_change": 30.0}, True),
        ({"min_percent": 30}, {}, {"pct_change": None}, True),
        ({"match": {"servicename": ["virtual machines"]}}, {}, {}, True),
        ({"match": {"ServiceName": ["Storage", "Virtual Machines"]}}, {}, {}, True),
        ({"match": {"ServiceName": ["Storage"]}}, {}, {}, False),
        ({"match": {"MeterCategory": ["Compute"]}}, {}, {}, False),
        ({"match": {"ResourceGroupName": [""]}}, {}, {"dimensions": {"ResourceGroupName": None}}, True),
    ],
)
def test_matches(repo, trigger_filter, kw, anomaly_kw, expected):
    write_automation(repo, "spikes", anomaly_trigger(**trigger_filter), **kw)
    assert matches(loaded(repo, "spikes"), anomaly(**anomaly_kw), CONFIG) is expected


def test_matches_ignores_schedule_automations(repo):
    write_automation(repo, "weekly", schedule())
    assert matches(loaded(repo, "weekly"), anomaly(), CONFIG) is False


# -- anomaly dispatch ------------------------------------------------------------------------------


def test_anomaly_dispatch_starts_investigations(repo):
    write_automation(repo, "spikes", anomaly_trigger(min_impact=100))
    fake = FakeGitHub(now=WEDNESDAY)
    small = anomaly("aaaaaaaaaaaa", daily_impact=50.0, total_impact=100.0)
    medium = anomaly("bbbbbbbbbbbb", total_impact=1000.0)
    large = anomaly("cccccccccccc", total_impact=9000.0)
    out = anomaly_dispatch(
        CONFIG, repo, fake.client(), REF, {"anomalies": [small, medium, large]}, now=WEDNESDAY
    )
    assert out == [
        {"fingerprint": "aaaaaaaaaaaa", "action": "skipped", "reason": "no matching automation"},
        {
            "fingerprint": "cccccccccccc",
            "automation": "spikes",
            "title": "New spend cccccccccccc",
            "action": "dispatched",
        },
        {
            "fingerprint": "bbbbbbbbbbbb",
            "automation": "spikes",
            "title": "New spend bbbbbbbbbbbb",
            "action": "dispatched",
        },
    ]
    first = fake.dispatches[0]
    assert first["workflow"] == RUN_WORKFLOW and first["ref"] == REF
    assert first["inputs"]["kind"] == "anomaly" and first["inputs"]["automation"] == "spikes"
    assert first["inputs"]["dedupe_key"] == "anomaly-cccccccccccc"
    payload = json.loads(first["inputs"]["anomaly"])
    assert "evidence_id" not in payload
    assert payload == {k: v for k, v in large.items() if k != "evidence_id"}

    # Rerunning the dispatcher sees the runs it started.
    again = anomaly_dispatch(CONFIG, repo, fake.client(), REF, {"anomalies": [medium, large]}, now=WEDNESDAY)
    assert [o["reason"] for o in again] == ["investigation already dispatched"] * 2
    assert len(fake.dispatches) == 2


def test_anomaly_dispatch_no_candidates_makes_no_calls(repo):
    write_automation(repo, "spikes", anomaly_trigger(min_impact=10_000))
    fake = FakeGitHub(now=WEDNESDAY)
    out = anomaly_dispatch(CONFIG, repo, fake.client(), REF, {"anomalies": [anomaly()]}, now=WEDNESDAY)
    assert out == [{"fingerprint": "0123456789ab", "action": "skipped", "reason": "no matching automation"}]
    assert fake.requests == []
    assert anomaly_dispatch(CONFIG, repo, fake.client(), REF, {}, now=WEDNESDAY) == []


def test_anomaly_dispatch_picks_first_matching_automation_by_name(repo):
    write_automation(repo, "z-all", anomaly_trigger())
    write_automation(repo, "a-off", anomaly_trigger(), enabled=False)
    write_automation(repo, "m-vms", anomaly_trigger(match={"ServiceName": ["Virtual Machines"]}))
    fake = FakeGitHub(now=WEDNESDAY)
    vm = anomaly("aaaaaaaaaaaa")
    sql = anomaly("bbbbbbbbbbbb", dimensions={"ServiceName": "SQL Database"})
    out = anomaly_dispatch(CONFIG, repo, fake.client(), REF, {"anomalies": [vm, sql]}, now=WEDNESDAY)
    assert [(o["fingerprint"], o["automation"]) for o in out] == [
        ("aaaaaaaaaaaa", "m-vms"),
        ("bbbbbbbbbbbb", "z-all"),
    ]


def test_anomaly_dispatch_skips_when_issue_exists(repo):
    write_automation(repo, "spikes", anomaly_trigger())
    fake = FakeGitHub(now=WEDNESDAY)
    fake.add_issue(
        "old", f"report\n{fp_marker('anomaly-aaaaaaaaaaaa')}", ["finops", "finops-anomaly"], state="closed"
    )
    fake.add_issue("unlabeled", f"x\n{fp_marker('anomaly-bbbbbbbbbbbb')}", ["other"])
    out = anomaly_dispatch(
        CONFIG,
        repo,
        fake.client(),
        REF,
        {"anomalies": [anomaly("aaaaaaaaaaaa"), anomaly("bbbbbbbbbbbb")]},
        now=WEDNESDAY,
    )
    assert [(o["fingerprint"], o["action"], o.get("reason")) for o in out] == [
        ("aaaaaaaaaaaa", "skipped", "issue already exists"),
        ("bbbbbbbbbbbb", "dispatched", None),
    ]
    issues_query = next(r for r in fake.requests if r.url.path.endswith("/issues"))
    assert issues_query.url.params["labels"] == "finops" and issues_query.url.params["state"] == "all"


def test_anomaly_dispatch_dedupe_window_is_three_days(repo):
    write_automation(repo, "spikes", anomaly_trigger())
    fake = FakeGitHub(now=WEDNESDAY)
    fake.add_run("finops anomaly spikes anomaly-aaaaaaaaaaaa", created_at=iso(WEDNESDAY - timedelta(days=2)))
    fake.add_run("finops anomaly spikes anomaly-bbbbbbbbbbbb", created_at=iso(WEDNESDAY - timedelta(days=4)))
    out = anomaly_dispatch(
        CONFIG,
        repo,
        fake.client(),
        REF,
        {"anomalies": [anomaly("aaaaaaaaaaaa"), anomaly("bbbbbbbbbbbb")]},
        now=WEDNESDAY,
    )
    assert [o["action"] for o in out] == ["skipped", "dispatched"]
    assert out[0]["reason"] == "investigation already dispatched"


def test_anomaly_dispatch_daily_budget(repo):
    write_automation(repo, "spikes", anomaly_trigger())
    cfg = CONFIG.model_copy(update={"anomaly": AnomalySettings(max_investigations_per_day=2)})
    fake = FakeGitHub(now=WEDNESDAY)
    fake.add_run("finops anomaly spikes anomaly-111111111111", created_at="2026-03-18T01:00:00Z")
    fake.add_run("finops schedule weekly weekly@20260318T0900Z", created_at="2026-03-18T09:00:00Z")
    fake.add_run("finops anomaly spikes anomaly-222222222222", created_at="2026-03-17T23:59:00Z")
    anomalies = [anomaly(fp, total_impact=t) for fp, t in [("aaaaaaaaaaaa", 3.0), ("bbbbbbbbbbbb", 2.0)]]
    out = anomaly_dispatch(cfg, repo, fake.client(), REF, {"anomalies": anomalies}, now=WEDNESDAY)
    assert [(o["fingerprint"], o["action"], o.get("reason")) for o in out] == [
        ("aaaaaaaaaaaa", "dispatched", None),
        ("bbbbbbbbbbbb", "skipped", "max_investigations_per_day reached"),
    ]
    today_query = [r for r in fake.requests if r.url.path.endswith("/runs")][-1]
    assert today_query.url.params["created"] == ">=2026-03-18T00:00:00Z"

    # Later the same day the budget is used up, including by the run just dispatched.
    more = anomaly_dispatch(
        cfg, repo, fake.client(), REF, {"anomalies": [anomaly("cccccccccccc")]}, now=WEDNESDAY
    )
    assert more[0]["reason"] == "max_investigations_per_day reached"


def test_anomaly_dispatch_dry_run(repo):
    write_automation(repo, "spikes", anomaly_trigger())
    cfg = CONFIG.model_copy(update={"anomaly": AnomalySettings(max_investigations_per_day=1)})
    fake = FakeGitHub(now=WEDNESDAY)
    anomalies = [anomaly("aaaaaaaaaaaa", total_impact=2.0), anomaly("bbbbbbbbbbbb", total_impact=1.0)]
    out = anomaly_dispatch(
        cfg, repo, fake.client(), REF, {"anomalies": anomalies}, now=WEDNESDAY, dry_run=True
    )
    assert [o["action"] for o in out] == ["would_dispatch", "skipped"]
    assert fake.writes == [] and fake.dispatches == []


def test_anomaly_dispatch_payload_round_trips_into_prepare(repo, rt, tmp_path):
    write_automation(repo, "spikes", anomaly_trigger(min_impact=100, subscriptions=[PROD]))
    detection = run_detection(rt)
    assert len(detection["anomalies"]) == 2
    fake = FakeGitHub(now=WEDNESDAY)
    out = anomaly_dispatch(CONFIG, repo, fake.client(), REF, detection, now=WEDNESDAY)
    assert [o["action"] for o in out] == ["dispatched", "dispatched"]
    for i, d in enumerate(fake.dispatches):
        inputs = TaskInputs(**{k: v for k, v in d["inputs"].items() if k != "kind"}, kind="anomaly")
        task = prepare(inputs, tmp_path / f"run-{i}", CONFIG, repo, None, {"GITHUB_RUN_ID": str(i)})
        src = detection["anomalies"][i]
        assert task["anomaly"]["fingerprint"] == src["fingerprint"]
        assert task["anomaly"]["daily_impact"] == src["daily_impact"]
        assert task["dedupe_key"] == f"anomaly-{src['fingerprint']}"
        assert task["subscriptions"] == [PROD]
