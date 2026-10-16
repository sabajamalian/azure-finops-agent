"""Cost Management query building, response parsing, and the query_costs tool."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import COST_DIMENSIONS, CostBasis, parse_scope
from .errors import ValidationFailed
from .evidence import Evidence
from .ratelimit import qpu_for_days
from .runtime import Runtime

INLINE_ROW_LIMIT = 50
DATA_LATENCY_DAYS = 2
_COST_COLS = ("Cost", "PreTaxCost", "totalCost", "CostUSD", "PreTaxCostUSD", "costInBillingCurrency")
_DATE_COLS = ("UsageDate", "BillingMonth", "Date")
GRANULARITIES = ("None", "Daily", "Monthly")


def parse_date(v: str | date) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(v.strip()[:10])
    except ValueError as exc:
        raise ValidationFailed(f"invalid date {v!r}; use YYYY-MM-DD") from exc


@dataclass
class CostRequest:
    scope: str
    start: date
    end: date  # inclusive
    granularity: str = "None"
    basis: CostBasis = "ActualCost"
    group_by: list[str] = field(default_factory=list)
    filters: dict[str, list[str]] = field(default_factory=dict)

    def validate(self, rt: Runtime) -> None:
        sub, _ = parse_scope(self.scope)
        if sub not in rt.config.subscription_ids():
            raise ValidationFailed(
                f"subscription {sub} is not configured; configured: {', '.join(rt.config.subscription_ids())}"
            )
        if self.end < self.start:
            raise ValidationFailed("end date is before start date")
        if (self.end - self.start).days > 365:
            raise ValidationFailed("a single cost query can cover at most 366 days")
        if self.granularity not in GRANULARITIES:
            raise ValidationFailed(f"granularity must be one of {GRANULARITIES}")
        if len(self.group_by) > 2:
            raise ValidationFailed("Cost Management supports at most 2 group_by dimensions per query")
        for d in [*self.group_by, *self.filters]:
            if d not in COST_DIMENSIONS and not d.startswith("tag:"):
                raise ValidationFailed(
                    f"unsupported dimension {d!r}; use one of {', '.join(COST_DIMENSIONS)} or tag:<key>"
                )

    @property
    def scope_path(self) -> str:
        sub, rg = parse_scope(self.scope)
        return f"/subscriptions/{sub}" + (f"/resourceGroups/{rg}" if rg else "")

    def body(self) -> dict:
        ds: dict = {
            "granularity": self.granularity,
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        }
        if self.group_by:
            ds["grouping"] = [
                {"type": "TagKey", "name": g[4:]}
                if g.startswith("tag:")
                else {"type": "Dimension", "name": g}
                for g in self.group_by
            ]
        clauses = []
        for dim, values in self.filters.items():
            if not values:
                continue
            if dim.startswith("tag:"):
                clauses.append({"tags": {"name": dim[4:], "operator": "In", "values": list(values)}})
            else:
                clauses.append({"dimensions": {"name": dim, "operator": "In", "values": list(values)}})
        if len(clauses) == 1:
            ds["filter"] = clauses[0]
        elif clauses:
            ds["filter"] = {"and": clauses}
        return {
            "type": self.basis,
            "timeframe": "Custom",
            "timePeriod": {
                "from": f"{self.start.isoformat()}T00:00:00Z",
                "to": f"{self.end.isoformat()}T23:59:59Z",
            },
            "dataset": ds,
        }


def _norm_date(v) -> str | None:
    if v is None:
        return None
    s = str(v)
    if s.isdigit() and len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return s[:10]


def parse_rows(pages: list[dict], group_by: list[str]) -> list[dict]:
    """Normalize Cost Management query pages into rows {date?, <dims>, cost, currency}."""
    out: list[dict] = []
    tag_keys = [g for g in group_by if g.startswith("tag:")]
    for page in pages:
        props = page.get("properties") or {}
        cols = [c.get("name") for c in props.get("columns") or []]
        if not cols:
            continue
        cost_i = next((cols.index(c) for c in _COST_COLS if c in cols), None)
        if cost_i is None:
            num = [
                i
                for i, c in enumerate(props["columns"])
                if c.get("type") == "Number" and cols[i] not in _DATE_COLS
            ]
            cost_i = num[0] if num else None
        if cost_i is None:
            raise ValidationFailed(f"cost column not found in response columns {cols}")
        date_i = next((cols.index(c) for c in _DATE_COLS if c in cols), None)
        cur_i = next((cols.index(c) for c in ("Currency", "BillingCurrency") if c in cols), None)
        for raw in props.get("rows") or []:
            row: dict = {}
            if date_i is not None:
                row["date"] = _norm_date(raw[date_i])
            for g in group_by:
                if g.startswith("tag:"):
                    continue
                if g in cols:
                    row[g] = raw[cols.index(g)] or ""
            if tag_keys and "TagValue" in cols:
                row[tag_keys[0]] = raw[cols.index("TagValue")] or "(untagged)"
            row["cost"] = float(raw[cost_i] or 0.0)
            row["currency"] = raw[cur_i] if cur_i is not None else "unknown"
            out.append(row)
    return out


def run_cost_query(
    rt: Runtime, req: CostRequest, tool: str, note: str | None = None
) -> tuple[list[dict], Evidence]:
    req.validate(rt)
    today = rt.today()
    notes: list[str] = []
    if note:
        notes.append(note)
    if req.end >= today:
        notes.append(
            f"End date clamped from {req.end} to {today - timedelta(days=1)}: today's costs are not available."
        )
        req.end = today - timedelta(days=1)
        if req.end < req.start:
            raise ValidationFailed("the requested period has no completed days yet")
    body = req.body()
    days = (req.end - req.start).days + 1
    responses = rt.arm.cost_query(req.scope_path, body, qpu=qpu_for_days(days))
    rows = parse_rows([r.body for r in responses], req.group_by)
    currencies = sorted({r["currency"] for r in rows})
    incomplete = req.end >= today - timedelta(days=DATA_LATENCY_DAYS)
    if incomplete:
        notes.append(
            f"Includes the last {DATA_LATENCY_DAYS} days, which may still be incomplete (Cost Management data lags 8 to 48 hours)."
        )
    if len(currencies) > 1:
        notes.append(
            "Multiple currencies present; totals are reported per currency and never summed across currencies."
        )
    data_through = req.end.isoformat()
    if req.granularity == "Daily":
        dated = [r["date"] for r in rows if r.get("date") and r["cost"] != 0]
        if dated:
            data_through = max(dated)
    partial_month = req.granularity == "Monthly" and (
        req.start.day != 1 or (req.end + timedelta(days=1)).day != 1
    )
    if partial_month:
        notes.append("First or last month is partial; compare like-for-like day counts.")
    ev = rt.evidence.new(
        tool=tool,
        source="Cost Management query",
        scope=[req.scope_path],
        period_from=req.start.isoformat(),
        period_to=req.end.isoformat(),
        granularity=req.granularity,
        cost_basis=req.basis,
        currency=currencies,
        data_through=data_through,
        partial_period=incomplete or partial_month,
        row_count=len(rows),
        request_ids=[r.request_id for r in responses if r.request_id],
        query={"scope": req.scope_path, "body": body},
        notes=notes,
    )
    return rows, ev


def totals_by_currency(rows: list[dict]) -> dict[str, float]:
    t: dict[str, float] = defaultdict(float)
    for r in rows:
        t[r["currency"]] += r["cost"]
    return {k: round(v, 2) for k, v in t.items()}


def present_rows(
    rt: Runtime, rows: list[dict], ev: Evidence, description: str, sort_desc: bool = True
) -> dict:
    """Return rows inline when small; otherwise store a dataset and return the top rows plus its id."""
    ordered = sorted(rows, key=lambda r: -r["cost"]) if sort_desc else rows
    shown = [{**r, "cost": round(r["cost"], 2)} for r in ordered[:INLINE_ROW_LIMIT]]
    out = {"rows": shown, "row_count": len(rows)}
    if len(rows) > INLINE_ROW_LIMIT:
        ds = rt.datasets.add(rows, description, evidence_id=ev.id)
        out["dataset_id"] = ds.id
        out["dataset_columns"] = ds.columns
        out["note"] = (
            f"Showing top {INLINE_ROW_LIMIT} of {len(rows)} rows by cost. "
            f"Use query_dataset with dataset_id {ds.id} for the rest."
        )
    return out


def query_costs(
    rt: Runtime,
    scope: str,
    start_date: str,
    end_date: str,
    granularity: str = "None",
    group_by: list[str] | None = None,
    filters: dict[str, list[str]] | None = None,
    cost_basis: str | None = None,
) -> dict:
    req = CostRequest(
        scope=scope,
        start=parse_date(start_date),
        end=parse_date(end_date),
        granularity=granularity,
        basis=cost_basis or rt.config.azure.cost_basis,  # type: ignore[arg-type]
        group_by=list(group_by or []),
        filters=dict(filters or {}),
    )
    if req.basis not in ("ActualCost", "AmortizedCost"):
        raise ValidationFailed("cost_basis must be ActualCost or AmortizedCost")
    rows, ev = run_cost_query(rt, req, "query_costs")
    sort_desc = granularity == "None"
    body = present_rows(rt, rows, ev, f"query_costs {req.scope_path} {req.start}..{req.end}", sort_desc)
    return {
        "status": "ok",
        "evidence_id": ev.id,
        "evidence": ev.summary(),
        "totals": totals_by_currency(rows),
        **body,
    }
