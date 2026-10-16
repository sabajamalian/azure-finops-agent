"""Daily cost anomaly detection (Azure has no anomaly API, so this is our own deterministic detector).

Per series (subscription x configured dimensions x currency):
- expected = median of the same weekday over the trailing baseline window when there are >= 3 samples,
  otherwise the median of the whole window
- sigma = max(MAD * 1.4826, 5% of expected, 1.0)
- flag a day when impact >= min_impact, z >= z_threshold, and percent >= min_percent
  (zero baselines are labeled new_spend and skip the percent test)
- consecutive flagged days form one episode; its fingerprint uses the first flagged day so a continuing
  spike is reported once
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from .config import AnomalySettings
from .costs import CostRequest, run_cost_query
from .errors import FinOpsError
from .runtime import Runtime

NEW_SPEND_EPSILON = 1.0
REPORT_RECENT_DAYS = 7


@dataclass
class Anomaly:
    fingerprint: str
    subscription_id: str
    subscription_name: str
    dimensions: dict
    currency: str
    kind: str  # spike | new_spend
    start_date: str
    end_date: str
    days: int
    expected_daily: float
    actual_daily: float
    daily_impact: float
    total_impact: float
    pct_change: float | None
    z_score: float | None
    evidence_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def title(self) -> str:
        dims = ", ".join(f"{v}" for v in self.dimensions.values() if v) or "subscription total"
        what = "New spend" if self.kind == "new_spend" else "Cost spike"
        return (
            f"{what}: {dims} in {self.subscription_name} up {self.daily_impact:,.0f} {self.currency}/day "
            f"since {self.start_date}"
        )


def fingerprint(sub: str, dims: dict, start: str) -> str:
    key = "|".join([sub.lower(), *(f"{k}={str(v).lower()}" for k, v in sorted(dims.items())), start])
    return hashlib.sha1(key.encode()).hexdigest()[:12]  # noqa: S324 - identifier, not security


def score_day(history: list[tuple[date, float]], day: date, actual: float) -> tuple[float, float, float]:
    """Return (expected, sigma, z) for `actual` on `day` given trailing (date, cost) history."""
    values = [v for _, v in history]
    same = [v for d, v in history if d.weekday() == day.weekday()]
    expected = statistics.median(same) if len(same) >= 3 else statistics.median(values)
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) * 1.4826
    sigma = max(mad, 0.05 * expected, 1.0)
    return expected, sigma, (actual - expected) / sigma


def detect_series(series: dict[date, float], start: date, end: date, s: AnomalySettings) -> list[dict]:
    """Episodes of flagged days in [start, end]. `series` may omit zero days."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    vals = [(d, series.get(d, 0.0)) for d in days]
    flagged: list[dict] = []
    for i in range(s.baseline_days, len(vals)):
        d, actual = vals[i]
        history = vals[i - s.baseline_days : i]
        expected, _sigma, z = score_day(history, d, actual)
        impact = actual - expected
        new_spend = expected < NEW_SPEND_EPSILON
        pct = None if new_spend else impact / expected * 100
        if impact >= s.min_impact and z >= s.z_threshold and (new_spend or (pct or 0) >= s.min_percent):
            flagged.append(
                {
                    "date": d,
                    "expected": expected,
                    "actual": actual,
                    "impact": impact,
                    "pct": pct,
                    "z": z,
                    "new_spend": new_spend,
                }
            )
    episodes: list[dict] = []
    for f in flagged:
        if episodes and (f["date"] - episodes[-1]["days"][-1]["date"]).days == 1:
            episodes[-1]["days"].append(f)
        else:
            episodes.append({"days": [f]})
    out = []
    for ep in episodes:
        ds = ep["days"]
        first, last = ds[0], ds[-1]
        out.append(
            {
                "start": first["date"],
                "end": last["date"],
                "days": len(ds),
                "kind": "new_spend" if first["new_spend"] else "spike",
                "expected_daily": first["expected"],
                "actual_daily": last["actual"],
                "daily_impact": last["impact"],
                "total_impact": sum(x["impact"] for x in ds),
                "pct_change": last["pct"],
                "z_score": None if first["new_spend"] else max(x["z"] for x in ds),
            }
        )
    return out


def detect_from_rows(
    rows: list[dict],
    sub: str,
    sub_name: str,
    dims: list[str],
    start: date,
    end: date,
    s: AnomalySettings,
    evidence_id: str | None = None,
    recent_days: int = REPORT_RECENT_DAYS,
) -> list[Anomaly]:
    series: dict[tuple, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if not r.get("date"):
            continue
        key = (*[r.get(d, "") for d in dims], r["currency"])
        series[key][date.fromisoformat(r["date"])] += r["cost"]
    out: list[Anomaly] = []
    report_from = end - timedelta(days=recent_days - 1)
    for key, ser in series.items():
        dvals = dict(zip(dims, key[:-1], strict=True))
        for ep in detect_series(ser, start, end, s):
            if ep["end"] < report_from:
                continue
            out.append(
                Anomaly(
                    fingerprint=fingerprint(sub, dvals, ep["start"].isoformat()),
                    subscription_id=sub,
                    subscription_name=sub_name,
                    dimensions=dvals,
                    currency=key[-1],
                    kind=ep["kind"],
                    start_date=ep["start"].isoformat(),
                    end_date=ep["end"].isoformat(),
                    days=ep["days"],
                    expected_daily=round(ep["expected_daily"], 2),
                    actual_daily=round(ep["actual_daily"], 2),
                    daily_impact=round(ep["daily_impact"], 2),
                    total_impact=round(ep["total_impact"], 2),
                    pct_change=None if ep["pct_change"] is None else round(ep["pct_change"], 1),
                    z_score=None if ep["z_score"] is None else round(ep["z_score"], 1),
                    evidence_id=evidence_id,
                )
            )
    out.sort(key=lambda a: -a.total_impact)
    return out


def detection_window(today: date, s: AnomalySettings) -> tuple[date, date]:
    end = today - timedelta(days=1 + s.skip_recent_days)
    start = end - timedelta(days=s.lookback_days - 1)
    return start, end


def run_detection(
    rt: Runtime, subscriptions: list[str] | None = None, settings: AnomalySettings | None = None
) -> dict:
    s = settings or rt.config.anomaly
    if s.lookback_days < s.baseline_days + 1:
        raise FinOpsError("lookback_days must exceed baseline_days")
    start, end = detection_window(rt.today(), s)
    subs = [x.lower() for x in (subscriptions or rt.config.subscription_ids())]
    anomalies: list[Anomaly] = []
    errors: list[dict] = []
    evidence_ids: list[str] = []
    for sub in subs:
        req = CostRequest(f"/subscriptions/{sub}", start, end, "Daily", "ActualCost", list(s.group_by))
        try:
            rows, ev = run_cost_query(rt, req, "detect_anomalies", note="daily series for anomaly detection")
        except FinOpsError as exc:
            errors.append({"subscription_id": sub, **exc.to_dict()})
            continue
        evidence_ids.append(ev.id)
        anomalies += detect_from_rows(
            rows, sub, rt.config.subscription_name(sub), list(s.group_by), start, end, s, ev.id
        )
    anomalies.sort(key=lambda a: -a.total_impact)
    return {
        "status": "ok" if not errors else ("partial" if evidence_ids else "unavailable"),
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "settings": s.model_dump(),
        "anomalies": [a.to_dict() | {"title": a.title()} for a in anomalies],
        "errors": errors,
        "evidence_ids": evidence_ids,
        "notes": [
            f"The most recent {s.skip_recent_days} days are excluded because Cost Management data is still arriving.",
            "Only increases are reported. Expected values use same-weekday medians over the trailing "
            f"{s.baseline_days} days.",
        ],
    }
