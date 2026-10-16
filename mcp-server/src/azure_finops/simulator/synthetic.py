"""Synthetic Azure: an httpx MockTransport that answers the read-only ARM calls the server makes.

Used by unit tests, evals, and `FINOPS_FIXTURE_MODE=synthetic` demos. Data is deterministic.

Scenario (relative to `today`):
- contoso-prod runs a steady web, data, and shared platform.
- 6 days ago alice@contoso.com created vm-gpu-train-01 (NC6s v3) in rg-ml: new spend of ~1,180 USD/day.
- 5 days ago bob@contoso.com added a diagnostic setting that sends app logs to Log Analytics:
  ingestion in rg-shared jumps from ~38 to ~160 USD/day starting 4 days ago.
- contoso-dev is small and stable, with weekend auto-shutdown.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

from ..config import AzureSettings, Config, Destination, Subscription

TENANT = "00000000-0000-0000-0000-0000000000aa"
PROD = "11111111-1111-1111-1111-111111111111"
DEV = "22222222-2222-2222-2222-222222222222"
DEFAULT_TODAY = date(2026, 3, 18)
SUB_NAMES = {PROD: "contoso-prod", DEV: "contoso-dev"}


def synthetic_config() -> Config:
    return Config(
        azure=AzureSettings(
            tenant_id=TENANT,
            subscriptions=[
                Subscription(id=PROD, name="contoso-prod"),
                Subscription(id=DEV, name="contoso-dev"),
            ],
        ),
        timezone="America/Los_Angeles",
        destinations=[
            Destination(id="finops-team", labels=["finops"]),
            Destination(id="platform-team", labels=["finops", "platform"]),
        ],
    )


@dataclass
class Res:
    sub: str
    rg: str
    name: str
    rtype: str
    service: str
    meter: str
    base: float
    weekend: float = 1.0
    location: str = "eastus"
    tags: dict = field(default_factory=dict)
    start: int | None = None  # days before today the resource started billing
    step: tuple[int, float] | None = None  # (days before today, new base)

    @property
    def id(self) -> str:
        return (
            f"/subscriptions/{self.sub}/resourceGroups/{self.rg}/providers/{self.rtype}/{self.name}".lower()
        )


RESOURCES = [
    Res(
        PROD,
        "rg-web",
        "app-contoso-web",
        "Microsoft.Web/sites",
        "Azure App Service",
        "P1v3 App",
        96,
        tags={"env": "prod", "team": "web"},
    ),
    Res(
        PROD,
        "rg-web",
        "sql-contoso/databases/db-orders",
        "Microsoft.Sql/servers",
        "SQL Database",
        "vCore",
        140,
        tags={"env": "prod", "team": "web"},
    ),
    Res(
        PROD,
        "rg-web",
        "stcontosoweb",
        "Microsoft.Storage/storageAccounts",
        "Storage",
        "Hot LRS Data Stored",
        22,
        tags={"env": "prod", "team": "web"},
    ),
    Res(
        PROD,
        "rg-data",
        "aks-nodepool1-vmss",
        "Microsoft.Compute/virtualMachineScaleSets",
        "Virtual Machines",
        "D4s v5",
        210,
        weekend=0.75,
        tags={"env": "prod", "team": "data"},
    ),
    Res(
        PROD,
        "rg-data",
        "disk-old-01",
        "Microsoft.Compute/disks",
        "Storage",
        "P10 Managed Disk",
        4.9,
        tags={"env": "prod", "team": "data"},
    ),
    Res(
        PROD,
        "rg-ml",
        "vm-gpu-train-01",
        "Microsoft.Compute/virtualMachines",
        "Virtual Machines",
        "NC6s v3",
        1180,
        tags={"env": "prod", "team": "ml"},
        start=6,
    ),
    Res(
        PROD,
        "rg-ml",
        "stcontosoml",
        "Microsoft.Storage/storageAccounts",
        "Storage",
        "Hot LRS Data Stored",
        15,
        tags={"env": "prod", "team": "ml"},
    ),
    Res(
        PROD,
        "rg-shared",
        "log-contoso",
        "Microsoft.OperationalInsights/workspaces",
        "Log Analytics",
        "Analytics Logs Data Ingestion",
        38,
        tags={"env": "prod", "team": "platform"},
        step=(4, 160),
    ),
    Res(
        PROD,
        "rg-shared",
        "egress",
        "Microsoft.Network/publicIPAddresses",
        "Bandwidth",
        "Standard Data Transfer Out",
        9,
        tags={"env": "prod", "team": "platform"},
    ),
    Res(
        DEV,
        "rg-dev",
        "vm-dev-01",
        "Microsoft.Compute/virtualMachines",
        "Virtual Machines",
        "D2s v5",
        24,
        weekend=0.3,
        tags={"env": "dev", "team": "web"},
    ),
    Res(
        DEV,
        "rg-dev",
        "stcontosodev",
        "Microsoft.Storage/storageAccounts",
        "Storage",
        "Hot LRS Data Stored",
        4,
        tags={"env": "dev"},
    ),
    Res(
        DEV,
        "rg-dev",
        "app-dev",
        "Microsoft.Web/sites",
        "Azure App Service",
        "B1 App",
        12,
        tags={"env": "dev"},
    ),
]

DIM_ATTR = {
    "SubscriptionId": lambda r: r.sub,
    "ResourceGroupName": lambda r: r.rg,
    "ResourceId": lambda r: r.id,
    "ResourceType": lambda r: r.rtype.lower(),
    "ResourceLocation": lambda r: r.location,
    "ServiceName": lambda r: r.service,
    "MeterCategory": lambda r: r.service,
    "Meter": lambda r: r.meter,
    "ChargeType": lambda r: "Usage",
    "PricingModel": lambda r: "OnDemand",
}


def _noise(key: str) -> float:
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
    return (h / 0xFFFFFFFF - 0.5) * 0.06


class SyntheticAzure:
    def __init__(
        self, today: date | None = None, deny: set[tuple[str, str]] | None = None, page_size: int = 5000
    ):
        self.today = today or DEFAULT_TODAY
        self.deny = deny or set()  # {(kind, subscription)}
        self.page_size = page_size
        self.calls: list[tuple[str, str]] = []

    # -- data -----------------------------------------------------------------------------------
    def cost(self, r: Res, d: date) -> float:
        age = (self.today - d).days
        if r.start is not None and age > r.start:
            return 0.0
        if d >= self.today:
            return 0.0
        base = r.step[1] if r.step and age <= r.step[0] else r.base
        if d.weekday() >= 5:
            base *= r.weekend
        return round(base * (1 + _noise(f"{r.id}|{d}")), 4)

    def activity_events(self, sub: str) -> list[dict]:
        t = self.today
        ev = []

        def add(days_ago, hour, caller, op, label, rid, rg, status="Succeeded", category="Administrative"):
            ts = datetime.combine(t - timedelta(days=days_ago), datetime.min.time(), UTC) + timedelta(
                hours=hour
            )
            ev.append(
                {
                    "caller": caller,
                    "eventTimestamp": ts.isoformat().replace("+00:00", "Z"),
                    "operationName": {"value": op, "localizedValue": label},
                    "status": {"value": status, "localizedValue": status},
                    "category": {"value": category},
                    "resourceId": rid,
                    "resourceGroupName": rg,
                    "subscriptionId": sub,
                    "correlationId": hashlib.md5(f"{rid}{ts}{status}".encode()).hexdigest(),  # noqa: S324
                    "eventDataId": hashlib.md5(f"{rid}{ts}{op}{status}".encode()).hexdigest(),  # noqa: S324
                    "level": "Informational",
                }
            )

        if sub == PROD:
            gpu = next(r for r in RESOURCES if r.name == "vm-gpu-train-01")
            add(
                6,
                9.5,
                "alice@contoso.com",
                "Microsoft.Compute/virtualMachines/write",
                "Create or Update Virtual Machine",
                gpu.id,
                "rg-ml",
                status="Started",
            )
            add(
                6,
                9.6,
                "alice@contoso.com",
                "Microsoft.Compute/virtualMachines/write",
                "Create or Update Virtual Machine",
                gpu.id,
                "rg-ml",
            )
            add(
                6,
                9.4,
                "alice@contoso.com",
                "Microsoft.Network/networkInterfaces/write",
                "Create or Update Network Interface",
                f"/subscriptions/{PROD}/resourceGroups/rg-ml/providers/Microsoft.Network/networkInterfaces/vm-gpu-train-01-nic",
                "rg-ml",
            )
            web = next(r for r in RESOURCES if r.name == "app-contoso-web")
            add(
                5,
                16.2,
                "bob@contoso.com",
                "Microsoft.Insights/diagnosticSettings/write",
                "Create or update resource diagnostic setting",
                f"{web.id}/providers/microsoft.insights/diagnosticsettings/send-to-la",
                "rg-web",
            )
            add(
                3,
                11.0,
                "carol@contoso.com",
                "Microsoft.Resources/tags/write",
                "Update tags",
                f"/subscriptions/{PROD}/resourceGroups/rg-web/providers/Microsoft.Storage/storageAccounts/stcontosoweb",
                "rg-web",
            )
            add(20, 14.0, "pipeline-sp", "Microsoft.Web/sites/write", "Update website", web.id, "rg-web")
        else:
            add(
                8,
                10.0,
                "dave@contoso.com",
                "Microsoft.Compute/disks/delete",
                "Delete Disk",
                f"/subscriptions/{DEV}/resourceGroups/rg-dev/providers/Microsoft.Compute/disks/vm-dev-02-osdisk",
                "rg-dev",
            )
            add(
                2,
                18.0,
                "dave@contoso.com",
                "Microsoft.Compute/virtualMachines/deallocate/action",
                "Deallocate Virtual Machine",
                f"/subscriptions/{DEV}/resourceGroups/rg-dev/providers/Microsoft.Compute/virtualMachines/vm-dev-01",
                "rg-dev",
            )
        return ev

    def resource_changes(self) -> list[dict]:
        t = self.today
        gpu = next(r for r in RESOURCES if r.name == "vm-gpu-train-01")
        web = next(r for r in RESOURCES if r.name == "app-contoso-web")

        def ts(days_ago, hour):
            dt = datetime.combine(t - timedelta(days=days_ago), datetime.min.time(), UTC) + timedelta(
                hours=hour
            )
            return dt.isoformat().replace("+00:00", "Z")

        return [
            {
                "changeTime": ts(6, 9.7),
                "targetResourceId": gpu.id,
                "changeType": "Create",
                "changedBy": "alice@contoso.com",
                "clientType": "Azure Portal",
                "operation": "Microsoft.Compute/virtualMachines/write",
                "subscriptionId": PROD,
                "resourceGroup": "rg-ml",
                "changedProperties": [],
            },
            {
                "changeTime": ts(5, 16.3),
                "targetResourceId": f"{web.id}/providers/microsoft.insights/diagnosticsettings/send-to-la",
                "changeType": "Create",
                "changedBy": "bob@contoso.com",
                "clientType": "Azure CLI",
                "operation": "Microsoft.Insights/diagnosticSettings/write",
                "subscriptionId": PROD,
                "resourceGroup": "rg-web",
                "changedProperties": ["properties.logs"],
            },
            {
                "changeTime": ts(3, 11.1),
                "targetResourceId": f"/subscriptions/{PROD}/resourcegroups/rg-web/providers/"
                "microsoft.storage/storageaccounts/stcontosoweb",
                "changeType": "Update",
                "changedBy": "carol@contoso.com",
                "clientType": "Azure Portal",
                "operation": "Microsoft.Resources/tags/write",
                "subscriptionId": PROD,
                "resourceGroup": "rg-web",
                "changedProperties": ["tags.costcenter"],
            },
        ]

    def advisor(self) -> list[dict]:
        def rec(i, sub, rg, rid, impact, field_, value, problem, solution, monthly, rtype):
            return {
                "id": f"/subscriptions/{sub}/providers/microsoft.advisor/recommendations/rec-{i}",
                "subscriptionId": sub,
                "resourceGroup": rg,
                "resourceId": rid,
                "impact": impact,
                "impactedField": field_,
                "impactedValue": value,
                "problem": problem,
                "solution": solution,
                "savingsAmount": monthly,
                "annualSavingsAmount": round(monthly * 12, 2),
                "savingsCurrency": "USD",
                "recommendationTypeId": rtype,
                "lastUpdated": (self.today - timedelta(days=1)).isoformat() + "T06:00:00Z",
            }

        dev_vm = next(r for r in RESOURCES if r.name == "vm-dev-01")
        disk = next(r for r in RESOURCES if r.name == "disk-old-01")
        sql = next(r for r in RESOURCES if r.name.startswith("sql-contoso"))
        return [
            rec(
                1,
                PROD,
                "",
                f"/subscriptions/{PROD}",
                "High",
                "Microsoft.Subscriptions/subscriptions",
                PROD,
                "Consider virtual machine reserved instance to save over your on-demand costs",
                "Buy a 1-year reservation for D4s v5 in East US",
                1250.0,
                "84b1a508-fc21-49da-979e-96894f1665df",
            ),
            rec(
                2,
                DEV,
                "rg-dev",
                dev_vm.id,
                "Medium",
                "Microsoft.Compute/virtualMachines",
                "vm-dev-01",
                "Right-size or shutdown underutilized virtual machines",
                "Resize vm-dev-01 to B2s",
                140.0,
                "e10b1381-5f0a-47ff-8c7b-37bd13d7c974",
            ),
            rec(
                3,
                PROD,
                "rg-data",
                disk.id,
                "Low",
                "Microsoft.Compute/disks",
                "disk-old-01",
                "You have disks that have not been attached to a VM for more than 30 days",
                "Delete or snapshot unattached disk disk-old-01",
                35.0,
                "48eda464-1485-4dcf-a674-d0905df5054a",
            ),
            rec(
                4,
                PROD,
                "rg-web",
                sql.id,
                "Medium",
                "Microsoft.Sql/servers/databases",
                "db-orders",
                "Right-size underutilized SQL databases",
                "Scale db-orders from 8 to 4 vCores",
                210.0,
                "a8f8e0c6-1f2a-4e4e-9b6c-0b0f3f0f0001",
            ),
        ]

    # -- HTTP -----------------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        path = parts.path.rstrip("/")
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        rid = hashlib.md5(f"{request.method}{request.url}{request.content!r}".encode()).hexdigest()  # noqa: S324
        headers = {"x-ms-request-id": f"sim-{rid[:12]}"}

        m = re.match(r"^/subscriptions/([0-9a-f-]{36})", path, re.I)
        sub = m.group(1).lower() if m else None

        def deny(kind: str, subs: list[str]) -> httpx.Response | None:
            for s in subs:
                if (kind, s) in self.deny:
                    return httpx.Response(
                        403,
                        headers=headers,
                        json={
                            "error": {
                                "code": "AuthorizationFailed",
                                "message": f"The client does not have authorization to perform action ({kind}) over scope "
                                f"'/subscriptions/{s}'.",
                            }
                        },
                    )
            return None

        if path == "/subscriptions" and request.method == "GET":
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "value": [
                        {"subscriptionId": s, "displayName": n, "state": "Enabled", "tenantId": TENANT}
                        for s, n in SUB_NAMES.items()
                    ]
                },
            )
        if sub and path.lower().endswith("/providers/microsoft.costmanagement/query"):
            return deny("cost", [sub]) or self._cost(path, body, q, headers)
        if path.lower() == "/providers/microsoft.resourcegraph/resources":
            subs = [s.lower() for s in body.get("subscriptions", [])]
            return deny("arg", subs) or self._arg(body, subs, headers)
        if sub and path.lower().endswith("/providers/microsoft.insights/eventtypes/management/values"):
            return deny("activity", [sub]) or self._activity(sub, q, headers)
        if sub and path.lower().endswith("/providers/microsoft.authorization/permissions"):
            return httpx.Response(
                200, headers=headers, json={"value": [{"actions": ["*/read"], "notActions": []}]}
            )
        if sub and re.match(r"^/subscriptions/[0-9a-f-]{36}$", path, re.I):
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "subscriptionId": sub,
                    "displayName": SUB_NAMES.get(sub, sub),
                    "state": "Enabled",
                    "tenantId": TENANT,
                },
            )
        return httpx.Response(404, headers=headers, json={"error": {"code": "NotFound", "message": path}})

    def _cost(self, path: str, body: dict, q: dict, headers: dict) -> httpx.Response:
        m = re.match(r"^/subscriptions/([0-9a-f-]{36})(?:/resourceGroups/([^/]+))?/", path, re.I)
        sub, rg = m.group(1).lower(), (m.group(2) or "").lower() or None
        tp = body["timePeriod"]
        start = date.fromisoformat(tp["from"][:10])
        end = date.fromisoformat(tp["to"][:10])
        ds = body["dataset"]
        gran = ds.get("granularity", "None")
        groups = ds.get("grouping") or []
        flt = ds.get("filter")

        def match(r: Res, f: dict | None) -> bool:
            if not f:
                return True
            if "and" in f:
                return all(match(r, x) for x in f["and"])
            if "or" in f:
                return any(match(r, x) for x in f["or"])
            if "dimensions" in f:
                d = f["dimensions"]
                val = str(DIM_ATTR.get(d["name"], lambda _r: "")(r)).lower()
                return val in [v.lower() for v in d["values"]]
            if "tags" in f:
                t = f["tags"]
                return str(r.tags.get(t["name"], "")).lower() in [v.lower() for v in t["values"]]
            return True

        agg: dict[tuple, float] = defaultdict(float)
        d = start
        while d <= end:
            for r in RESOURCES:
                if r.sub != sub or (rg and r.rg != rg) or not match(r, flt):
                    continue
                c = self.cost(r, d)
                if c == 0:
                    continue
                key = []
                if gran == "Daily":
                    key.append(int(d.strftime("%Y%m%d")))
                elif gran == "Monthly":
                    key.append(d.replace(day=1).isoformat() + "T00:00:00")
                for g in groups:
                    if g["type"] == "TagKey":
                        key += [g["name"], r.tags.get(g["name"], "")]
                    else:
                        key.append(DIM_ATTR.get(g["name"], lambda _r: "")(r))
                agg[tuple(key)] += c
            d += timedelta(days=1)

        cols = [{"name": "Cost", "type": "Number"}]
        if gran == "Daily":
            cols.append({"name": "UsageDate", "type": "Number"})
        elif gran == "Monthly":
            cols.append({"name": "BillingMonth", "type": "Datetime"})
        for g in groups:
            if g["type"] == "TagKey":
                cols += [{"name": "TagKey", "type": "String"}, {"name": "TagValue", "type": "String"}]
            else:
                cols.append({"name": g["name"], "type": "String"})
        cols.append({"name": "Currency", "type": "String"})
        rows = [[round(v, 4), *k, "USD"] for k, v in sorted(agg.items(), key=lambda kv: kv[0])]

        offset = int(q.get("$skiptoken", "0"))
        page = rows[offset : offset + self.page_size]
        props = {"columns": cols, "rows": page, "nextLink": None}
        if offset + self.page_size < len(rows):
            props["nextLink"] = (
                f"https://management.azure.com{path}?api-version={q.get('api-version')}&$skiptoken={offset + self.page_size}"
            )
        return httpx.Response(
            200,
            headers=headers,
            json={"id": "sim", "name": "sim", "type": "Microsoft.CostManagement/query", "properties": props},
        )

    def _arg(self, body: dict, subs: list[str], headers: dict) -> httpx.Response:
        query = body.get("query", "")
        ql = query.lower()
        if "advisorresources" in ql:
            data = [r for r in self.advisor() if r["subscriptionId"] in subs]
        elif "resourcechanges" in ql:
            data = [r for r in self.resource_changes() if r["subscriptionId"] in subs]
            # honor the time window and resource group filters that changes.py puts in the KQL
            between = re.search(r"between \(datetime\(([^)]+)\) \.\. datetime\(([^)]+)\)\)", query)
            if between:
                lo, hi = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in between.groups())
                data = [
                    r
                    for r in data
                    if lo <= datetime.fromisoformat(r["changeTime"].replace("Z", "+00:00")) <= hi
                ]
            rg = re.search(r"resourceGroup =~ '([^']+)'", query)
            if rg:
                data = [r for r in data if r["resourceGroup"].lower() == rg.group(1).lower()]
        elif "resourcecontainers" in ql:
            rgs = sorted({(r.sub, r.rg) for r in RESOURCES if r.sub in subs})
            data = [
                {"subscriptionId": s, "resourceGroup": g, "location": "eastus", "tags": {}} for s, g in rgs
            ]
        else:
            data = [
                {
                    "id": r.id,
                    "name": r.name.split("/")[-1],
                    "type": r.rtype.lower(),
                    "subscriptionId": r.sub,
                    "resourceGroup": r.rg,
                    "location": r.location,
                    "tags": r.tags,
                }
                for r in RESOURCES
                if r.sub in subs
            ]
        if "take 1" in ql or "limit 1" in ql:
            data = data[:1]
        return httpx.Response(
            200,
            headers=headers,
            json={
                "totalRecords": len(data),
                "count": len(data),
                "data": data,
                "resultTruncated": "false",
                "facets": [],
            },
        )

    def _activity(self, sub: str, q: dict, headers: dict) -> httpx.Response:
        flt = q.get("$filter", "")
        ge = re.search(r"eventTimestamp ge '([^']+)'", flt)
        le = re.search(r"eventTimestamp le '([^']+)'", flt)
        rg = re.search(r"resourceGroupName eq '([^']+)'", flt)
        events = self.activity_events(sub)

        def ts(s):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        if ge:
            events = [e for e in events if ts(e["eventTimestamp"]) >= ts(ge.group(1))]
        if le:
            events = [e for e in events if ts(e["eventTimestamp"]) <= ts(le.group(1))]
        if rg:
            events = [e for e in events if e["resourceGroupName"].lower() == rg.group(1).lower()]
        return httpx.Response(200, headers=headers, json={"value": events})
