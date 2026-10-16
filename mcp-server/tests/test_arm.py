"""ArmClient: read-only allowlist, scope guard, retries, errors, caching, paging, audit."""

from __future__ import annotations

import json
import os
from collections import namedtuple
from urllib.parse import parse_qs

import httpx
import pytest

from azure_finops import arm as armmod
from azure_finops.arm import ALLOWED, COST_API_VERSIONS, ArmClient, classify
from azure_finops.audit import AuditLog
from azure_finops.errors import AzureApiError, PermissionDenied, ReadOnlyViolation, ScopeError
from azure_finops.simulator.synthetic import DEV, PROD, SyntheticAzure

OTHER = "33333333-3333-3333-3333-333333333333"
SUB = f"/subscriptions/{PROD}"
COST_PATH = f"{SUB}/providers/Microsoft.CostManagement/query"
ACTIVITY_PATH = f"{SUB}/providers/Microsoft.Insights/eventtypes/management/values"
ARG_PATH = "/providers/Microsoft.ResourceGraph/resources"


class Handler:
    """MockTransport handler that replays queued responses (or callables) and records every request."""

    def __init__(self, *responses, default=None):
        self.queue = list(responses)
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else self.default
        if item is None:
            return httpx.Response(200, json={"value": []}, headers={"x-ms-request-id": "req-ok"})
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item


class CountingLimiter:
    def __init__(self):
        self.calls: list[int] = []

    def acquire(self, qpu: int = 1) -> float:
        self.calls.append(qpu)
        return 0.0


def make_client(handler, **kw) -> ArmClient:
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    kw.setdefault("limiter", CountingLimiter())
    kw.setdefault("cache_ttl", 0)
    client = ArmClient([PROD, DEV], transport=httpx.MockTransport(handler), **kw)
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def err(status: int, code: str = "Boom", message: str = "it broke", rid: str = "req-err", headers=None):
    return httpx.Response(
        status,
        headers={"x-ms-request-id": rid, **(headers or {})},
        json={"error": {"code": code, "message": message}},
    )


# -- read-only allowlist -----------------------------------------------------------------------

BLOCKED = [
    ("PUT", f"{SUB}/resourceGroups/rg"),
    ("PATCH", f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"),
    ("DELETE", f"{SUB}/resourceGroups/rg"),
    ("DELETE", f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"),
    ("PUT", COST_PATH),
    ("PATCH", "/subscriptions"),
    ("DELETE", f"{SUB}/providers/Microsoft.Advisor/recommendations"),
    ("PUT", ARG_PATH),
    ("POST", f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm/start"),
    ("POST", f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm/deallocate"),
    ("POST", f"{SUB}/providers/Microsoft.Advisor/generateRecommendation"),
    ("POST", f"{SUB}/providers/Microsoft.Advisor/recommendations"),
    ("POST", f"{SUB}/providers/Microsoft.Advisor/recommendations/rec-1/suppressions/s1"),
    ("GET", f"{SUB}/resourceGroups/rg/providers/Microsoft.Advisor/recommendations"),
    ("GET", f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"),
    ("GET", f"{SUB}/resourceGroups"),
    ("GET", f"{SUB}/providers/Microsoft.KeyVault/vaults"),
    ("GET", f"{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st/listKeys"),
    ("POST", f"{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st/listKeys"),
    ("GET", COST_PATH),
    ("POST", f"{SUB}/providers/Microsoft.CostManagement/exports/e1/run"),
    ("PUT", f"{SUB}/providers/Microsoft.CostManagement/exports/e1"),
    ("GET", f"{SUB}/providers/Microsoft.CostManagement/exports"),
    ("POST", f"{SUB}/providers/Microsoft.CostManagement/query/extra"),
    ("POST", f"{COST_PATH}?api-version=2023-11-01"),
    ("POST", f"{SUB}/resourceGroups/rg/nested/providers/Microsoft.CostManagement/query"),
    ("POST", "/providers/Microsoft.Management/managementGroups/mg1/providers/Microsoft.CostManagement/query"),
    ("GET", "/providers/Microsoft.Management/managementGroups/mg1"),
    ("POST", "/providers/Microsoft.Billing/billingAccounts/123/providers/Microsoft.CostManagement/query"),
    ("POST", "/subscriptions"),
    ("HEAD", "/subscriptions"),
    ("OPTIONS", "/subscriptions"),
    ("GET", "/subscriptions/not-a-guid"),
    ("GET", f"{SUB}/providers/Microsoft.Insights/eventtypes/management"),
    ("GET", "http://management.azure.com/subscriptions"),
    ("GET", "https://evil.example.com/subscriptions"),
    ("GET", f"https://evil.example.com{ACTIVITY_PATH}"),
    ("POST", f"https://evil.example.com{COST_PATH}"),
    ("GET", "https://management.azure.com.evil.example.com/subscriptions"),
    ("GET", "https://management.azure.com@evil.example.com/subscriptions"),
    ("GET", "https://management.azure.com:8443/subscriptions"),
    ("GET", "https://graph.microsoft.com/v1.0/me"),
]


@pytest.mark.parametrize(("method", "path"), BLOCKED)
def test_non_allowlisted_requests_never_reach_transport(method, path):
    h = Handler()
    c = make_client(h)
    with pytest.raises(ReadOnlyViolation) as ei:
        c.request(method, path, params={"api-version": "2023-11-01"}, body={"subscriptions": [PROD]})
    assert h.requests == []
    assert c.request_count == 0
    assert ei.value.to_dict()["status"] == "blocked"


ACCEPTED = [
    ("GET", "/subscriptions", None, "subscriptions"),
    ("GET", "/subscriptions/", None, "subscriptions"),
    ("GET", SUB, None, "subscriptions"),
    ("POST", COST_PATH, {"type": "ActualCost"}, "cost"),
    ("POST", COST_PATH.lower(), {"type": "ActualCost"}, "cost"),
    ("POST", f"{SUB}/resourceGroups/rg-ml/providers/Microsoft.CostManagement/query", {}, "cost"),
    ("POST", f"{SUB}/providers/Microsoft.CostManagement/forecast", {}, "cost"),
    ("POST", f"{SUB}/resourceGroups/rg-ml/providers/Microsoft.CostManagement/forecast", {}, "cost"),
    ("POST", ARG_PATH, {"subscriptions": [PROD], "query": "resources"}, "arg"),
    ("GET", ACTIVITY_PATH, None, "activity"),
    ("GET", f"{SUB}/providers/Microsoft.Advisor/recommendations", None, "advisor"),
    ("GET", f"{SUB}/providers/Microsoft.Authorization/permissions", None, "authz"),
    ("GET", f"{SUB}/resourceGroups/rg-ml/providers/Microsoft.Authorization/permissions", None, "authz"),
    ("GET", f"https://management.azure.com{ACTIVITY_PATH}?api-version=2015-04-01", None, "activity"),
]


@pytest.mark.parametrize(("method", "path", "body", "kind"), ACCEPTED)
def test_allowlisted_requests_are_sent(method, path, body, kind):
    h = Handler()
    c = make_client(h)
    resp = c.request(method, path, params={"api-version": "x"}, body=body)
    assert resp.status == 200
    assert len(h.requests) == 1
    sent = h.requests[0]
    assert sent.method == method
    assert sent.url.scheme == "https" and sent.url.host == "management.azure.com"
    if method == "GET":
        assert sent.content == b""
    else:
        assert json.loads(sent.content) == body
    if not path.startswith("https://"):
        assert classify(method, path.rstrip("/") or "/") == kind


def test_every_allowlist_entry_is_exercised():
    for method, rx, kind in ALLOWED:
        assert any(m == method and k == kind and rx.match(p.rstrip("/")) for m, p, _b, k in ACCEPTED), (
            f"no accepted example for {method} {rx.pattern}"
        )
    assert {k for _m, _r, k in ALLOWED} == {k for *_x, k in ACCEPTED}


def test_only_cost_requests_consume_qpu():
    h = Handler()
    lim = CountingLimiter()
    c = make_client(h, limiter=lim)
    c.get("/subscriptions", {"api-version": "x"})
    c.get(ACTIVITY_PATH, {"api-version": "x"})
    c.post(ARG_PATH, {"subscriptions": [PROD], "query": "q"})
    assert lim.calls == []
    c.post(COST_PATH, {"a": 1}, qpu=3)
    assert lim.calls == [3]


def test_bearer_token_is_sent_and_cached():
    Tok = namedtuple("Tok", "token expires_on")

    class Cred:
        calls = 0

        def get_token(self, scope):
            assert scope == "https://management.azure.com/.default"
            Cred.calls += 1
            return Tok("secret-token-value", 4_102_444_800)  # year 2100

    h = Handler()
    audit = AuditLog()
    c = ArmClient(
        [PROD],
        credential=Cred(),
        transport=httpx.MockTransport(h),
        cache_ttl=0,
        audit=audit,
        sleep=lambda s: None,
    )
    c.get("/subscriptions")
    c.get(SUB)
    assert [r.headers["Authorization"] for r in h.requests] == ["Bearer secret-token-value"] * 2
    assert Cred.calls == 1
    assert "secret-token-value" not in json.dumps(audit.records)


# -- scope guard -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", f"/subscriptions/{OTHER}", None),
        ("POST", f"/subscriptions/{OTHER}/providers/Microsoft.CostManagement/query", {}),
        ("POST", f"/subscriptions/{OTHER}/resourceGroups/rg/providers/Microsoft.CostManagement/query", {}),
        ("GET", f"/subscriptions/{OTHER}/providers/Microsoft.Advisor/recommendations", None),
        ("GET", f"/subscriptions/{OTHER}/providers/Microsoft.Insights/eventtypes/management/values", None),
        ("POST", ARG_PATH, {"subscriptions": [PROD, OTHER], "query": "resources"}),
        ("POST", ARG_PATH, {"subscriptions": [], "query": "resources"}),
        ("POST", ARG_PATH, {"query": "resources"}),
        ("POST", ARG_PATH, {"subscriptions": [PROD], "managementGroups": ["mg1"], "query": "resources"}),
        ("POST", ARG_PATH, {"managementGroups": ["mg1"], "query": "resources"}),
    ],
)
def test_scope_guard_blocks_unconfigured_scopes(method, path, body):
    h = Handler()
    c = make_client(h)
    with pytest.raises(ScopeError) as ei:
        c.request(method, path, params={"api-version": "x"}, body=body)
    assert h.requests == []
    assert ei.value.to_dict()["status"] == "out_of_scope"


def test_scope_error_names_subscription_and_remediation():
    c = make_client(Handler())
    with pytest.raises(ScopeError) as ei:
        c.get(f"/subscriptions/{OTHER}")
    assert OTHER in ei.value.message
    assert "finops/config.yaml" in ei.value.remediation
    with pytest.raises(ScopeError) as ei:
        c.post(ARG_PATH, {"subscriptions": [PROD, OTHER], "query": "q"})
    assert OTHER in ei.value.message and PROD not in ei.value.message


def test_scope_guard_is_case_insensitive_for_configured_subscriptions():
    h = Handler()
    c = make_client(h)
    c.get(f"/subscriptions/{PROD.upper()}")
    c.post(ARG_PATH, {"subscriptions": [DEV.upper()], "query": "q"})
    assert len(h.requests) == 2


# -- retries and errors ------------------------------------------------------------------------


def test_429_retry_after_header_is_honoured_then_succeeds():
    h = Handler(err(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": True}))
    audit = AuditLog()
    c = make_client(h, audit=audit)
    resp = c.get(SUB)
    assert resp.body == {"ok": True}
    assert c.sleeps == [7.0]
    assert len(h.requests) == 2
    assert [r["status"] for r in audit.records] == [429, 200]
    assert [r["attempt"] for r in audit.records] == [0, 1]


def test_cost_management_retry_headers_use_the_longest_wait():
    h = Handler(
        err(
            429,
            headers={
                "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "12",
                "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "30",
                "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after": "5",
            },
        ),
        err(429, headers={"x-ms-ratelimit-microsoft.costmanagement-client-retry-after": "500"}),
        httpx.Response(200, json={"properties": {"rows": []}}),
    )
    lim = CountingLimiter()
    c = make_client(h, limiter=lim)
    c.post(COST_PATH, {"q": 1}, {"api-version": "x"}, qpu=2)
    assert c.sleeps == [30.0, 120.0]  # the second wait is capped at 120 s
    assert lim.calls == [2, 2, 2]  # every attempt is charged against the QPU budget


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_retried_with_exponential_backoff(status):
    h = Handler(err(status), err(status), httpx.Response(200, json={"v": 1}))
    c = make_client(h)
    assert c.get(SUB).body == {"v": 1}
    assert len(c.sleeps) == 2
    assert 2 <= c.sleeps[0] < 3 and 4 <= c.sleeps[1] < 5


def test_gives_up_after_max_retries():
    h = Handler(default=lambda _r: err(429, rid="req-final", headers={"Retry-After": "1"}))
    c = make_client(h, max_retries=2)
    with pytest.raises(AzureApiError) as ei:
        c.get(SUB)
    assert len(h.requests) == 3
    assert c.sleeps == [1.0, 1.0]
    assert ei.value.status == 429
    assert ei.value.request_id == "req-final"
    assert ei.value.to_dict()["http_status"] == 429


def test_network_errors_are_retried_then_reported():
    h = Handler(httpx.ConnectError("boom"), httpx.Response(200, json={"v": 1}))
    c = make_client(h)
    assert c.get(SUB).body == {"v": 1}
    assert len(c.sleeps) == 1

    h2 = Handler(default=httpx.ReadTimeout("slow"))
    c2 = make_client(h2, max_retries=1)
    with pytest.raises(AzureApiError, match="network error"):
        c2.get(SUB)
    assert len(h2.requests) == 2


@pytest.mark.parametrize(
    ("status", "method", "path", "body", "role", "scope"),
    [
        (
            403,
            "POST",
            f"{SUB}/resourceGroups/rg-ml/providers/Microsoft.CostManagement/query",
            {},
            "Cost Management Reader",
            f"{SUB}/resourceGroups/rg-ml",
        ),
        (401, "GET", ACTIVITY_PATH, None, "Monitoring Reader", SUB),
        (
            403,
            "POST",
            ARG_PATH,
            {"subscriptions": [PROD, DEV], "query": "q"},
            "Reader",
            f"{SUB}, /subscriptions/{DEV}",
        ),
        (403, "GET", f"{SUB}/providers/Microsoft.Advisor/recommendations", None, "Advisor", SUB),
    ],
)
def test_auth_failures_become_permission_denied_with_remediation(status, method, path, body, role, scope):
    h = Handler(err(status, code="AuthorizationFailed", message="no access"))
    c = make_client(h)
    with pytest.raises(PermissionDenied) as ei:
        c.request(method, path, body=body)
    e = ei.value
    assert len(h.requests) == 1  # never retried
    assert c.sleeps == []
    assert "AuthorizationFailed" in e.message and str(status) in e.message
    assert role in e.remediation and scope in e.remediation
    assert e.missing.endswith(f" on {scope}")
    d = e.to_dict()
    assert d["status"] == "unavailable" and d["missing"] == e.missing and d["remediation"] == e.remediation


def test_scope_label_overrides_remediation_scope():
    c = make_client(Handler(err(403)))
    with pytest.raises(PermissionDenied) as ei:
        c.post(COST_PATH, {}, scope_label="my-label")
    assert "my-label" in ei.value.remediation


@pytest.mark.parametrize("status", [400, 404, 409])
def test_other_4xx_raise_azure_api_error_with_request_id(status):
    h = Handler(err(status, code="BadRequest", message="bad grouping", rid="req-42"))
    c = make_client(h)
    with pytest.raises(AzureApiError) as ei:
        c.post(COST_PATH, {"q": 1})
    e = ei.value
    assert len(h.requests) == 1
    assert e.status == status and e.request_id == "req-42"
    assert "BadRequest: bad grouping" in e.message
    assert e.to_dict() == {
        "status": "azure_error",
        "error": e.message,
        "http_status": status,
        "request_id": "req-42",
    }


def test_error_message_falls_back_to_text_and_correlation_id():
    h = Handler(httpx.Response(404, text="plain failure", headers={"x-ms-correlation-request-id": "corr-1"}))
    c = make_client(h)
    with pytest.raises(AzureApiError) as ei:
        c.get(SUB)
    assert "plain failure" in ei.value.message
    assert ei.value.request_id == "corr-1"


# -- cache -------------------------------------------------------------------------------------


def test_memory_cache_serves_repeat_requests_within_ttl(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(armmod.time, "time", lambda: now[0])
    h = Handler(
        default=lambda r: httpx.Response(200, json={"n": len(h.requests)}, headers={"x-ms-request-id": "r1"})
    )
    audit = AuditLog()
    c = make_client(h, cache_ttl=60, audit=audit)
    first = c.post(COST_PATH, {"q": 1}, {"api-version": "x"})
    second = c.post(COST_PATH, {"q": 1}, {"api-version": "x"})
    assert len(h.requests) == 1
    assert first.from_cache is False and second.from_cache is True
    assert second.body == first.body and second.request_id == "r1"
    assert audit.records[-1]["cache"] == "hit"

    c.post(COST_PATH, {"q": 2}, {"api-version": "x"})  # different body
    c.post(COST_PATH, {"q": 1}, {"api-version": "y"})  # different params
    assert len(h.requests) == 3

    now[0] += 61  # expired
    c.post(COST_PATH, {"q": 1}, {"api-version": "x"})
    assert len(h.requests) == 4


def test_cache_key_ignores_body_key_order():
    h = Handler()
    c = make_client(h, cache_ttl=60)
    c.post(COST_PATH, {"a": 1, "b": 2})
    c.post(COST_PATH, {"b": 2, "a": 1})
    assert len(h.requests) == 1


def test_zero_ttl_disables_cache():
    h = Handler()
    c = make_client(h, cache_ttl=0)
    c.get(SUB)
    c.get(SUB)
    assert len(h.requests) == 2


def test_errors_are_not_cached():
    h = Handler(err(404), httpx.Response(200, json={"ok": 1}))
    c = make_client(h, cache_ttl=60)
    with pytest.raises(AzureApiError):
        c.get(SUB)
    assert c.get(SUB).body == {"ok": 1}
    assert len(h.requests) == 2


def test_disk_cache_is_shared_across_clients(tmp_path):
    h = Handler(default=lambda r: httpx.Response(200, json={"v": 1}, headers={"x-ms-request-id": "disk-1"}))
    c1 = make_client(h, cache_ttl=300, cache_dir=tmp_path / "cache")
    c1.get(SUB, {"api-version": "x"})
    files = list((tmp_path / "cache").glob("*.json"))
    assert len(files) == 1

    c2 = make_client(h, cache_ttl=300, cache_dir=tmp_path / "cache")
    resp = c2.get(SUB, {"api-version": "x"})
    assert len(h.requests) == 1
    assert resp.from_cache and resp.body == {"v": 1} and resp.request_id == "disk-1"

    old = files[0].stat().st_mtime - 3600
    os.utime(files[0], (old, old))
    c3 = make_client(h, cache_ttl=300, cache_dir=tmp_path / "cache")
    assert c3.get(SUB, {"api-version": "x"}).from_cache is False
    assert len(h.requests) == 2


# -- paging ------------------------------------------------------------------------------------


def test_get_paged_follows_next_link():
    nxt = f"https://management.azure.com{ACTIVITY_PATH}?api-version=2015-04-01&$skipToken=abc"
    h = Handler(
        httpx.Response(200, json={"value": [1], "nextLink": nxt}),
        httpx.Response(200, json={"value": [2], "nextLink": nxt + "2"}),
        httpx.Response(200, json={"value": [3]}),
    )
    c = make_client(h)
    pages = list(c.get_paged(ACTIVITY_PATH, {"api-version": "2015-04-01"}))
    assert [p.body["value"] for p in pages] == [[1], [2], [3]]
    q = parse_qs(h.requests[1].url.query.decode())
    assert q == {"api-version": ["2015-04-01"], "$skipToken": ["abc"]}


def test_get_paged_respects_max_pages():
    nxt = f"https://management.azure.com{ACTIVITY_PATH}?api-version=1&$skipToken=again"
    h = Handler(default=lambda r: httpx.Response(200, json={"value": [], "nextLink": nxt}))
    c = make_client(h)
    assert len(list(c.get_paged(ACTIVITY_PATH, max_pages=4))) == 4
    assert len(h.requests) == 4


@pytest.mark.parametrize(
    ("next_link", "exc"),
    [
        ("https://evil.example.com/subscriptions?page=2", ReadOnlyViolation),
        (f"http://management.azure.com{ACTIVITY_PATH}?page=2", ReadOnlyViolation),
        (
            f"https://management.azure.com{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
            ReadOnlyViolation,
        ),
        (
            f"https://management.azure.com/subscriptions/{OTHER}/providers/Microsoft.Insights/eventtypes/management/values",
            ScopeError,
        ),
    ],
)
def test_get_paged_refuses_bad_next_links(next_link, exc):
    h = Handler(httpx.Response(200, json={"value": [1], "nextLink": next_link}))
    c = make_client(h)
    pages = c.get_paged(ACTIVITY_PATH)
    assert next(pages).body["value"] == [1]
    with pytest.raises(exc):
        next(pages)
    assert len(h.requests) == 1


def test_cost_query_follows_skiptoken_pages_with_simulator():
    sim = SyntheticAzure(page_size=4)
    lim = CountingLimiter()
    c = ArmClient(
        [PROD, DEV], transport=httpx.MockTransport(sim.handle), limiter=lim, cache_ttl=0, sleep=lambda s: None
    )
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": "2026-03-01T00:00:00Z", "to": "2026-03-10T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        },
    }
    pages = c.cost_query(SUB, body, qpu=1)
    rows = [r for p in pages for r in p.body["properties"]["rows"]]
    assert len(rows) == 10  # one row per day
    assert len(pages) == 3 and [len(p.body["properties"]["rows"]) for p in pages] == [4, 4, 2]
    assert [m for m, _p in sim.calls] == ["POST"] * 3
    assert lim.calls == [1, 1, 1]
    assert len({p.request_id for p in pages}) == 3


def test_cost_query_refuses_foreign_next_link():
    h = Handler(
        httpx.Response(200, json={"properties": {"rows": [[1]], "nextLink": "https://evil.example.com/x"}})
    )
    c = make_client(h)
    with pytest.raises(ReadOnlyViolation):
        c.cost_query(SUB, {"q": 1})
    assert len(h.requests) == 1


def test_cost_query_respects_max_pages():
    nxt = f"https://management.azure.com{COST_PATH}?api-version=x&$skiptoken=1"
    h = Handler(default=lambda r: httpx.Response(200, json={"properties": {"rows": [], "nextLink": nxt}}))
    c = make_client(h)
    assert len(c.cost_query(SUB, {"q": 1}, max_pages=3)) == 3


# -- api-version fallback ----------------------------------------------------------------------


def _version(req: httpx.Request) -> str:
    return parse_qs(req.url.query.decode())["api-version"][0]


def test_cost_query_falls_back_to_older_api_version():
    assert COST_API_VERSIONS[-1] == "2023-11-01"

    def handle(req):
        if _version(req) != "2023-11-01":
            return err(
                400,
                code="InvalidApiVersionParameter",
                message=f"The api-version '{_version(req)}' is invalid.",
            )
        return httpx.Response(200, json={"properties": {"rows": [[1.0]]}})

    h = Handler(default=handle)
    c = make_client(h)
    pages = c.cost_query(SUB, {"q": 1})
    assert pages[0].body["properties"]["rows"] == [[1.0]]
    assert [_version(r) for r in h.requests] == list(dict.fromkeys(COST_API_VERSIONS))


def test_cost_query_does_not_fall_back_on_other_400s():
    h = Handler(default=lambda r: err(400, code="BadRequest", message="invalid grouping dimension"))
    c = make_client(h)
    with pytest.raises(AzureApiError, match="invalid grouping"):
        c.cost_query(SUB, {"q": 1})
    assert len(h.requests) == 1


def test_cost_query_raises_when_no_api_version_is_supported():
    h = Handler(
        default=lambda r: err(400, code="NoRegisteredProviderFound", message="supported api-versions are X")
    )
    c = make_client(h)
    with pytest.raises(AzureApiError) as ei:
        c.cost_query(SUB, {"q": 1})
    assert ei.value.status == 400
    assert len(h.requests) == len(dict.fromkeys(COST_API_VERSIONS))


# -- resource graph ----------------------------------------------------------------------------


def test_resource_graph_pages_with_skip_token_and_table_format():
    h = Handler(
        httpx.Response(
            200, json={"data": [{"a": 1}], "$skipToken": "tok"}, headers={"x-ms-request-id": "g1"}
        ),
        httpx.Response(
            200,
            json={"data": {"columns": [{"name": "a"}], "rows": [[2], [3]]}},
            headers={"x-ms-request-id": "g2"},
        ),
    )
    c = make_client(h)
    rows, rids = c.resource_graph("resources", [PROD])
    assert rows == [{"a": 1}, {"a": 2}, {"a": 3}]
    assert rids == ["g1", "g2"]
    second = json.loads(h.requests[1].content)
    assert second["options"]["$skipToken"] == "tok"
    assert second["subscriptions"] == [PROD]


def test_resource_graph_stops_at_max_rows():
    h = Handler(default=lambda r: httpx.Response(200, json={"data": [{"a": 1}] * 3, "$skipToken": "more"}))
    c = make_client(h)
    rows, _ = c.resource_graph("resources", [PROD], max_rows=5)
    assert len(h.requests) == 2 and len(rows) >= 5
    assert json.loads(h.requests[0].content)["options"]["$top"] == 5
    assert json.loads(h.requests[1].content)["options"]["$top"] == 2


# -- audit -------------------------------------------------------------------------------------


def test_every_request_attempt_is_audited(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    h = Handler(err(503, rid="a1"), httpx.Response(200, json={}, headers={"x-ms-request-id": "a2"}))
    c = make_client(h, audit=audit, cache_ttl=60)
    c.get(SUB, {"api-version": "x"})
    c.get(SUB, {"api-version": "x"})  # cache hit
    lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [ln["event"] for ln in lines] == ["arm_request"] * 3
    assert [(ln.get("status"), ln["request_id"]) for ln in lines] == [(503, "a1"), (200, "a2"), (None, "a2")]
    assert lines[2]["cache"] == "hit"
    for ln in lines:
        assert ln["method"] == "GET" and ln["path"] == SUB and "ts" in ln
    assert all(isinstance(ln["ms"], int) for ln in lines[:2])


def test_blocked_requests_are_not_audited_as_sent():
    audit = AuditLog()
    c = make_client(Handler(), audit=audit)
    with pytest.raises(ReadOnlyViolation):
        c.request("DELETE", f"{SUB}/resourceGroups/rg")
    assert audit.records == []
