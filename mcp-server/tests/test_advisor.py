"""Advisor cost recommendations: savings per currency, filters, and owner routing."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import yaml
from conftest import make_runtime

from azure_finops.advisor import get_advisor_cost_recommendations, load_owner_rules, route_owner
from azure_finops.simulator.synthetic import DEFAULT_TODAY, DEV, PROD


def write_owners(repo, owners):
    (repo / "finops" / "context" / "owners.yaml").write_text(yaml.safe_dump({"owners": owners}))


def rec_ids(out) -> list[str]:
    return [r["id"].rsplit("/", 1)[-1] for r in out["recommendations"]]


def test_recommendations_sorted_with_annual_savings(rt):
    out = get_advisor_cost_recommendations(rt)
    assert out["status"] == "ok" and out["count"] == 4
    assert rec_ids(out) == ["rec-1", "rec-4", "rec-2", "rec-3"]
    assert [r["annual_savings"] for r in out["recommendations"]] == [15000.0, 2520.0, 1680.0, 420.0]
    assert out["annual_savings_by_currency"] == {"USD": 19620.0}
    first = out["recommendations"][0]
    assert first["subscription"] == "contoso-prod" and first["subscription_id"] == PROD
    assert first["resource_group"] is None  # subscription-level reservation recommendation
    assert first["impact"] == "High" and first["savings_amount_as_reported"] == 1250.0
    assert first["currency"] == "USD" and first["last_updated"] == "2026-03-17T06:00:00Z"
    sql = out["recommendations"][1]
    assert sql["resource"] == "db-orders" and sql["resource_type"] == "Microsoft.Sql/servers/databases"
    assert sql["resource_group"] == "rg-web"
    ev = rt.evidence.get(out["evidence_id"])
    assert ev.scope == [f"/subscriptions/{PROD}", f"/subscriptions/{DEV}"]
    assert ev.currency == ["USD"] and ev.row_count == 4
    assert any("not guaranteed" in n for n in ev.notes) and any("double count" in n for n in ev.notes)
    assert "dataset_id" not in out


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"min_annual_savings": 2000}, ["rec-1", "rec-4"]),
        ({"min_annual_savings": 2520}, ["rec-1", "rec-4"]),
        ({"min_annual_savings": 1e6}, []),
        ({"impact": ["Medium"]}, ["rec-4", "rec-2"]),
        ({"impact": ["Low", "High"]}, ["rec-1", "rec-3"]),
        ({"impact": ["Medium"], "min_annual_savings": 2000}, ["rec-4"]),
        ({"subscriptions": [DEV.upper()]}, ["rec-2"]),
    ],
)
def test_filters(rt, kwargs, expected):
    out = get_advisor_cost_recommendations(rt, **kwargs)
    assert rec_ids(out) == expected
    assert out["count"] == len(expected)
    assert out["annual_savings_by_currency"] == (
        {"USD": round(sum(r["annual_savings"] for r in out["recommendations"]), 2)} if expected else {}
    )


def test_top_n_spills_to_dataset(rt):
    out = get_advisor_cost_recommendations(rt, top_n=2)
    assert out["count"] == 4 and rec_ids(out) == ["rec-1", "rec-4"]
    assert out["annual_savings_by_currency"] == {"USD": 19620.0}  # totals cover everything, not just top_n
    _, rows, _ = rt.datasets.query(f"select count(*), sum(annual_savings) from {out['dataset_id']}")
    assert rows == [[4, 19620.0]]


def test_no_owners_file_leaves_recommendations_unrouted(rt):
    out = get_advisor_cost_recommendations(rt)
    assert all(r["owner"] is None for r in out["recommendations"])
    assert "owners.yaml" in out["owner_routing"]


def test_owner_routing_first_match_wins(rt, repo):
    write_owners(
        repo,
        [
            {"team": "ml-team", "match": {"resource_group": "rg-ml*"}},
            {"team": "dba-team", "match": {"resource_type": "microsoft.sql/*"}},
            {"team": "web-team", "match": {"subscription": PROD.upper(), "resource_group": "rg-web"}},
            {"team": "nobody", "match": {"subscription": DEV, "resource_group": "rg-web"}},
            {"team": "dev-team", "match": {"subscription": DEV}},
            {"team": "storage-team", "match": {"resource_id_contains": "/DISKS/"}},
            {"team": "finops-team", "match": {}},
        ],
    )
    out = get_advisor_cost_recommendations(rt)
    owners = {r["id"].rsplit("/", 1)[-1]: r["owner"] for r in out["recommendations"]}
    assert owners == {
        "rec-1": "finops-team",  # subscription-level rec falls through to the catch-all
        "rec-4": "dba-team",  # SQL rule comes before the rg-web rule
        "rec-2": "dev-team",  # the DEV+rg-web rule needs both keys to match
        "rec-3": "storage-team",
    }
    assert "owner_routing" not in out


def test_owner_routing_glob_and_case(repo):
    rules = [
        {"team": "data", "match": {"resource_group": "RG-DATA"}},
        {"team": "compute", "match": {"resource_type": "Microsoft.Compute/*"}},
        {"team": "q", "match": {"resource_group": "rg-?eb"}},
    ]
    assert (
        route_owner(rules, {"resourceGroup": "rg-data", "impactedField": "Microsoft.Compute/disks"}) == "data"
    )
    assert (
        route_owner(rules, {"resourceGroup": "rg-dev", "impactedField": "microsoft.compute/virtualmachines"})
        == "compute"
    )
    assert route_owner(rules, {"resourceGroup": "rg-web", "impactedField": "Microsoft.Sql/servers"}) == "q"
    assert route_owner(rules, {"resourceGroup": "rg-webx", "impactedField": "Microsoft.Sql/servers"}) is None
    assert route_owner([], {"resourceGroup": "rg"}) is None
    assert route_owner([{"match": {}}], {}) is None  # a rule without a team routes to nobody


def test_load_owner_rules(repo):
    assert load_owner_rules(repo) == []
    (repo / "finops" / "context" / "owners.yaml").write_text("")
    assert load_owner_rules(repo) == []
    write_owners(repo, [{"team": "t", "match": {"subscription": PROD}}])
    assert load_owner_rules(repo) == [{"team": "t", "match": {"subscription": PROD}}]


def test_savings_are_never_summed_across_currencies(repo):
    def row(i, amount, currency):
        return {
            "id": f"/subscriptions/{PROD}/providers/microsoft.advisor/recommendations/x-{i}",
            "subscriptionId": PROD,
            "resourceGroup": "rg",
            "impact": "Medium",
            "impactedField": "Microsoft.Compute/virtualMachines",
            "impactedValue": f"vm{i}",
            "annualSavingsAmount": amount,
            "savingsCurrency": currency,
        }

    data = [
        row(1, 1000, "USD"),
        row(2, 900, "EUR"),
        row(3, 500, "USD"),
        row(4, 50, None),
        row(5, None, "EUR"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": data, "totalRecords": len(data)})

    rt = make_runtime(SimpleNamespace(handle=handler, today=DEFAULT_TODAY))
    out = get_advisor_cost_recommendations(rt)
    assert out["annual_savings_by_currency"] == {"USD": 1500.0, "EUR": 900.0, "unknown": 50.0}
    assert out["evidence"]["currency"] == ["EUR", "USD", "unknown"]
    assert [r["annual_savings"] for r in out["recommendations"]] == [1000.0, 900.0, 500.0, 50.0, 0.0]
    assert out["recommendations"][-1]["currency"] == "EUR"
