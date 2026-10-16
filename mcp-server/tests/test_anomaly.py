"""Anomaly detection: per-series scoring, episodes, fingerprints, and the simulator scenario."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import make_runtime

from azure_finops.anomaly import (
    detect_from_rows,
    detect_series,
    detection_window,
    fingerprint,
    run_detection,
    score_day,
)
from azure_finops.config import AnomalySettings
from azure_finops.errors import FinOpsError
from azure_finops.simulator.synthetic import DEFAULT_TODAY, DEV, PROD, SyntheticAzure

S = AnomalySettings()
START = date(2026, 1, 5)  # a Monday
N = 60
END = START + timedelta(days=N - 1)


def day(i: int) -> date:
    return START + timedelta(days=i)


def series(fn) -> dict[date, float]:
    return {day(i): fn(i) for i in range(N)}


def noise(i: int) -> float:
    return ((i * 7) % 5 - 2) * 1.5  # deterministic, within +-3


# ---- detect_series -----------------------------------------------------------------------------


def test_flat_series_with_noise_has_no_anomalies():
    assert detect_series(series(lambda i: 100 + noise(i)), START, END, S) == []


def test_step_change_is_one_spike_episode():
    eps = detect_series(series(lambda i: 100.0 if i < 50 else 300.0), START, END, S)
    assert len(eps) == 1
    ep = eps[0]
    assert ep["kind"] == "spike"
    assert (ep["start"], ep["end"], ep["days"]) == (day(50), day(59), 10)
    assert ep["expected_daily"] == 100.0 and ep["actual_daily"] == 300.0
    assert ep["daily_impact"] == 200.0 and ep["total_impact"] == 2000.0
    assert ep["pct_change"] == 200.0
    assert ep["z_score"] >= S.z_threshold


def test_zero_baseline_is_new_spend():
    eps = detect_series({day(i): 500.0 for i in range(52, N)}, START, END, S)  # missing days count as zero
    assert len(eps) == 1
    ep = eps[0]
    assert ep["kind"] == "new_spend" and ep["start"] == day(52) and ep["days"] == 8
    assert ep["pct_change"] is None and ep["z_score"] is None
    assert ep["expected_daily"] == 0.0 and ep["daily_impact"] == 500.0


def test_near_zero_baseline_counts_as_new_spend():
    eps = detect_series(series(lambda i: 0.5 if i < 55 else 80.0), START, END, S)
    assert [e["kind"] for e in eps] == ["new_spend"]


def test_below_min_impact_is_ignored():
    assert detect_series(series(lambda i: 100.0 if i < 50 else 140.0), START, END, S) == []  # +40/day
    loose = S.model_copy(update={"min_impact": 30.0})
    assert len(detect_series(series(lambda i: 100.0 if i < 50 else 140.0), START, END, loose)) == 1


def test_below_min_percent_is_ignored():
    assert detect_series(series(lambda i: 1000.0 if i < 50 else 1200.0), START, END, S) == []  # +20%
    loose = S.model_copy(update={"min_percent": 15.0})
    assert len(detect_series(series(lambda i: 1000.0 if i < 50 else 1200.0), START, END, loose)) == 1


def test_z_threshold_is_enforced():
    strict = S.model_copy(update={"z_threshold": 100.0})
    assert detect_series(series(lambda i: 100.0 if i < 50 else 300.0), START, END, strict) == []


def test_noisy_baseline_raises_sigma():
    # +-60 swings make MAD large, so a +70 day is not significant
    noisy = series(lambda i: 200.0 + (60 if i % 2 else -60))
    noisy[day(55)] = 330.0
    assert detect_series(noisy, START, END, S) == []


def test_weekday_seasonality_is_not_flagged():
    # Saturday batch jobs: 500 on Saturdays, 100 otherwise. Same-weekday medians absorb this.
    weekly = series(lambda i: 500.0 if day(i).weekday() == 5 else 100.0)
    assert detect_series(weekly, START, END, S) == []
    weekend_off = series(lambda i: 20.0 if day(i).weekday() >= 5 else 200.0)
    assert detect_series(weekend_off, START, END, S) == []


def test_weekday_seasonality_still_catches_real_spike():
    weekly = series(lambda i: 500.0 if day(i).weekday() == 5 else 100.0)
    tuesday = next(i for i in range(50, N) if day(i).weekday() == 1)
    weekly[day(tuesday)] = 400.0
    eps = detect_series(weekly, START, END, S)
    assert [(e["start"], e["days"], e["kind"]) for e in eps] == [(day(tuesday), 1, "spike")]


def test_separate_spikes_form_separate_episodes():
    s = series(lambda i: 100.0)
    for i in (40, 41, 50, 51, 52):
        s[day(i)] = 400.0
    eps = detect_series(s, START, END, S)
    assert [(e["start"], e["days"]) for e in eps] == [(day(40), 2), (day(50), 3)]


def test_first_baseline_days_are_not_scored():
    s = series(lambda i: 100.0)
    s[day(10)] = 10_000.0  # inside the first baseline window
    assert detect_series(s, START, END, S) == []


def test_decreases_are_not_reported():
    assert detect_series(series(lambda i: 300.0 if i < 50 else 10.0), START, END, S) == []


def test_score_day_uses_same_weekday_median_when_available():
    hist = [(day(i), 500.0 if day(i).weekday() == 5 else 100.0) for i in range(28)]
    sat = next(day(i) for i in range(28, 35) if day(i).weekday() == 5)
    expected, sigma, z = score_day(hist, sat, 500.0)
    assert expected == 500.0 and z == 0.0
    expected, sigma, _ = score_day(hist[:10], sat, 500.0)  # < 3 Saturdays -> overall median
    assert expected == 100.0 and sigma == 5.0  # MAD 0 -> 5% of expected


# ---- fingerprints and windows ------------------------------------------------------------------


def test_fingerprint_is_stable_and_normalized():
    dims = {"ResourceGroupName": "rg-ml", "ServiceName": "Virtual Machines"}
    fp = fingerprint(PROD, dims, "2026-03-12")
    assert fp == "81ef8927d747"  # pinned: changing it would re-open every tracked anomaly
    assert (
        fingerprint(
            PROD.upper(), {"ServiceName": "VIRTUAL MACHINES", "ResourceGroupName": "RG-ML"}, "2026-03-12"
        )
        == fp
    )
    assert fingerprint(PROD, dims, "2026-03-13") != fp
    assert fingerprint(DEV, dims, "2026-03-12") != fp
    assert fingerprint(PROD, {**dims, "ServiceName": "Storage"}, "2026-03-12") != fp
    assert len(fp) == 12


def test_continuing_spike_keeps_its_fingerprint():
    def rows(last: int):
        return [
            {
                "date": day(i).isoformat(),
                "ServiceName": "VM",
                "cost": 100.0 if i < 50 else 300.0,
                "currency": "USD",
            }
            for i in range(last + 1)
        ]

    a = detect_from_rows(rows(55), PROD, "prod", ["ServiceName"], START, day(55), S)
    b = detect_from_rows(rows(58), PROD, "prod", ["ServiceName"], START, day(58), S)
    assert len(a) == len(b) == 1
    assert a[0].fingerprint == b[0].fingerprint
    assert (a[0].days, b[0].days) == (6, 9)


def test_detect_from_rows_skips_old_episodes_and_splits_currencies():
    rows = []
    for i in range(N):
        rows.append(
            {
                "date": day(i).isoformat(),
                "ServiceName": "A",
                "cost": 400.0 if i in (35, 36) else 100.0,
                "currency": "USD",
            }
        )
        rows.append(
            {
                "date": day(i).isoformat(),
                "ServiceName": "A",
                "cost": 0.0 if i < 57 else 90.0,
                "currency": "EUR",
            }
        )
    rows.append({"ServiceName": "A", "cost": 1e6, "currency": "USD"})  # undated rows are ignored
    found = detect_from_rows(rows, PROD, "prod", ["ServiceName"], START, END, S, evidence_id="ev-009")
    assert [(a.currency, a.kind, a.start_date) for a in found] == [("EUR", "new_spend", day(57).isoformat())]
    assert found[0].evidence_id == "ev-009" and found[0].dimensions == {"ServiceName": "A"}
    everything = detect_from_rows(rows, PROD, "prod", ["ServiceName"], START, END, S, recent_days=N)
    assert {(a.currency, a.start_date) for a in everything} == {
        ("EUR", day(57).isoformat()),
        ("USD", day(35).isoformat()),
    }
    assert everything[0].total_impact >= everything[1].total_impact


def test_detection_window():
    assert detection_window(DEFAULT_TODAY, S) == (date(2026, 1, 15), date(2026, 3, 15))
    s2 = S.model_copy(update={"skip_recent_days": 0, "lookback_days": 30})
    assert detection_window(DEFAULT_TODAY, s2) == (date(2026, 2, 16), date(2026, 3, 17))


# ---- run_detection on the simulator ------------------------------------------------------------


def test_run_detection_finds_the_scenario_anomalies(rt):
    out = run_detection(rt)
    assert out["status"] == "ok" and out["errors"] == []
    assert out["window"] == {"from": "2026-01-15", "to": "2026-03-15"}
    assert len(out["evidence_ids"]) == 2
    found = out["anomalies"]
    assert len(found) == 2
    assert all(a["subscription_id"] == PROD for a in found)

    gpu, logs = found
    assert gpu["dimensions"] == {"ResourceGroupName": "rg-ml", "ServiceName": "Virtual Machines"}
    assert gpu["kind"] == "new_spend"
    assert (gpu["start_date"], gpu["end_date"], gpu["days"]) == ("2026-03-12", "2026-03-15", 4)
    assert gpu["daily_impact"] == pytest.approx(1174.31, abs=0.01)
    assert gpu["fingerprint"] == "81ef8927d747"
    assert gpu["pct_change"] is None and gpu["z_score"] is None
    assert gpu["subscription_name"] == "contoso-prod" and gpu["currency"] == "USD"
    assert (
        gpu["title"] == "New spend: rg-ml, Virtual Machines in contoso-prod up 1,174 USD/day since 2026-03-12"
    )
    assert gpu["evidence_id"] == out["evidence_ids"][0]

    assert logs["dimensions"] == {"ResourceGroupName": "rg-shared", "ServiceName": "Log Analytics"}
    assert logs["kind"] == "spike"
    assert (logs["start_date"], logs["end_date"], logs["days"]) == ("2026-03-14", "2026-03-15", 2)
    assert logs["daily_impact"] == pytest.approx(119.3, abs=0.01)
    assert logs["fingerprint"] == "55603b5b1c53"
    assert logs["pct_change"] > 200 and logs["z_score"] >= 3.5
    assert logs["title"].startswith("Cost spike: rg-shared, Log Analytics in contoso-prod")

    ev = rt.evidence.get(out["evidence_ids"][0])
    assert ev.tool == "detect_anomalies" and ev.granularity == "Daily"
    assert ev.period_from == "2026-01-15" and ev.period_to == "2026-03-15"
    assert any("2 days are excluded" in n for n in out["notes"])
    assert out["settings"]["group_by"] == ["ResourceGroupName", "ServiceName"]


def test_run_detection_single_subscription(rt):
    out = run_detection(rt, subscriptions=[DEV.upper()])
    assert out["status"] == "ok" and out["anomalies"] == [] and len(out["evidence_ids"]) == 1
    assert rt.evidence.get(out["evidence_ids"][0]).scope == [f"/subscriptions/{DEV}"]


def test_run_detection_custom_settings(rt):
    out = run_detection(rt, settings=S.model_copy(update={"min_impact": 500.0}))
    assert [a["dimensions"]["ServiceName"] for a in out["anomalies"]] == ["Virtual Machines"]
    by_service = run_detection(rt, settings=S.model_copy(update={"group_by": ["ServiceName"]}))
    assert {(a["dimensions"]["ServiceName"], a["kind"]) for a in by_service["anomalies"]} == {
        ("Virtual Machines", "spike"),
        ("Log Analytics", "spike"),
    }


def test_run_detection_rejects_lookback_not_exceeding_baseline(rt):
    with pytest.raises(FinOpsError, match="lookback_days must exceed baseline_days"):
        run_detection(rt, settings=AnomalySettings(lookback_days=28, baseline_days=28))


def test_run_detection_partial_when_one_subscription_is_denied(repo):
    sim = SyntheticAzure(today=DEFAULT_TODAY, deny={("cost", PROD)})
    rt = make_runtime(sim)
    out = run_detection(rt)
    assert out["status"] == "partial"
    assert len(out["errors"]) == 1
    err = out["errors"][0]
    assert err["subscription_id"] == PROD and err["status"] == "unavailable"
    assert err["missing"] == f"Cost Management Reader (or Reader) on /subscriptions/{PROD}"
    assert "403" in err["error"] and "Cost Management Reader" in err["remediation"]
    assert len(out["evidence_ids"]) == 1
    assert rt.evidence.get(out["evidence_ids"][0]).scope == [f"/subscriptions/{DEV}"]
    assert out["anomalies"] == []
    assert any(DEV in path for _, path in sim.calls)


def test_run_detection_unavailable_when_all_denied(repo):
    rt = make_runtime(SyntheticAzure(today=DEFAULT_TODAY, deny={("cost", PROD), ("cost", DEV)}))
    out = run_detection(rt)
    assert out["status"] == "unavailable"
    assert {e["subscription_id"] for e in out["errors"]} == {PROD, DEV}
    assert out["evidence_ids"] == [] and out["anomalies"] == []
