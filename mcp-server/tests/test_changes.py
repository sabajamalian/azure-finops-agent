"""Change history (Activity Log + resourcechanges), merging, degradation, and correlation scoring."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
from conftest import DEV_SCOPE, PROD_SCOPE, make_runtime

from azure_finops.changes import (
    CAVEAT,
    _merge,
    _op_weight,
    _resource_match,
    _time_weight,
    correlate_changes,
    get_change_history,
)
from azure_finops.errors import ValidationFailed
from azure_finops.simulator.synthetic import DEFAULT_TODAY, DEV, PROD, SyntheticAzure

GPU_ID = (
    f"/subscriptions/{PROD}/resourcegroups/rg-ml/providers/microsoft.compute/virtualmachines/vm-gpu-train-01"
)
NIC_ID = f"/subscriptions/{PROD}/resourcegroups/rg-ml/providers/microsoft.network/networkinterfaces/vm-gpu-train-01-nic"
ARG_PATH = "/providers/microsoft.resourcegraph/resources"


def history(rt, scope=PROD_SCOPE, start="2026-03-01", end="2026-03-17", **kw):
    return get_change_history(rt, scope, start, end, **kw)


def ops(out) -> list[str]:
    return [c["operation"].split("/", 1)[1] for c in out["changes"]]


# ---- get_change_history ------------------------------------------------------------------------


def test_history_merges_both_sources(rt):
    out = history(rt)
    assert out["status"] == "ok"
    assert out["period"] == {"from": "2026-03-01T00:00:00Z", "to": "2026-03-17T23:59:59Z"}
    assert out["count"] == 4 and len(out["changes"]) == 4
    times = [c["time"] for c in out["changes"]]
    assert times == sorted(times, reverse=True)
    assert ops(out) == [
        "tags/write",
        "diagnosticSettings/write",
        "virtualMachines/write",
        "networkInterfaces/write",
    ]
    tags, diag, gpu, nic = out["changes"]
    assert tags["caller"] == "carol@contoso.com" and tags["source"] == "activity_log+resource_changes"
    assert tags["changed_properties"] == ["tags.costcenter"]
    assert diag["caller"] == "bob@contoso.com" and diag["kind"] == "create"
    assert diag["source"] == "activity_log+resource_changes" and diag["client"] == "Azure CLI"
    assert gpu["resource_id"] == GPU_ID
    assert gpu["caller"] == "alice@contoso.com" and gpu["kind"] == "create" and gpu["change_type"] == "Create"
    assert gpu["client"] == "Azure Portal" and gpu["time"] == "2026-03-12T09:36:00Z"
    assert gpu["source"] == "activity_log+resource_changes"
    assert nic["source"] == "activity_log" and nic["kind"] == "write" and nic["resource_id"] == NIC_ID
    assert all(c["subscription_id"] == PROD for c in out["changes"])


def test_history_drops_started_events_and_evidence_is_recorded(rt):
    out = history(rt)
    assert sum(1 for c in out["changes"] if c["resource_id"] == GPU_ID) == 1  # "Started" event filtered
    assert len(out["evidence_ids"]) == 2
    al, rg = (rt.evidence.get(e) for e in out["evidence_ids"])
    assert al.source == "Activity Log (Administrative, succeeded)" and al.row_count == 4
    assert al.scope == [PROD_SCOPE]
    assert "eventTimestamp ge '2026-03-01T00:00:00Z'" in al.query["filter"]
    assert rg.source == "Azure Resource Graph resourcechanges" and rg.row_count == 3
    assert rg.period_from == "2026-03-04T00:00:00Z"  # 14-day retention
    assert any("14 days" in n for n in out["notes"])


def test_history_includes_older_activity_log_events(rt):
    out = history(rt, start="2026-02-20")
    assert out["count"] == 5
    old = out["changes"][-1]
    assert (
        old["caller"] == "pipeline-sp"
        and old["source"] == "activity_log"
        and old["time"].startswith("2026-02-26")
    )


def test_history_respects_requested_window_in_both_sources(rt):
    out = history(rt, start="2026-03-01", end="2026-03-10")
    assert out["status"] == "ok" and out["count"] == 0 and out["changes"] == []
    assert len(out["evidence_ids"]) == 2


def test_history_resource_group_scope(rt):
    out = history(rt, scope=f"{PROD_SCOPE}/resourceGroups/rg-ml")
    assert ops(out) == ["virtualMachines/write", "networkInterfaces/write"]
    assert {c["resource_group"] for c in out["changes"]} == {"rg-ml"}
    al = rt.evidence.get(out["evidence_ids"][0])
    assert "resourceGroupName eq 'rg-ml'" in al.query["filter"]
    kql = rt.evidence.get(out["evidence_ids"][1]).query["kql"]
    assert "resourceGroup =~ 'rg-ml'" in kql


def test_history_resource_filter_and_overflow_dataset(rt):
    out = history(rt, resource_id_contains="VM-GPU-TRAIN-01")
    assert {c["resource_id"] for c in out["changes"]} == {GPU_ID, NIC_ID}
    big = history(rt, max_items=2)
    assert big["count"] == 4 and len(big["changes"]) == 2
    assert big["changes"][0]["operation"].endswith("tags/write")
    _, rows, _ = rt.datasets.query(f"select count(*) from {big['dataset_id']}")
    assert rows == [[4]]


def test_history_dev_subscription(rt):
    out = history(rt, scope=DEV_SCOPE)
    assert [(c["kind"], c["caller"]) for c in out["changes"]] == [
        ("action", "dave@contoso.com"),
        ("delete", "dave@contoso.com"),
    ]


def test_history_window_is_clamped_to_activity_log_retention(rt):
    out = history(rt, start="2025-01-01")
    assert out["period"]["from"] == "2025-12-19T00:00:00Z"  # today - 89 days


def test_history_older_than_resource_graph_retention_skips_it(repo):
    sim = SyntheticAzure(today=DEFAULT_TODAY)
    rt = make_runtime(sim)
    out = history(rt, start="2026-02-01", end="2026-02-28")
    assert out["count"] == 1 and out["changes"][0]["caller"] == "pipeline-sp"
    assert len(out["evidence_ids"]) == 1
    assert not any(path.lower() == ARG_PATH for _, path in sim.calls)
    assert any("14 days" in n for n in out["notes"])


def test_history_rejects_reversed_dates(rt):
    with pytest.raises(ValidationFailed, match="end_date is before start_date"):
        history(rt, start="2026-03-10", end="2026-03-01")


def test_activity_log_denied_degrades_to_resource_graph(repo):
    rt = make_runtime(SyntheticAzure(today=DEFAULT_TODAY, deny={("activity", PROD)}))
    out = history(rt)
    assert out["status"] == "ok" and len(out["evidence_ids"]) == 1
    assert {c["source"] for c in out["changes"]} == {"resource_changes"}
    assert len(out["changes"]) == 3
    note = next(n for n in out["notes"] if n.startswith("Activity Log unavailable"))
    assert "403" in note and "Fix:" in note and "Reader" in note


def test_resource_graph_denied_degrades_to_activity_log(repo):
    rt = make_runtime(SyntheticAzure(today=DEFAULT_TODAY, deny={("arg", PROD)}))
    out = history(rt)
    assert out["status"] == "ok" and len(out["evidence_ids"]) == 1
    assert {c["source"] for c in out["changes"]} == {"activity_log"}
    assert ops(out) == [
        "tags/write",
        "diagnosticSettings/write",
        "virtualMachines/write",
        "networkInterfaces/write",
    ]
    assert out["changes"][2]["kind"] == "write"  # no resourcechanges to upgrade it to "create"
    assert any(n.startswith("Resource Graph change history unavailable") for n in out["notes"])


def test_both_sources_denied_is_unavailable(repo):
    rt = make_runtime(SyntheticAzure(today=DEFAULT_TODAY, deny={("arg", PROD), ("activity", PROD)}))
    out = history(rt)
    assert out["status"] == "unavailable" and out["changes"] == [] and out["evidence_ids"] == []
    assert len([n for n in out["notes"] if "unavailable" in n]) == 2


def test_activity_log_filters_and_paging(repo):
    sim = SyntheticAzure(today=DEFAULT_TODAY)

    def ev(op, status="Succeeded", category="Administrative", hour=10):
        return {
            "caller": "eve@contoso.com",
            "eventTimestamp": f"2026-03-16T{hour:02d}:00:00Z",
            "operationName": {"value": op, "localizedValue": op},
            "status": {"value": status},
            "category": {"value": category},
            "resourceId": f"/subscriptions/{PROD}/resourceGroups/rg-x/providers/{op.rsplit('/', 2)[0]}/thing-{hour}",
            "resourceGroupName": "rg-x",
            "correlationId": f"c{hour}",
        }

    extra = [
        ev("Microsoft.Compute/virtualMachines/read", hour=1),
        ev("Microsoft.Compute/virtualMachines/write", status="Failed", hour=2),
        ev("Microsoft.Compute/virtualMachines/write", category="Policy", hour=3),
        ev("Microsoft.Compute/virtualMachines/start/action", hour=4),
        ev("Microsoft.Storage/storageAccounts/delete", hour=5),
        ev("Microsoft.Compute/virtualMachines/write", status="Accepted", hour=6),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "eventtypes/management/values" in request.url.path.lower():
            if "skiptoken" in str(request.url):
                return httpx.Response(200, json={"value": extra}, headers={"x-ms-request-id": "page-2"})
            resp = sim.handle(request)
            body = json.loads(resp.content)
            body["nextLink"] = (
                f"https://management.azure.com{request.url.path}?api-version=2015-04-01&$skiptoken=abc"
            )
            return httpx.Response(200, json=body, headers=resp.headers)
        return sim.handle(request)

    rt = make_runtime(SimpleNamespace(handle=handler, today=DEFAULT_TODAY))
    out = history(rt)
    eve = [c for c in out["changes"] if c["caller"] == "eve@contoso.com"]
    assert [(c["operation"].split("/", 1)[1], c["kind"]) for c in eve] == [
        ("storageAccounts/delete", "delete"),
        ("virtualMachines/start/action", "action"),
    ]
    al = rt.evidence.get(out["evidence_ids"][0])
    assert "page-2" in al.request_ids and al.row_count == 6


# ---- correlation -------------------------------------------------------------------------------


def test_correlate_gpu_increase_ranks_alice_first(rt):
    out = correlate_changes(rt, PROD_SCOPE, "2026-03-12", resource_ids=[GPU_ID.upper()])
    assert out["status"] == "ok" and out["caveat"] == CAVEAT
    assert out["window"] == {"from": "2026-03-09T00:00:00Z", "to": "2026-03-13T23:59:59Z"}
    top = out["candidates"][0]
    assert top["caller"] == "alice@contoso.com"
    assert top["operation"] == "Microsoft.Compute/virtualMachines/write"
    assert top["resource_id"] == GPU_ID
    assert top["confidence"] == "high" and top["score"] == pytest.approx(0.7)
    assert (
        top["why"].startswith("same resource;") and "after the start of the first increased day" in top["why"]
    )
    nic = out["candidates"][1]
    assert nic["resource_id"] == NIC_ID and nic["confidence"] == "medium"
    assert nic["why"].startswith("companion resource") and nic["score"] == pytest.approx(0.476)
    # bob's diagnostic setting landed 03-13 16:12, more than a day after the increase began
    assert all("diagnosticsettings" not in c["resource_id"] for c in out["candidates"])
    assert out["candidate_count"] == 2
    assert len(out["evidence_ids"]) == 2


def test_correlate_log_analytics_spike_points_to_diagnostic_setting(rt):
    out = correlate_changes(
        rt, PROD_SCOPE, "2026-03-14", resource_groups=["rg-shared"], service_names=["Log Analytics"]
    )
    top = out["candidates"][0]
    assert top["caller"] == "bob@contoso.com" and "diagnosticsettings" in top["resource_id"]
    assert top["score"] == pytest.approx(0.298, abs=0.001) and top["confidence"] == "medium"
    assert top["why"].startswith("same subscription and service type") and "8h before" in top["why"]
    assert [c["confidence"] for c in out["candidates"][1:]] == ["low", "low"]
    assert all(c["caller"] != "carol@contoso.com" for c in out["candidates"])  # 03-15 11:00 is too late


def test_correlate_days_before_is_clamped(rt):
    wide = correlate_changes(rt, PROD_SCOPE, "2026-03-12", days_before=100)
    assert wide["window"]["from"] == "2026-02-26T00:00:00Z"
    narrow = correlate_changes(rt, PROD_SCOPE, "2026-03-12", days_before=0)
    assert narrow["window"]["from"] == "2026-03-11T00:00:00Z"


def test_correlate_resource_group_scope_adds_rg(rt):
    out = correlate_changes(rt, f"{PROD_SCOPE}/resourceGroups/rg-ml", "2026-03-12", max_items=1)
    assert len(out["candidates"]) == 1 and out["candidate_count"] == 2
    assert out["candidates"][0]["why"].startswith("same resource group")


def test_correlate_unavailable_when_sources_denied(repo):
    rt = make_runtime(SyntheticAzure(today=DEFAULT_TODAY, deny={("arg", DEV), ("activity", DEV)}))
    out = correlate_changes(rt, DEV_SCOPE, "2026-03-12")
    assert out["status"] == "unavailable" and out["candidates"] == [] and out["caveat"] == CAVEAT


# ---- scoring helpers ---------------------------------------------------------------------------


D0 = date(2026, 3, 12)


@pytest.mark.parametrize(
    ("when", "weight"),
    [
        (datetime(2026, 3, 10, 0, 0, tzinfo=UTC), math.exp(-1)),
        (datetime(2026, 3, 11, 12, 0, tzinfo=UTC), math.exp(-0.25)),
        (datetime(2026, 3, 12, 0, 0, tzinfo=UTC), 1.0),
        (datetime(2026, 3, 12, 12, 0, tzinfo=UTC), 0.7),
        (datetime(2026, 3, 13, 0, 0, tzinfo=UTC), 0.7),
        (datetime(2026, 3, 13, 0, 1, tzinfo=UTC), 0.0),
        (datetime(2026, 3, 14, 9, 0, tzinfo=UTC), 0.0),
    ],
)
def test_time_weight(when, weight):
    assert _time_weight(when, D0) == pytest.approx(weight)


@pytest.mark.parametrize(
    ("op", "kind", "weight"),
    [
        ("Microsoft.Resources/tags/write", "write", 0.15),
        ("Microsoft.Authorization/roleAssignments/write", "write", 0.15),
        ("Microsoft.Authorization/locks/write", "write", 0.15),
        ("Microsoft.Compute/virtualMachines/write", "create", 1.0),
        ("Microsoft.Compute/virtualMachines/write", "write", 0.85),
        ("Microsoft.Compute/virtualMachines/start/action", "action", 0.9),
        ("Microsoft.Compute/virtualMachineScaleSets/scale/action", "action", 0.9),
        ("Microsoft.Compute/virtualMachines/deallocate/action", "action", 0.4),
        ("Microsoft.Compute/disks/delete", "delete", 0.3),
    ],
)
def test_op_weight(op, kind, weight):
    assert _op_weight({"operation": op, "kind": kind}) == weight


def test_resource_match_tiers():
    rg = "/subscriptions/x/resourcegroups/rg1/providers"
    vm = f"{rg}/microsoft.compute/virtualmachines/vm1"

    def m(cid, rgs=(), services=(), rids=(vm,)):
        return _resource_match(
            {"resource_id": cid, "resource_group": cid.split("/")[4]}, set(rids), set(rgs), set(services)
        )

    assert m(vm) == (1.0, "same resource")
    assert m(f"{vm}/extensions/ext1") == (1.0, "same resource")
    assert m(f"{rg}/microsoft.network/networkinterfaces/vm1-nic") == (0.8, "companion resource")
    assert m(f"{rg}/microsoft.compute/disks/vm1") == (0.8, "companion resource")
    # vm10 is a different VM, not a companion of vm1
    assert m(f"{rg}/microsoft.compute/virtualmachines/vm10")[1] != "companion resource"
    other = "/subscriptions/x/resourcegroups/rg2/providers/microsoft.compute/disks/d1"
    assert m(other, rgs={"rg2"}, services={"Virtual Machines"}, rids=()) == (
        0.7,
        "same resource group and service type",
    )
    assert m(other, rgs={"rg2"}, rids=()) == (0.5, "same resource group")
    assert m(other, services={"Storage"}, rids=()) == (0.35, "same subscription and service type")
    assert m(other, rids=()) == (0.2, "same subscription")


def test_merge_only_collapses_cross_source_pairs_within_ten_minutes():
    def c(t, source, rid="r1", kind="write"):
        return {"time": t, "source": source, "resource_id": rid, "kind": kind}

    merged = _merge(
        [
            c("2026-03-12T09:00:00Z", "activity_log"),
            c("2026-03-12T09:09:00Z", "resource_changes", kind="create") | {"client": "CLI"},
            c("2026-03-12T10:00:00Z", "resource_changes"),
            c("2026-03-12T10:05:00Z", "resource_changes"),
            c("2026-03-12T09:01:00Z", "activity_log", rid="r2"),
        ]
    )
    assert [(m["resource_id"], m["time"], m["source"]) for m in merged] == [
        ("r1", "2026-03-12T10:05:00Z", "resource_changes"),
        ("r1", "2026-03-12T10:00:00Z", "resource_changes"),
        ("r2", "2026-03-12T09:01:00Z", "activity_log"),
        ("r1", "2026-03-12T09:00:00Z", "activity_log+resource_changes"),
    ]
    assert merged[-1]["kind"] == "create" and merged[-1]["client"] == "CLI"
