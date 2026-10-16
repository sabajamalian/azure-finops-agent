"""Cost Management request building, response parsing, clamping, presentation, and paging."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from conftest import DEV_SCOPE, PROD_SCOPE, make_runtime

from azure_finops.costs import (
    INLINE_ROW_LIMIT,
    CostRequest,
    parse_date,
    parse_rows,
    present_rows,
    query_costs,
    run_cost_query,
    totals_by_currency,
)
from azure_finops.errors import ValidationFailed
from azure_finops.simulator.synthetic import DEFAULT_TODAY, PROD, SyntheticAzure


class CountingLimiter:
    def __init__(self):
        self.calls: list[int] = []

    def acquire(self, qpu: int = 1) -> float:
        self.calls.append(qpu)
        return 0.0


def req(**kw) -> CostRequest:
    base = {"scope": PROD_SCOPE, "start": date(2026, 3, 1), "end": date(2026, 3, 10)}
    base.update(kw)
    return CostRequest(**base)


def cost_page(columns: list[tuple[str, str]], rows: list[list], next_link: str | None = None) -> dict:
    return {
        "properties": {
            "columns": [{"name": n, "type": t} for n, t in columns],
            "rows": rows,
            "nextLink": next_link,
        }
    }


def runtime_with(handler):
    """Runtime whose transport is an arbitrary handler (today pinned to the simulator default)."""
    rt = make_runtime(SimpleNamespace(handle=handler, today=DEFAULT_TODAY))
    rt.arm.limiter = CountingLimiter()
    return rt


# ---- validation and request body ---------------------------------------------------------------


def test_validate_accepts_supported_request(rt):
    req(
        group_by=["ServiceName", "tag:team"], filters={"tag:env": ["prod"], "ResourceGroupName": ["rg"]}
    ).validate(rt)
    req(start=date(2025, 3, 10), end=date(2026, 3, 10)).validate(rt)  # 366 days inclusive


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"scope": "/subscriptions/33333333-3333-3333-3333-333333333333"}, "not configured"),
        ({"start": date(2026, 3, 10), "end": date(2026, 3, 9)}, "before start"),
        ({"start": date(2025, 3, 9), "end": date(2026, 3, 10)}, "at most 366 days"),
        ({"granularity": "Weekly"}, "granularity"),
        ({"group_by": ["ServiceName", "ResourceGroupName", "Meter"]}, "at most 2 group_by"),
        ({"group_by": ["Region"]}, "unsupported dimension 'Region'"),
        ({"filters": {"CostCenter": ["x"]}}, "unsupported dimension 'CostCenter'"),
    ],
)
def test_validate_rejects(rt, kw, message):
    with pytest.raises(ValidationFailed, match=message):
        req(**kw).validate(rt)


def test_validate_rejects_malformed_scope(rt):
    with pytest.raises(ValueError, match="unsupported scope"):
        req(scope=f"/subscriptions/{PROD}/providers/Microsoft.Compute/virtualMachines/x").validate(rt)


def test_scope_path_normalizes():
    assert req(scope=PROD.upper()).scope_path == f"/subscriptions/{PROD}"
    assert req(scope=f"{PROD_SCOPE}/resourceGroups/rg-ml/").scope_path == f"{PROD_SCOPE}/resourceGroups/rg-ml"


def test_body_shape():
    body = req(
        granularity="Daily",
        basis="AmortizedCost",
        group_by=["ServiceName", "tag:team"],
        filters={"ResourceGroupName": ["rg-ml", "rg-web"]},
    ).body()
    assert body == {
        "type": "AmortizedCost",
        "timeframe": "Custom",
        "timePeriod": {"from": "2026-03-01T00:00:00Z", "to": "2026-03-10T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ServiceName"}, {"type": "TagKey", "name": "team"}],
            "filter": {
                "dimensions": {"name": "ResourceGroupName", "operator": "In", "values": ["rg-ml", "rg-web"]}
            },
        },
    }


def test_body_combines_multiple_filters_and_skips_empty():
    ds = req(filters={"ServiceName": ["Storage"], "tag:env": ["prod"], "Meter": []}).body()["dataset"]
    assert "grouping" not in ds
    assert ds["filter"] == {
        "and": [
            {"dimensions": {"name": "ServiceName", "operator": "In", "values": ["Storage"]}},
            {"tags": {"name": "env", "operator": "In", "values": ["prod"]}},
        ]
    }
    assert "filter" not in req(filters={"Meter": []}).body()["dataset"]


def test_parse_date():
    assert parse_date("2026-03-05") == date(2026, 3, 5)
    assert parse_date(" 2026-03-05T10:00:00Z ") == date(2026, 3, 5)
    assert parse_date(date(2026, 1, 2)) == date(2026, 1, 2)
    with pytest.raises(ValidationFailed, match="YYYY-MM-DD"):
        parse_date("03/05/2026")


# ---- parse_rows --------------------------------------------------------------------------------


def test_parse_rows_daily_with_dimension_and_currency():
    page = cost_page(
        [
            ("PreTaxCost", "Number"),
            ("UsageDate", "Number"),
            ("ServiceName", "String"),
            ("Currency", "String"),
        ],
        [[12.5, 20260301, "Storage", "USD"], [None, 20260302, None, "USD"]],
    )
    assert parse_rows([page], ["ServiceName"]) == [
        {"date": "2026-03-01", "ServiceName": "Storage", "cost": 12.5, "currency": "USD"},
        {"date": "2026-03-02", "ServiceName": "", "cost": 0.0, "currency": "USD"},
    ]


def test_parse_rows_billing_currency_and_iso_dates():
    page = cost_page(
        [("costInBillingCurrency", "Number"), ("BillingMonth", "Datetime"), ("BillingCurrency", "String")],
        [[100, "2026-02-01T00:00:00", "EUR"]],
    )
    other = cost_page([("Cost", "Number"), ("Date", "String")], [[1, "2026-02-03T00:00:00Z"]])
    rows = parse_rows([page, other], [])
    assert rows == [
        {"date": "2026-02-01", "cost": 100.0, "currency": "EUR"},
        {"date": "2026-02-03", "cost": 1.0, "currency": "unknown"},
    ]


@pytest.mark.parametrize(
    "name", ["Cost", "PreTaxCost", "totalCost", "CostUSD", "PreTaxCostUSD", "costInBillingCurrency"]
)
def test_parse_rows_known_cost_columns(name):
    page = cost_page(
        [("UsageDate", "Number"), ("Quantity", "Number"), (name, "Number")], [[20260301, 999, 7.25]]
    )
    assert parse_rows([page], [])[0]["cost"] == 7.25


def test_parse_rows_falls_back_to_first_non_date_number_column():
    page = cost_page(
        [("UsageDate", "Number"), ("ResourceGroupName", "String"), ("Amount", "Number"), ("Other", "Number")],
        [[20260301, "rg", 3.5, 99]],
    )
    assert parse_rows([page], ["ResourceGroupName"]) == [
        {"date": "2026-03-01", "ResourceGroupName": "rg", "cost": 3.5, "currency": "unknown"}
    ]


def test_parse_rows_without_cost_column_fails():
    page = cost_page([("UsageDate", "Number"), ("ServiceName", "String")], [[20260301, "x"]])
    with pytest.raises(ValidationFailed, match="cost column not found"):
        parse_rows([page], [])


def test_parse_rows_tag_grouping_and_untagged():
    page = cost_page(
        [("Cost", "Number"), ("TagKey", "String"), ("TagValue", "String"), ("Currency", "String")],
        [[5, "team", "web", "USD"], [2, "team", "", "USD"], [1, "team", None, "USD"]],
    )
    rows = parse_rows([page], ["tag:team"])
    assert [r["tag:team"] for r in rows] == ["web", "(untagged)", "(untagged)"]
    assert "TagKey" not in rows[0]


def test_parse_rows_skips_pages_without_columns_and_ignores_missing_dims():
    good = cost_page([("Cost", "Number")], [[1]])
    rows = parse_rows([{}, {"properties": {}}, good], ["ServiceName"])
    assert rows == [{"cost": 1.0, "currency": "unknown"}]


def test_totals_by_currency_never_mixes():
    rows = [
        {"cost": 1.004, "currency": "USD"},
        {"cost": 2.0, "currency": "USD"},
        {"cost": 5.0, "currency": "EUR"},
    ]
    assert totals_by_currency(rows) == {"USD": 3.0, "EUR": 5.0}


# ---- run_cost_query ----------------------------------------------------------------------------


def test_end_date_is_clamped_to_yesterday_and_flagged_incomplete(rt):
    rows, ev = run_cost_query(rt, req(start=date(2026, 3, 10), end=DEFAULT_TODAY, granularity="Daily"), "t")
    assert ev.period_to == "2026-03-17"
    assert any("End date clamped from 2026-03-18 to 2026-03-17" in n for n in ev.notes)
    assert any("may still be incomplete" in n for n in ev.notes)
    assert ev.partial_period is True
    assert max(r["date"] for r in rows) == "2026-03-17"
    assert ev.data_through == "2026-03-17"
    assert ev.query["body"]["timePeriod"]["to"] == "2026-03-17T23:59:59Z"


def test_period_with_no_completed_days_is_rejected(rt):
    with pytest.raises(ValidationFailed, match="no completed days"):
        run_cost_query(rt, req(start=DEFAULT_TODAY, end=DEFAULT_TODAY), "t")


@pytest.mark.parametrize(
    ("end", "incomplete"),
    [(date(2026, 3, 15), False), (date(2026, 3, 16), True), (date(2026, 3, 17), True)],
)
def test_incomplete_flag_near_today(rt, end, incomplete):
    _, ev = run_cost_query(rt, req(start=date(2026, 3, 1), end=end), "t")
    assert ev.partial_period is incomplete
    assert any("incomplete" in n for n in ev.notes) is incomplete
    assert not any("clamped" in n for n in ev.notes)


def test_monthly_partial_month_flag(rt):
    _, full = run_cost_query(
        rt, req(start=date(2026, 1, 1), end=date(2026, 2, 28), granularity="Monthly"), "t"
    )
    assert full.partial_period is False and full.notes == []
    _, part = run_cost_query(
        rt, req(start=date(2026, 1, 15), end=date(2026, 2, 28), granularity="Monthly"), "t"
    )
    assert part.partial_period is True
    assert any("partial" in n for n in part.notes)
    rows, tail = run_cost_query(
        rt, req(start=date(2026, 2, 1), end=date(2026, 3, 10), granularity="Monthly"), "t"
    )
    assert tail.partial_period is True
    assert sorted({r["date"] for r in rows}) == ["2026-02-01", "2026-03-01"]


def test_daily_data_through_is_last_day_with_cost():
    def handler(request: httpx.Request) -> httpx.Response:
        rows = [[10.0, 20260310, "USD"], [11.0, 20260311, "USD"], [0.0, 20260312, "USD"]]
        return httpx.Response(
            200,
            json=cost_page([("Cost", "Number"), ("UsageDate", "Number"), ("Currency", "String")], rows),
            headers={"x-ms-request-id": "rid-1"},
        )

    rt = runtime_with(handler)
    _, ev = run_cost_query(rt, req(start=date(2026, 3, 10), end=date(2026, 3, 13), granularity="Daily"), "t")
    assert ev.period_to == "2026-03-13"
    assert ev.data_through == "2026-03-11"
    assert ev.request_ids == ["rid-1"]


def test_non_daily_data_through_is_period_end(rt):
    _, ev = run_cost_query(rt, req(start=date(2026, 3, 1), end=date(2026, 3, 10)), "t")
    assert ev.data_through == "2026-03-10"


def test_evidence_records_query_details(rt):
    rows, ev = run_cost_query(
        rt,
        req(start=date(2026, 3, 1), end=date(2026, 3, 10), group_by=["ServiceName"]),
        "query_costs",
        note="hi",
    )
    assert ev.tool == "query_costs" and ev.source == "Cost Management query"
    assert ev.scope == [PROD_SCOPE]
    assert (ev.period_from, ev.period_to, ev.granularity) == ("2026-03-01", "2026-03-10", "None")
    assert ev.cost_basis == "ActualCost" and ev.currency == ["USD"]
    assert ev.row_count == len(rows) > 0
    assert ev.notes == ["hi"]
    assert ev.request_ids and all(r.startswith("sim-") for r in ev.request_ids)
    assert (
        ev.query["scope"] == PROD_SCOPE
        and ev.query["body"]["dataset"]["grouping"][0]["name"] == "ServiceName"
    )
    assert ev.query_hash


@pytest.mark.parametrize(
    ("start", "qpu"), [(date(2026, 3, 1), 1), (date(2026, 1, 1), 3), (date(2025, 3, 18), 12)]
)
def test_qpu_charged_by_range(rt, start, qpu):
    rt.arm.limiter = CountingLimiter()
    run_cost_query(rt, req(start=start, end=date(2026, 3, 17)), "t")
    assert rt.arm.limiter.calls == [qpu]


def test_multi_currency_totals_are_kept_separate():
    def handler(request: httpx.Request) -> httpx.Response:
        rows = [
            [100.0, "Virtual Machines", "USD"],
            [40.0, "Storage", "USD"],
            [70.0, "Virtual Machines", "EUR"],
        ]
        cols = [("Cost", "Number"), ("ServiceName", "String"), ("Currency", "String")]
        return httpx.Response(200, json=cost_page(cols, rows))

    rt = runtime_with(handler)
    out = query_costs(rt, PROD_SCOPE, "2026-03-01", "2026-03-10", group_by=["ServiceName"])
    assert out["totals"] == {"USD": 140.0, "EUR": 70.0}
    assert set(out["totals"]) == {"USD", "EUR"}
    assert out["evidence"]["currency"] == ["EUR", "USD"]
    assert any("never summed across currencies" in n for n in out["evidence"]["notes"])
    assert [r["currency"] for r in out["rows"]] == ["USD", "EUR", "USD"]  # sorted by cost, currencies intact


# ---- presentation and the query_costs tool -----------------------------------------------------


def test_present_rows_inline_when_small(rt):
    rows = [{"ServiceName": f"s{i}", "cost": float(i) + 0.004, "currency": "USD"} for i in range(5)]
    ev = rt.evidence.new(tool="t", source="s", scope=[])
    out = present_rows(rt, rows, ev, "small")
    assert out["row_count"] == 5 and "dataset_id" not in out
    assert [r["cost"] for r in out["rows"]] == [4.0, 3.0, 2.0, 1.0, 0.0]
    assert rows[0]["cost"] == 0.004  # originals untouched
    assert [r["ServiceName"] for r in present_rows(rt, rows, ev, "x", sort_desc=False)["rows"]] == [
        "s0",
        "s1",
        "s2",
        "s3",
        "s4",
    ]
    assert rt.datasets.list() == []


def test_present_rows_spills_to_dataset(rt):
    rows = [
        {"ResourceId": f"r{i}", "cost": float(i), "currency": "USD"} for i in range(INLINE_ROW_LIMIT + 10)
    ]
    ev = rt.evidence.new(tool="t", source="s", scope=[])
    out = present_rows(rt, rows, ev, "big")
    assert out["row_count"] == 60 and len(out["rows"]) == INLINE_ROW_LIMIT
    assert out["rows"][0]["cost"] == 59.0 and out["rows"][-1]["cost"] == 10.0
    assert out["dataset_id"] == "ds_1" and out["dataset_columns"] == ["ResourceId", "cost", "currency"]
    assert "ds_1" in out["note"] and "50 of 60" in out["note"]
    info = rt.datasets.info("ds_1")
    assert info.row_count == 60 and info.evidence_id == ev.id
    assert rt.datasets.query("select min(cost), max(cost) from ds_1")[1] == [[0.0, 59.0]]


def test_query_costs_by_service(rt):
    out = query_costs(rt, PROD_SCOPE, "2026-03-09", "2026-03-15", group_by=["ServiceName"])
    assert out["status"] == "ok" and out["evidence_id"] == out["evidence"]["id"]
    services = [r["ServiceName"] for r in out["rows"]]
    assert services[0] == "Virtual Machines"
    assert set(services) == {
        "Virtual Machines",
        "SQL Database",
        "Azure App Service",
        "Log Analytics",
        "Storage",
        "Bandwidth",
    }
    costs = [r["cost"] for r in out["rows"]]
    assert costs == sorted(costs, reverse=True)
    assert out["totals"]["USD"] == pytest.approx(sum(costs), abs=0.05)
    assert out["totals"]["USD"] == pytest.approx(8593.73, abs=0.01)


def test_query_costs_filters_and_resource_group_scope(rt):
    by_filter = query_costs(
        rt,
        PROD_SCOPE,
        "2026-03-09",
        "2026-03-15",
        group_by=["ResourceGroupName"],
        filters={"ResourceGroupName": ["rg-ml"]},
    )
    assert [r["ResourceGroupName"] for r in by_filter["rows"]] == ["rg-ml"]
    by_scope = query_costs(rt, f"{PROD_SCOPE}/resourceGroups/rg-ml", "2026-03-09", "2026-03-15")
    assert by_scope["evidence"]["scope"] == [f"{PROD_SCOPE}/resourceGroups/rg-ml"]
    assert by_scope["totals"] == by_filter["totals"]


def test_query_costs_tag_grouping(rt):
    out = query_costs(rt, DEV_SCOPE, "2026-03-09", "2026-03-15", group_by=["tag:team"])
    assert {r["tag:team"] for r in out["rows"]} == {"web", "(untagged)"}


def test_query_costs_daily_spills_and_keeps_date_order(rt):
    out = query_costs(
        rt, PROD_SCOPE, "2026-02-01", "2026-03-15", granularity="Daily", group_by=["ServiceName"]
    )
    assert out["row_count"] > INLINE_ROW_LIMIT
    assert out["dataset_id"] == "ds_1"
    dates = [r["date"] for r in out["rows"]]
    assert dates == sorted(dates) and dates[0] == "2026-02-01"
    _, rows, _ = rt.datasets.query("select count(distinct date), min(date), max(date) from ds_1")
    assert rows == [[43, "2026-02-01", "2026-03-15"]]


def test_query_costs_rejects_bad_basis(rt):
    with pytest.raises(ValidationFailed, match="cost_basis"):
        query_costs(rt, PROD_SCOPE, "2026-03-01", "2026-03-10", cost_basis="Usage")


def test_query_costs_uses_configured_default_basis(rt):
    out = query_costs(rt, PROD_SCOPE, "2026-03-01", "2026-03-10")
    assert out["evidence"]["cost_basis"] == "ActualCost"


def test_paging_via_skiptoken_matches_single_page():
    paged_sim = SyntheticAzure(today=DEFAULT_TODAY, page_size=10)
    paged = make_runtime(paged_sim)
    paged.arm.limiter = CountingLimiter()
    whole = make_runtime(SyntheticAzure(today=DEFAULT_TODAY))
    whole.arm.limiter = CountingLimiter()

    args = (PROD_SCOPE, "2026-02-15", "2026-03-15")
    kw = {"granularity": "Daily", "group_by": ["ServiceName"]}
    a = query_costs(paged, *args, **kw)
    b = query_costs(whole, *args, **kw)
    n = a["row_count"]
    assert n == b["row_count"] > 100
    pages = -(-n // 10)
    cost_calls = [c for c in paged_sim.calls if c[1].lower().endswith("/microsoft.costmanagement/query")]
    assert len(cost_calls) == pages
    assert paged.arm.limiter.calls == [1] * pages  # every page is charged
    assert len(a["evidence"]["request_ids"]) == pages
    assert a["totals"] == b["totals"]
    qa = paged.datasets.query("select date, ServiceName, cost from ds_1 order by date, ServiceName")[1]
    qb = whole.datasets.query("select date, ServiceName, cost from ds_1 order by date, ServiceName")[1]
    assert qa == qb
