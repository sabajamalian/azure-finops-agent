"""Change history (Activity Log + Resource Graph resourcechanges) and change-to-cost correlation."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from .config import parse_scope
from .costs import parse_date
from .errors import FinOpsError, ValidationFailed
from .runtime import Runtime

ACTIVITY_RETENTION_DAYS = 90
RESOURCECHANGES_RETENTION_DAYS = 14
CAVEAT = (
    "Correlation is not proof. These changes happened near the cost increase and touch related resources; "
    "confirm with the people involved before drawing conclusions."
)

# Cost Management ServiceName -> resource provider namespaces that usually drive it.
SERVICE_PROVIDERS = {
    "virtual machines": ("microsoft.compute",),
    "storage": ("microsoft.storage", "microsoft.compute/disks"),
    "log analytics": ("microsoft.operationalinsights", "microsoft.insights"),
    "azure monitor": ("microsoft.insights", "microsoft.operationalinsights"),
    "sql database": ("microsoft.sql",),
    "azure app service": ("microsoft.web",),
    "azure kubernetes service": ("microsoft.containerservice", "microsoft.compute"),
    "bandwidth": ("microsoft.network",),
    "virtual network": ("microsoft.network",),
    "azure cosmos db": ("microsoft.documentdb",),
    "azure databricks": ("microsoft.databricks",),
    "cognitive services": ("microsoft.cognitiveservices",),
    "foundry models": ("microsoft.cognitiveservices",),
}


def _changes_kql(start: datetime, end: datetime, rg: str | None) -> str:
    rg_clause = f"\n| where resourceGroup =~ '{rg}'" if rg else ""
    return f"""resourcechanges
| extend changeTime = todatetime(properties.changeAttributes.timestamp),
         targetResourceId = tostring(properties.targetResourceId),
         changeType = tostring(properties.changeType),
         changedBy = tostring(properties.changeAttributes.changedBy),
         clientType = tostring(properties.changeAttributes.clientType),
         operation = tostring(properties.changeAttributes.operation),
         changedProperties = bag_keys(properties.changes)
| where changeTime between (datetime({start.isoformat()}) .. datetime({end.isoformat()})){rg_clause}
| project changeTime, targetResourceId, changeType, changedBy, clientType, operation, subscriptionId,
          resourceGroup, changedProperties
| order by changeTime desc"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _op_kind(op: str) -> str:
    o = op.lower()
    if o.endswith("/delete"):
        return "delete"
    if o.endswith("/action"):
        return "action"
    if o.endswith("/write"):
        return "write"
    return "other"


def fetch_changes(
    rt: Runtime, sub: str, rg: str | None, start: datetime, end: datetime
) -> tuple[list[dict], list[str], list[str]]:
    """Returns (changes, evidence_ids, notes). Missing permissions on one source don't fail the other."""
    changes: list[dict] = []
    evidence_ids: list[str] = []
    notes: list[str] = []
    flt = f"eventTimestamp ge '{_iso(start)}' and eventTimestamp le '{_iso(end)}'"
    if rg:
        flt += f" and resourceGroupName eq '{rg}'"
    path = f"/subscriptions/{sub}/providers/Microsoft.Insights/eventtypes/management/values"
    try:
        rids = []
        for page in rt.arm.get_paged(path, {"api-version": "2015-04-01", "$filter": flt}):
            if page.request_id:
                rids.append(page.request_id)
            for e in page.body.get("value", []):
                status = (e.get("status") or {}).get("value", "")
                op = (e.get("operationName") or {}).get("value", "")
                category = (e.get("category") or {}).get("value", "Administrative")
                if status != "Succeeded" or category != "Administrative" or _op_kind(op) == "other":
                    continue
                changes.append(
                    {
                        "time": e.get("eventTimestamp"),
                        "source": "activity_log",
                        "caller": e.get("caller"),
                        "operation": op,
                        "operation_label": (e.get("operationName") or {}).get("localizedValue"),
                        "kind": _op_kind(op),
                        "resource_id": (e.get("resourceId") or "").lower(),
                        "resource_group": (e.get("resourceGroupName") or "").lower(),
                        "subscription_id": sub,
                        "correlation_id": e.get("correlationId"),
                    }
                )
        ev = rt.evidence.new(
            tool="get_change_history",
            source="Activity Log (Administrative, succeeded)",
            scope=[path.split("/providers")[0]],
            period_from=_iso(start),
            period_to=_iso(end),
            row_count=len(changes),
            request_ids=rids,
            query={"path": path, "filter": flt},
        )
        evidence_ids.append(ev.id)
    except FinOpsError as exc:
        notes.append(
            f"Activity Log unavailable: {exc.message}"
            + (f" Fix: {exc.remediation}" if exc.remediation else "")
        )

    rc_start = max(
        start,
        datetime.combine(rt.today(), datetime.min.time(), UTC)
        - timedelta(days=RESOURCECHANGES_RETENTION_DAYS),
    )
    if rc_start < end:
        n_before = len(changes)
        try:
            kql = _changes_kql(rc_start, end, rg)
            rows, rids = rt.arm.resource_graph(kql, [sub])
            for r in rows:
                op = r.get("operation") or ""
                changes.append(
                    {
                        "time": r.get("changeTime"),
                        "source": "resource_changes",
                        "caller": r.get("changedBy") or None,
                        "client": r.get("clientType"),
                        "operation": op,
                        "change_type": r.get("changeType"),
                        "kind": {"Create": "create", "Delete": "delete"}.get(
                            r.get("changeType") or "", _op_kind(op)
                        ),
                        "resource_id": (r.get("targetResourceId") or "").lower(),
                        "resource_group": (r.get("resourceGroup") or "").lower(),
                        "subscription_id": sub,
                        "changed_properties": r.get("changedProperties") or [],
                    }
                )
            ev = rt.evidence.new(
                tool="get_change_history",
                source="Azure Resource Graph resourcechanges",
                scope=[f"/subscriptions/{sub}"],
                period_from=_iso(rc_start),
                period_to=_iso(end),
                row_count=len(changes) - n_before,
                request_ids=rids,
                query={"kql": kql},
            )
            evidence_ids.append(ev.id)
        except FinOpsError as exc:
            notes.append(f"Resource Graph change history unavailable: {exc.message}")
    if start < rc_start:
        notes.append(
            f"Resource Graph keeps {RESOURCECHANGES_RETENTION_DAYS} days of property-level changes; earlier changes "
            "come from the Activity Log only."
        )
    return changes, evidence_ids, notes


def _merge(changes: list[dict]) -> list[dict]:
    """Collapse the same change seen in both sources (same resource, within 10 minutes)."""
    out: list[dict] = []
    for c in sorted(changes, key=lambda x: (x["resource_id"], x["time"] or "")):
        prev = out[-1] if out else None
        if (
            prev
            and prev["resource_id"] == c["resource_id"]
            and prev["source"] != c["source"]
            and abs((_dt(prev["time"]) - _dt(c["time"])).total_seconds()) <= 600
        ):
            prev["source"] = "activity_log+resource_changes"
            for k in ("change_type", "client", "changed_properties"):
                if c.get(k) and not prev.get(k):
                    prev[k] = c[k]
            if prev.get("kind") == "write" and c.get("kind") == "create":
                prev["kind"] = "create"
            continue
        out.append(dict(c))
    out.sort(key=lambda x: x["time"] or "", reverse=True)
    return out


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _window(rt: Runtime, start_date: str, end_date: str) -> tuple[datetime, datetime]:
    s, e = parse_date(start_date), parse_date(end_date)
    if e < s:
        raise ValidationFailed("end_date is before start_date")
    earliest = rt.today() - timedelta(days=ACTIVITY_RETENTION_DAYS - 1)
    if s < earliest:
        s = earliest
    return datetime.combine(s, datetime.min.time(), UTC), datetime.combine(
        e, datetime.max.time(), UTC
    ).replace(microsecond=0)


def get_change_history(
    rt: Runtime,
    scope: str,
    start_date: str,
    end_date: str,
    resource_id_contains: str | None = None,
    max_items: int = 100,
) -> dict:
    sub, rg = parse_scope(scope)
    start, end = _window(rt, start_date, end_date)
    changes, ev_ids, notes = fetch_changes(rt, sub, rg, start, end)
    merged = _merge(changes)
    if resource_id_contains:
        merged = [c for c in merged if resource_id_contains.lower() in c["resource_id"]]
    out = {
        "status": "ok" if ev_ids else "unavailable",
        "scope": scope,
        "period": {"from": _iso(start), "to": _iso(end)},
        "count": len(merged),
        "changes": merged[:max_items],
        "evidence_ids": ev_ids,
        "notes": notes,
    }
    if len(merged) > max_items:
        out["dataset_id"] = rt.datasets.add(merged, f"change history {scope}", evidence_id=ev_ids[0]).id
    return out


def _resource_match(change: dict, rids: set[str], rgs: set[str], services: set[str]) -> tuple[float, str]:
    cid = change["resource_id"]
    for rid in rids:
        if cid == rid or cid.startswith(rid + "/") or rid.startswith(cid + "/"):
            return 1.0, "same resource"
        # VM <-> its NIC/disk naming convention, for example vm-x and vm-x-nic
        name, cname = rid.rsplit("/", 1)[-1], cid.rsplit("/", 1)[-1]
        if name and (cname == name or (cname.startswith(name) and cname[len(name)] in "-_.")):
            return 0.8, "companion resource"
    providers = {p for s in services for p in SERVICE_PROVIDERS.get(s.lower(), ())}
    type_hit = any(f"/providers/{p}" in cid for p in providers)
    if change["resource_group"] and change["resource_group"] in rgs:
        return (0.7, "same resource group and service type") if type_hit else (0.5, "same resource group")
    return (0.35, "same subscription and service type") if type_hit else (0.2, "same subscription")


def _op_weight(change: dict) -> float:
    op = change["operation"].lower()
    if "/tags/" in op or op.endswith("roleassignments/write") or "/locks/" in op:
        return 0.15
    kind = change.get("kind")
    if kind == "create":
        return 1.0
    if kind == "write":
        return 0.85
    if kind == "action":
        return 0.9 if any(x in op for x in ("/start/", "/scale", "/restart/")) else 0.4
    if kind == "delete":
        return 0.3
    return 0.3


def _time_weight(change_time: datetime, anomaly_start: date) -> float:
    start = datetime.combine(anomaly_start, datetime.min.time(), UTC)
    delta_days = (start - change_time).total_seconds() / 86400
    if delta_days < -1:
        return 0.0
    if delta_days < 0:
        return 0.7  # same day as the increase (cost day boundaries are UTC)
    return math.exp(-delta_days / 2.0)


def correlate_changes(
    rt: Runtime,
    scope: str,
    increase_start_date: str,
    resource_ids: list[str] | None = None,
    resource_groups: list[str] | None = None,
    service_names: list[str] | None = None,
    days_before: int = 3,
    max_items: int = 10,
) -> dict:
    sub, rg = parse_scope(scope)
    d0 = parse_date(increase_start_date)
    days_before = max(1, min(days_before, 14))
    start = datetime.combine(d0 - timedelta(days=days_before), datetime.min.time(), UTC)
    end = datetime.combine(d0 + timedelta(days=1), datetime.max.time(), UTC).replace(microsecond=0)
    changes, ev_ids, notes = fetch_changes(rt, sub, rg, start, end)
    rids = {r.lower() for r in resource_ids or []}
    rgs = {g.lower() for g in resource_groups or []} | ({rg.lower()} if rg else set())
    for rid in rids:
        parts = rid.split("/")
        if "resourcegroups" in parts:
            rgs.add(parts[parts.index("resourcegroups") + 1])
    services = set(service_names or [])
    ranked = []
    for c in _merge(changes):
        rm, why = _resource_match(c, rids, rgs, services)
        tw = _time_weight(_dt(c["time"]), d0)
        ow = _op_weight(c)
        score = round(rm * tw * ow, 3)
        if score <= 0.02:
            continue
        conf = "high" if score >= 0.5 else "medium" if score >= 0.2 else "low"
        ranked.append({**c, "score": score, "confidence": conf, "why": f"{why}; {_ago(d0, _dt(c['time']))}"})
    ranked.sort(key=lambda x: -x["score"])
    return {
        "status": "ok" if ev_ids else "unavailable",
        "scope": scope,
        "increase_start_date": d0.isoformat(),
        "window": {"from": _iso(start), "to": _iso(end)},
        "candidates": ranked[:max_items],
        "candidate_count": len(ranked),
        "caveat": CAVEAT,
        "evidence_ids": ev_ids,
        "notes": notes,
    }


def _ago(d0: date, t: datetime) -> str:
    hours = (datetime.combine(d0, datetime.min.time(), UTC) - t).total_seconds() / 3600
    if hours >= 0:
        return f"{hours:.0f}h before the increase began (UTC)"
    return f"{-hours:.0f}h after the start of the first increased day (UTC)"
