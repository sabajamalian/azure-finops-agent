"""list_scopes and get_capabilities."""

from __future__ import annotations

from datetime import timedelta

from .advisor import ADVISOR_KQL
from .costs import CostRequest, run_cost_query
from .errors import FinOpsError, PermissionDenied
from .runtime import Runtime


def list_scopes(rt: Runtime) -> dict:
    configured = rt.config.subscription_ids()
    visible: dict[str, dict] = {}
    notes: list[str] = []
    try:
        for page in rt.arm.get_paged("/subscriptions", {"api-version": "2022-12-01"}):
            for s in page.body.get("value", []):
                visible[s["subscriptionId"].lower()] = s
    except FinOpsError as exc:
        notes.append(f"Could not list subscriptions: {exc.message}")
    rgs: dict[str, list[str]] = {}
    try:
        rows, _ = rt.arm.resource_graph(
            "resourcecontainers | where type == 'microsoft.resources/subscriptions/resourcegroups' "
            "| project subscriptionId, resourceGroup = name, location | order by resourceGroup asc",
            configured,
        )
        for r in rows:
            rgs.setdefault(r["subscriptionId"].lower(), []).append(r["resourceGroup"])
    except FinOpsError as exc:
        notes.append(f"Could not list resource groups: {exc.message}")
    subs = []
    for s in rt.config.azure.subscriptions:
        v = visible.get(s.id)
        subs.append(
            {
                "id": s.id,
                "name": s.name or (v or {}).get("displayName"),
                "scope": f"/subscriptions/{s.id}",
                "visible_to_identity": v is not None,
                "state": (v or {}).get("state"),
                "resource_groups": rgs.get(s.id, []),
            }
        )
    not_configured = [
        {"id": k, "name": v.get("displayName")} for k, v in visible.items() if k not in configured
    ]
    if not_configured:
        notes.append(
            f"{len(not_configured)} other subscription(s) are visible to this identity but not configured; "
            "the agent won't query them."
        )
    return {
        "status": "ok",
        "tenant_id": rt.config.azure.tenant_id,
        "default_cost_basis": rt.config.azure.cost_basis,
        "timezone": rt.config.timezone,
        "subscriptions": subs,
        "visible_but_not_configured": not_configured,
        "notes": notes,
    }


def _probe(fn) -> dict:
    try:
        fn()
        return {"status": "available"}
    except PermissionDenied as exc:
        return {"status": "unavailable", "missing": exc.missing, "remediation": exc.remediation}
    except FinOpsError as exc:
        return {"status": "error", "error": exc.message}


def get_capabilities(rt: Runtime, refresh: bool = False) -> dict:
    if rt.capabilities and not refresh:
        return rt.capabilities
    y = rt.today() - timedelta(days=1)
    per_sub = {}
    for sub in rt.config.subscription_ids():
        scope = f"/subscriptions/{sub}"
        per_sub[sub] = {
            "name": rt.config.subscription_name(sub),
            "cost_data": _probe(
                lambda scope=scope: run_cost_query(
                    rt, CostRequest(scope, y, y), "get_capabilities", note="capability probe"
                )
            ),
            "resource_graph": _probe(
                lambda sub=sub: rt.arm.resource_graph("resourcecontainers | take 1 | project id", [sub])
            ),
            "advisor": _probe(lambda sub=sub: rt.arm.resource_graph(ADVISOR_KQL + "\n| take 1", [sub])),
            "activity_log": _probe(
                lambda scope=scope: rt.arm.get(
                    f"{scope}/providers/Microsoft.Insights/eventtypes/management/values",
                    {
                        "api-version": "2015-04-01",
                        "$filter": f"eventTimestamp ge '{y.isoformat()}T00:00:00Z' and "
                        f"eventTimestamp le '{y.isoformat()}T01:00:00Z'",
                    },
                )
            ),
        }
    unavailable = [
        {"subscription": v["name"], "capability": cap, **res}
        for v in per_sub.values()
        for cap, res in v.items()
        if isinstance(res, dict) and res.get("status") != "available"
    ]
    rt.capabilities = {
        "status": "ok",
        "mode": "synthetic fixture data (not real Azure)" if rt.fixture else "live Azure",
        "read_only": True,
        "subscriptions": per_sub,
        "unavailable": unavailable,
        "tools_not_available_in_this_version": [
            "forecasts",
            "budgets",
            "reservation and savings plan utilization",
            "retail price lookup (use Azure MCP pricing_get if enabled)",
            "management group and billing account scopes",
            "any write or remediation",
        ],
    }
    return rt.capabilities
