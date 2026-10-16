"""Period comparison and driver analysis (deterministic math the model must not redo)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .costs import CostRequest, parse_date, run_cost_query, totals_by_currency
from .errors import ValidationFailed
from .runtime import Runtime

RECONCILE_TOLERANCE = 0.005  # 0.5% of total, or 1 currency unit, whichever is larger


def _periods(
    current_start: str, current_end: str, baseline_start: str | None, baseline_end: str | None
) -> tuple[date, date, date, date]:
    cs, ce = parse_date(current_start), parse_date(current_end)
    if ce < cs:
        raise ValidationFailed("current_end is before current_start")
    if baseline_start and baseline_end:
        bs, be = parse_date(baseline_start), parse_date(baseline_end)
    elif baseline_start or baseline_end:
        raise ValidationFailed("give both baseline_start and baseline_end, or neither")
    else:
        n = (ce - cs).days + 1
        be = cs - timedelta(days=1)
        bs = be - timedelta(days=n - 1)
    if be < bs:
        raise ValidationFailed("baseline_end is before baseline_start")
    if not (be < cs or bs > ce):
        raise ValidationFailed("baseline and current periods overlap")
    return cs, ce, bs, be


def compare_rows(
    base_rows: list[dict], cur_rows: list[dict], dims: list[str], base_days: int, cur_days: int
) -> dict[str, dict]:
    """Per-currency comparison. Baseline is scaled to the current period length when day counts differ."""
    scale = cur_days / base_days if base_days else 1.0
    by_cur: dict[str, dict[tuple, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in base_rows:
        by_cur[r["currency"]][tuple(r.get(d, "") for d in dims)][0] += r["cost"]
    for r in cur_rows:
        by_cur[r["currency"]][tuple(r.get(d, "") for d in dims)][1] += r["cost"]
    out: dict[str, dict] = {}
    for currency, groups in by_cur.items():
        base_total = sum(v[0] for v in groups.values())
        cur_total = sum(v[1] for v in groups.values())
        base_norm_total = base_total * scale
        total_delta = cur_total - base_norm_total
        items = []
        for key, (b, c) in groups.items():
            bn = b * scale
            delta = c - bn
            if bn == 0 and c > 0:
                status = "new"
            elif c == 0 and bn > 0:
                status = "removed"
            elif abs(delta) < 0.005:
                status = "unchanged"
            else:
                status = "increased" if delta > 0 else "decreased"
            items.append(
                {
                    **dict(zip(dims, key, strict=True)),
                    "baseline": round(b, 2),
                    "baseline_normalized": round(bn, 2),
                    "current": round(c, 2),
                    "delta": round(delta, 2),
                    "pct_change": None if bn == 0 else round(delta / bn * 100, 1),
                    "share_of_total_change": None
                    if abs(total_delta) < 0.005
                    else round(delta / total_delta * 100, 1),
                    "status": status,
                }
            )
        items.sort(key=lambda x: -abs(x["delta"]))
        out[currency] = {
            "baseline_total": round(base_total, 2),
            "baseline_total_normalized": round(base_norm_total, 2),
            "current_total": round(cur_total, 2),
            "delta": round(total_delta, 2),
            "pct_change": None if base_norm_total == 0 else round(total_delta / base_norm_total * 100, 1),
            "items": items,
        }
    return out


def _reconcile(grouped: list[dict], totals: list[dict]) -> list[str]:
    g, t = totals_by_currency(grouped), totals_by_currency(totals)
    notes = []
    for cur in sorted(set(g) | set(t)):
        a, b = g.get(cur, 0.0), t.get(cur, 0.0)
        if abs(a - b) > max(1.0, abs(b) * RECONCILE_TOLERANCE):
            notes.append(
                f"Reconciliation gap in {cur}: grouped rows sum to {a:,.2f} but the ungrouped total is {b:,.2f}. "
                "Some cost may lack the grouping dimension (for example purchases without a resource)."
            )
    return notes


def compare_periods(
    rt: Runtime,
    scope: str,
    current_start: str,
    current_end: str,
    baseline_start: str | None = None,
    baseline_end: str | None = None,
    group_by: list[str] | None = None,
    filters: dict[str, list[str]] | None = None,
    cost_basis: str | None = None,
    top_n: int = 15,
) -> dict:
    cs, ce, bs, be = _periods(current_start, current_end, baseline_start, baseline_end)
    dims = list(group_by or ["ServiceName"])
    basis = cost_basis or rt.config.azure.cost_basis

    def q(start, end, gb, label):
        req = CostRequest(scope, start, end, "None", basis, list(gb), dict(filters or {}))  # type: ignore[arg-type]
        return run_cost_query(rt, req, "compare_periods", note=label)

    cur_rows, cur_ev = q(cs, ce, dims, "current period, grouped")
    ce = date.fromisoformat(cur_ev.period_to)  # may have been clamped to yesterday
    if ce < cs:
        raise ValidationFailed("current period has no completed days")
    base_rows, base_ev = q(bs, be, dims, "baseline period, grouped")
    cur_tot, cur_tot_ev = q(cs, ce, [], "current period, total for reconciliation")
    base_tot, base_tot_ev = q(bs, be, [], "baseline period, total for reconciliation")

    cur_days, base_days = (ce - cs).days + 1, (be - bs).days + 1
    result = compare_rows(base_rows, cur_rows, dims, base_days, cur_days)
    notes = _reconcile(cur_rows, cur_tot) + _reconcile(base_rows, base_tot)
    if cur_days != base_days:
        notes.append(
            f"Periods have different lengths ({base_days} vs {cur_days} days); the baseline is scaled by "
            f"{cur_days}/{base_days} so deltas compare like-for-like daily run rates."
        )
    if len(result) > 1:
        notes.append("Multiple currencies: each currency is compared separately and never summed.")
    for ev in (cur_ev, base_ev):
        notes.extend(n for n in ev.notes if "incomplete" in n and n not in notes)

    by_currency = {}
    for cur, r in result.items():
        items = r.pop("items")
        if len(items) > top_n:
            ds = rt.datasets.add(
                items, f"compare_periods {scope} {bs}..{be} vs {cs}..{ce}", evidence_id=cur_ev.id
            )
            r["dataset_id"] = ds.id
        r["top_changes"] = items[:top_n]
        r["groups_compared"] = len(items)
        by_currency[cur] = r
    return {
        "status": "ok",
        "basis": basis,
        "group_by": dims,
        "current_period": {"from": cs.isoformat(), "to": ce.isoformat(), "days": cur_days},
        "baseline_period": {"from": bs.isoformat(), "to": be.isoformat(), "days": base_days},
        "by_currency": by_currency,
        "notes": notes,
        "evidence_ids": [cur_ev.id, base_ev.id, cur_tot_ev.id, base_tot_ev.id],
    }


def explain_drivers(
    rt: Runtime,
    scope: str,
    current_start: str,
    current_end: str,
    baseline_start: str | None = None,
    baseline_end: str | None = None,
    service_names: list[str] | None = None,
    cost_basis: str | None = None,
    top_n: int = 10,
) -> dict:
    """Drill from services down to resources and meters for the biggest movers."""
    cs, ce, bs, be = _periods(current_start, current_end, baseline_start, baseline_end)
    basis = cost_basis or rt.config.azure.cost_basis
    filters = {"ServiceName": list(service_names)} if service_names else {}
    evidence_ids: list[str] = []
    sections = {}
    for label, dims in (
        ("by_resource", ["ServiceName", "ResourceId"]),
        ("by_meter", ["ServiceName", "Meter"]),
    ):
        rows = {}
        for which, (s, e) in (("current", (cs, ce)), ("baseline", (bs, be))):
            req = CostRequest(scope, s, e, "None", basis, dims, dict(filters))  # type: ignore[arg-type]
            rows[which], ev = run_cost_query(rt, req, "explain_drivers", note=f"{which} {label}")
            evidence_ids.append(ev.id)
            if which == "current":
                ce = date.fromisoformat(ev.period_to)
        cmp = compare_rows(rows["baseline"], rows["current"], dims, (be - bs).days + 1, (ce - cs).days + 1)
        sections[label] = {
            cur: {"delta": r["delta"], "top": r["items"][:top_n], "groups": len(r["items"])}
            for cur, r in cmp.items()
        }
    return {
        "status": "ok",
        "basis": basis,
        "current_period": {"from": cs.isoformat(), "to": ce.isoformat()},
        "baseline_period": {"from": bs.isoformat(), "to": be.isoformat()},
        "service_filter": service_names or [],
        **sections,
        "evidence_ids": evidence_ids,
        "notes": [
            "Resource and meter views are separate queries; each ranks movers by absolute change in the current "
            "period versus the length-normalized baseline."
        ],
    }
