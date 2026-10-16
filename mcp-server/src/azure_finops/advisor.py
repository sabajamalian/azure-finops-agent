"""Azure Advisor cost recommendations via Azure Resource Graph, with owner routing from context."""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from pathlib import Path

import yaml

from .runtime import Runtime

ADVISOR_KQL = """advisorresources
| where type == 'microsoft.advisor/recommendations'
| where tostring(properties.category) == 'Cost'
| extend resourceId = tostring(properties.resourceMetadata.resourceId),
         impact = tostring(properties.impact),
         impactedField = tostring(properties.impactedField),
         impactedValue = tostring(properties.impactedValue),
         problem = tostring(properties.shortDescription.problem),
         solution = tostring(properties.shortDescription.solution),
         savingsAmount = todouble(properties.extendedProperties.savingsAmount),
         annualSavingsAmount = todouble(properties.extendedProperties.annualSavingsAmount),
         savingsCurrency = tostring(properties.extendedProperties.savingsCurrency),
         recommendationTypeId = tostring(properties.recommendationTypeId),
         lastUpdated = tostring(properties.lastUpdated)
| project id, subscriptionId, resourceGroup, resourceId, impact, impactedField, impactedValue, problem, solution,
          savingsAmount, annualSavingsAmount, savingsCurrency, recommendationTypeId, lastUpdated"""

_IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def load_owner_rules(root: Path) -> list[dict]:
    p = root / "finops" / "context" / "owners.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return list(data.get("owners") or [])


def route_owner(rules: list[dict], rec: dict) -> str | None:
    """First matching rule wins. Match keys: subscription, resource_group (glob), resource_type (glob), tag."""
    rid = (rec.get("resourceId") or "").lower()
    for rule in rules:
        m = rule.get("match") or {}
        if (
            "subscription" in m
            and str(m["subscription"]).lower() != (rec.get("subscriptionId") or "").lower()
        ):
            continue
        if "resource_group" in m and not fnmatch.fnmatch(
            (rec.get("resourceGroup") or "").lower(), str(m["resource_group"]).lower()
        ):
            continue
        if "resource_type" in m and not fnmatch.fnmatch(
            (rec.get("impactedField") or "").lower(), str(m["resource_type"]).lower()
        ):
            continue
        if "resource_id_contains" in m and str(m["resource_id_contains"]).lower() not in rid:
            continue
        return rule.get("team")
    return None


def get_advisor_cost_recommendations(
    rt: Runtime,
    subscriptions: list[str] | None = None,
    min_annual_savings: float = 0.0,
    impact: list[str] | None = None,
    top_n: int = 25,
) -> dict:
    subs = [s.lower() for s in (subscriptions or rt.config.subscription_ids())]
    rows, rids = rt.arm.resource_graph(ADVISOR_KQL, subs)
    rules = load_owner_rules(rt.root)
    recs = []
    for r in rows:
        annual = float(r.get("annualSavingsAmount") or 0.0)
        if annual < min_annual_savings:
            continue
        if impact and r.get("impact") not in impact:
            continue
        recs.append(
            {
                "id": r.get("id"),
                "subscription": rt.config.subscription_name(r.get("subscriptionId", "")),
                "subscription_id": r.get("subscriptionId"),
                "resource_group": r.get("resourceGroup") or None,
                "resource": r.get("impactedValue"),
                "resource_type": r.get("impactedField"),
                "resource_id": r.get("resourceId"),
                "impact": r.get("impact"),
                "problem": r.get("problem"),
                "solution": r.get("solution"),
                "savings_amount_as_reported": r.get("savingsAmount"),
                "annual_savings": annual,
                "currency": r.get("savingsCurrency") or "unknown",
                "last_updated": r.get("lastUpdated"),
                "owner": route_owner(rules, r),
            }
        )
    recs.sort(key=lambda x: (-x["annual_savings"], _IMPACT_ORDER.get(x["impact"] or "", 3)))
    totals: dict[str, float] = defaultdict(float)
    for x in recs:
        totals[x["currency"]] += x["annual_savings"]
    ev = rt.evidence.new(
        tool="get_advisor_cost_recommendations",
        source="Azure Resource Graph advisorresources (Advisor, category Cost)",
        scope=[f"/subscriptions/{s}" for s in subs],
        currency=sorted(totals),
        row_count=len(recs),
        request_ids=rids,
        query={"kql": ADVISOR_KQL, "subscriptions": subs},
        notes=[
            "Savings are Advisor's estimates, not guaranteed. annual_savings comes from Advisor's "
            "annualSavingsAmount; savings_amount_as_reported is Advisor's savingsAmount (usually monthly).",
            "Recommendations can overlap (for example a reservation and a right-size on the same VM); totals may "
            "double count.",
        ],
    )
    out = {
        "status": "ok",
        "evidence_id": ev.id,
        "evidence": ev.summary(),
        "count": len(recs),
        "annual_savings_by_currency": {k: round(v, 2) for k, v in totals.items()},
        "recommendations": recs[:top_n],
    }
    if len(recs) > top_n:
        out["dataset_id"] = rt.datasets.add(recs, "advisor cost recommendations", evidence_id=ev.id).id
    if not rules:
        out["owner_routing"] = "No finops/context/owners.yaml found; owners are unassigned."
    return out
