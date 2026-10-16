"""Period comparison math, period defaults, and driver analysis on the simulator."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import DEV_SCOPE, PROD_SCOPE, make_runtime

from azure_finops.compare import _periods, _reconcile, compare_periods, compare_rows, explain_drivers
from azure_finops.errors import ValidationFailed
from azure_finops.ratelimit import QpuLimiter
from azure_finops.simulator.synthetic import DEFAULT_TODAY, PROD

GPU_ID = (
    f"/subscriptions/{PROD}/resourcegroups/rg-ml/providers/microsoft.compute/virtualmachines/vm-gpu-train-01"
)


@pytest.fixture
def crt(rt):
    rt.arm.limiter = QpuLimiter(windows=((10.0, 10**9),))
    return rt


def r(svc, cost, currency="USD", **extra):
    return {"ServiceName": svc, "cost": cost, "currency": currency, **extra}


# ---- compare_rows ------------------------------------------------------------------------------

BASE = [r("A", 140), r("B", 28), r("C", 14), r("E", 20)]
CUR = [r("A", 100), r("B", 7), r("D", 25), r("E", 10)]


def by_name(items):
    return {i["ServiceName"]: i for i in items}


def test_compare_rows_normalizes_baseline_and_classifies():
    out = compare_rows(BASE, CUR, ["ServiceName"], base_days=14, cur_days=7)
    usd = out["USD"]
    assert usd["baseline_total"] == 202.0
    assert usd["baseline_total_normalized"] == 101.0
    assert usd["current_total"] == 142.0
    assert usd["delta"] == 41.0
    assert usd["pct_change"] == pytest.approx(40.6)
    items = by_name(usd["items"])
    expected = {
        "A": (140, 70, 100, 30, 42.9, "increased"),
        "B": (28, 14, 7, -7, -50.0, "decreased"),
        "C": (14, 7, 0, -7, -100.0, "removed"),
        "D": (0, 0, 25, 25, None, "new"),
        "E": (20, 10, 10, 0, 0.0, "unchanged"),
    }
    for name, (b, bn, c, delta, pct, status) in expected.items():
        it = items[name]
        assert (it["baseline"], it["baseline_normalized"], it["current"]) == (b, bn, c), name
        assert it["delta"] == delta and it["pct_change"] == pct and it["status"] == status, name


def test_compare_rows_ranks_by_absolute_delta_and_shares_sum_to_100():
    items = compare_rows(BASE, CUR, ["ServiceName"], 14, 7)["USD"]["items"]
    assert [i["ServiceName"] for i in items] == ["A", "D", "B", "C", "E"]
    shares = {i["ServiceName"]: i["share_of_total_change"] for i in items}
    assert shares == {"A": 73.2, "D": 61.0, "B": -17.1, "C": -17.1, "E": 0.0}
    changed = [i["share_of_total_change"] for i in items if i["status"] != "unchanged"]
    assert sum(changed) == pytest.approx(100.0, abs=0.5)


def test_compare_rows_same_length_has_no_scaling():
    items = by_name(compare_rows([r("A", 10)], [r("A", 15)], ["ServiceName"], 7, 7)["USD"]["items"])
    assert items["A"]["baseline_normalized"] == 10.0 and items["A"]["delta"] == 5.0
    assert items["A"]["share_of_total_change"] == 100.0


def test_compare_rows_zero_total_change_has_no_share():
    out = compare_rows([r("A", 10), r("B", 5)], [r("A", 5), r("B", 10)], ["ServiceName"], 7, 7)["USD"]
    assert out["delta"] == 0.0
    assert all(i["share_of_total_change"] is None for i in out["items"])


def test_compare_rows_tiny_delta_is_unchanged():
    items = compare_rows([r("A", 10.001)], [r("A", 10.004)], ["ServiceName"], 7, 7)["USD"]["items"]
    assert items[0]["status"] == "unchanged"


def test_compare_rows_zero_baseline_everywhere():
    out = compare_rows([], [r("A", 5)], ["ServiceName"], 7, 7)["USD"]
    assert out["pct_change"] is None and out["baseline_total"] == 0.0
    assert out["items"][0]["status"] == "new" and out["items"][0]["pct_change"] is None


def test_compare_rows_keeps_currencies_apart_and_aggregates_duplicates():
    base = [r("A", 10), r("A", 5), r("A", 100, "EUR")]
    cur = [r("A", 20), r("A", 90, "EUR")]
    out = compare_rows(base, cur, ["ServiceName"], 7, 7)
    assert set(out) == {"USD", "EUR"}
    assert out["USD"]["items"][0]["baseline"] == 15.0 and out["USD"]["delta"] == 5.0
    assert out["EUR"]["delta"] == -10.0


def test_compare_rows_multiple_dims_and_missing_keys():
    base = [{"ServiceName": "S", "ResourceGroupName": "rg1", "cost": 4.0, "currency": "USD"}]
    cur = [{"ServiceName": "S", "cost": 6.0, "currency": "USD"}]
    items = compare_rows(base, cur, ["ServiceName", "ResourceGroupName"], 7, 7)["USD"]["items"]
    keys = {(i["ServiceName"], i["ResourceGroupName"]): i["status"] for i in items}
    assert keys == {("S", "rg1"): "removed", ("S", ""): "new"}


# ---- _periods and _reconcile -------------------------------------------------------------------


def test_default_baseline_is_preceding_period_of_same_length():
    cs, ce, bs, be = _periods("2026-03-09", "2026-03-15", None, None)
    assert (bs.isoformat(), be.isoformat()) == ("2026-03-02", "2026-03-08")
    _, _, bs, be = _periods("2026-03-01", "2026-03-31", None, None)
    assert (bs.isoformat(), be.isoformat()) == ("2026-01-29", "2026-02-28")
    _, _, bs, be = _periods("2026-03-10", "2026-03-10", None, None)
    assert bs == be and be.isoformat() == "2026-03-09"


def test_explicit_baselines_before_or_after_are_allowed():
    assert _periods("2026-03-09", "2026-03-15", "2026-02-01", "2026-03-08")[2].isoformat() == "2026-02-01"
    assert _periods("2026-03-09", "2026-03-15", "2026-03-16", "2026-03-20")[3].isoformat() == "2026-03-20"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("2026-03-15", "2026-03-09", None, None), "current_end is before"),
        (("2026-03-09", "2026-03-15", "2026-03-01", None), "both baseline_start and baseline_end"),
        (("2026-03-09", "2026-03-15", None, "2026-03-08"), "both baseline_start and baseline_end"),
        (("2026-03-09", "2026-03-15", "2026-03-08", "2026-03-01"), "baseline_end is before"),
        (("2026-03-09", "2026-03-15", "2026-03-05", "2026-03-09"), "overlap"),
        (("2026-03-09", "2026-03-15", "2026-03-15", "2026-03-20"), "overlap"),
        (("2026-03-09", "2026-03-15", "2026-03-10", "2026-03-12"), "overlap"),
        (("2026-03-09", "2026-03-15", "2026-03-01", "2026-03-31"), "overlap"),
        (("2026-03-09", "2026-03-15", "2026-03-09", "2026-03-15"), "overlap"),
    ],
)
def test_periods_rejects(args, message):
    with pytest.raises(ValidationFailed, match=message):
        _periods(*args)


def test_reconcile_tolerance():
    assert _reconcile([r("A", 100)], [{"cost": 100.4, "currency": "USD"}]) == []  # within 1 unit
    assert _reconcile([r("A", 1000)], [{"cost": 1004, "currency": "USD"}]) == []  # within 0.5%
    gap = _reconcile([r("A", 1000)], [{"cost": 1006, "currency": "USD"}])
    assert len(gap) == 1 and "Reconciliation gap in USD" in gap[0] and "1,006.00" in gap[0]
    assert "EUR" in _reconcile([], [{"cost": 50, "currency": "EUR"}])[0]


# ---- compare_periods on the simulator ----------------------------------------------------------


def test_compare_periods_ranks_virtual_machines_first(crt):
    out = compare_periods(crt, PROD_SCOPE, "2026-03-09", "2026-03-15")
    assert out["status"] == "ok" and out["basis"] == "ActualCost" and out["group_by"] == ["ServiceName"]
    assert out["current_period"] == {"from": "2026-03-09", "to": "2026-03-15", "days": 7}
    assert out["baseline_period"] == {"from": "2026-03-02", "to": "2026-03-08", "days": 7}
    assert out["notes"] == []  # grouped rows reconcile with the ungrouped totals
    assert list(out["by_currency"]) == ["USD"]
    usd = out["by_currency"]["USD"]
    assert usd["baseline_total"] == pytest.approx(3632.31, abs=0.01)
    assert usd["current_total"] == pytest.approx(8593.73, abs=0.01)
    assert usd["delta"] == pytest.approx(usd["current_total"] - usd["baseline_total"], abs=0.02)
    top = usd["top_changes"]
    assert top[0]["ServiceName"] == "Virtual Machines" and top[0]["status"] == "increased"
    assert top[0]["delta"] == pytest.approx(4718.6, abs=0.01)
    assert top[0]["share_of_total_change"] == pytest.approx(95.1)
    assert top[1]["ServiceName"] == "Log Analytics" and top[1]["delta"] == pytest.approx(242.26, abs=0.01)
    assert usd["groups_compared"] == len(top) == 6 and "dataset_id" not in usd
    assert sum(t["share_of_total_change"] for t in top) == pytest.approx(100, abs=0.5)
    assert len(set(out["evidence_ids"])) == 4
    assert all(crt.evidence.get(e).tool == "compare_periods" for e in out["evidence_ids"])


def test_compare_periods_scales_uneven_baseline(crt):
    out = compare_periods(crt, DEV_SCOPE, "2026-03-09", "2026-03-15", "2026-02-23", "2026-03-08")
    usd = out["by_currency"]["USD"]
    assert out["baseline_period"]["days"] == 14
    assert usd["baseline_total_normalized"] == pytest.approx(usd["baseline_total"] / 2, abs=0.01)
    assert any("scaled by 7/14" in n for n in out["notes"])
    assert abs(usd["pct_change"]) < 10  # DEV is stable


def test_compare_periods_clamps_current_to_yesterday(crt):
    out = compare_periods(crt, DEV_SCOPE, "2026-03-12", "2026-03-18")
    assert out["current_period"] == {"from": "2026-03-12", "to": "2026-03-17", "days": 6}
    assert out["baseline_period"] == {"from": "2026-03-05", "to": "2026-03-11", "days": 7}
    assert any("scaled by 6/7" in n for n in out["notes"])
    assert any("incomplete" in n for n in out["notes"])


def test_compare_periods_spills_large_results_to_dataset(crt):
    out = compare_periods(crt, PROD_SCOPE, "2026-03-09", "2026-03-15", group_by=["ResourceId"], top_n=3)
    usd = out["by_currency"]["USD"]
    assert len(usd["top_changes"]) == 3 and usd["groups_compared"] > 3
    assert usd["top_changes"][0]["ResourceId"] == GPU_ID
    _, rows, _ = crt.datasets.query(f"select count(*) from {usd['dataset_id']}")
    assert rows == [[usd["groups_compared"]]]


def test_compare_periods_multi_currency():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cur = body["timePeriod"]["from"].startswith("2026-03-09")
        grouped = bool(body["dataset"].get("grouping"))
        rows = [[120.0 if cur else 100.0, "VM", "USD"], [80.0 if cur else 100.0, "VM", "EUR"]]
        cols = [{"name": "Cost", "type": "Number"}]
        if grouped:
            cols.append({"name": "ServiceName", "type": "String"})
        else:
            rows = [[x[0], x[2]] for x in rows]
        cols.append({"name": "Currency", "type": "String"})
        return httpx.Response(200, json={"properties": {"columns": cols, "rows": rows}})

    rt = make_runtime(SimpleNamespace(handle=handler, today=DEFAULT_TODAY))
    out = compare_periods(rt, PROD_SCOPE, "2026-03-09", "2026-03-15")
    assert out["by_currency"]["USD"]["delta"] == 20.0
    assert out["by_currency"]["EUR"]["delta"] == -20.0
    assert any("never summed" in n for n in out["notes"])
    assert not any("Reconciliation gap" in n for n in out["notes"])


def test_compare_periods_reports_reconciliation_gap():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["dataset"].get("grouping"):
            cols = [{"name": "Cost", "type": "Number"}, {"name": "ServiceName", "type": "String"}]
            return httpx.Response(200, json={"properties": {"columns": cols, "rows": [[90.0, "VM"]]}})
        return httpx.Response(
            200, json={"properties": {"columns": [{"name": "Cost", "type": "Number"}], "rows": [[100.0]]}}
        )

    rt = make_runtime(SimpleNamespace(handle=handler, today=DEFAULT_TODAY))
    out = compare_periods(rt, PROD_SCOPE, "2026-03-09", "2026-03-15")
    gaps = [n for n in out["notes"] if "Reconciliation gap" in n]
    assert len(gaps) == 2  # current and baseline


# ---- explain_drivers ---------------------------------------------------------------------------


def test_explain_drivers_surfaces_gpu_vm(crt):
    out = explain_drivers(crt, PROD_SCOPE, "2026-03-09", "2026-03-15")
    assert out["baseline_period"] == {"from": "2026-03-02", "to": "2026-03-08"}
    res = out["by_resource"]["USD"]
    top = res["top"][0]
    assert top["ResourceId"] == GPU_ID and top["ServiceName"] == "Virtual Machines"
    assert top["status"] == "new" and top["baseline"] == 0.0
    assert top["current"] == pytest.approx(4711, abs=5)
    assert res["delta"] == pytest.approx(4961, abs=5)
    meter = out["by_meter"]["USD"]["top"][0]
    assert meter["Meter"] == "NC6s v3" and meter["status"] == "new"
    assert len(out["evidence_ids"]) == 4 and out["service_filter"] == []
    assert all(crt.evidence.get(e).tool == "explain_drivers" for e in out["evidence_ids"])


def test_explain_drivers_service_filter(crt):
    out = explain_drivers(
        crt, PROD_SCOPE, "2026-03-09", "2026-03-15", service_names=["Log Analytics"], top_n=5
    )
    top = out["by_resource"]["USD"]["top"]
    assert len(top) == 1 and top[0]["ResourceId"].endswith("/workspaces/log-contoso")
    assert top[0]["status"] == "increased" and top[0]["delta"] == pytest.approx(242.26, abs=0.01)
    assert out["service_filter"] == ["Log Analytics"]
    ev = crt.evidence.get(out["evidence_ids"][0])
    assert ev.query["body"]["dataset"]["filter"]["dimensions"]["values"] == ["Log Analytics"]
