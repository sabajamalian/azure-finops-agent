"""Read-only Azure Resource Manager client.

Every request is checked against a method + path allowlist and against the configured subscriptions
*before* it is sent. Write methods and unknown endpoints raise ReadOnlyViolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from .audit import AuditLog
from .errors import AzureApiError, PermissionDenied, ReadOnlyViolation, ScopeError
from .ratelimit import QpuLimiter

ARM_HOST = "management.azure.com"
ARM = f"https://{ARM_HOST}"
ARM_SCOPE = "https://management.azure.com/.default"

COST_API_VERSIONS = [os.environ.get("FINOPS_COSTMGMT_API_VERSION", "2026-06-01"), "2023-11-01"]
ARG_API_VERSION = "2024-04-01"
ACTIVITY_API_VERSION = "2015-04-01"
SUBSCRIPTIONS_API_VERSION = "2022-12-01"
AUTHZ_API_VERSION = "2022-04-01"

_SUB = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_RG = r"/resourceGroups/[^/]+"

# (method, path regex, kind). kind drives rate limiting and permission remediation text.
ALLOWED: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/subscriptions$"), "subscriptions"),
    ("GET", re.compile(rf"^/subscriptions/{_SUB}$"), "subscriptions"),
    (
        "POST",
        re.compile(rf"^/subscriptions/{_SUB}(?:{_RG})?/providers/Microsoft\.CostManagement/query$", re.I),
        "cost",
    ),
    (
        "POST",
        re.compile(rf"^/subscriptions/{_SUB}(?:{_RG})?/providers/Microsoft\.CostManagement/forecast$", re.I),
        "cost",
    ),
    ("POST", re.compile(r"^/providers/Microsoft\.ResourceGraph/resources$", re.I), "arg"),
    (
        "GET",
        re.compile(
            rf"^/subscriptions/{_SUB}/providers/Microsoft\.Insights/eventtypes/management/values$", re.I
        ),
        "activity",
    ),
    (
        "GET",
        re.compile(rf"^/subscriptions/{_SUB}/providers/Microsoft\.Advisor/recommendations$", re.I),
        "advisor",
    ),
    (
        "GET",
        re.compile(
            rf"^/subscriptions/{_SUB}(?:{_RG})?/providers/Microsoft\.Authorization/permissions$", re.I
        ),
        "authz",
    ),
]

REMEDIATION = {
    "cost": (
        "Cost Management Reader (or Reader) on {scope}. For EA, the enrollment needs 'AO view charges' "
        "enabled; for MCA, the billing profile needs 'Azure charges' visibility for subscription owners."
    ),
    "arg": "Reader on {scope} so Azure Resource Graph can return its resources.",
    "activity": "Reader (or Monitoring Reader) on {scope} to read the Activity Log.",
    "advisor": "Reader on {scope} to read Azure Advisor recommendations.",
    "subscriptions": "Reader on {scope}.",
    "authz": "Reader on {scope}.",
}

_RETRY_HEADER_RE = re.compile(r"retry-after$", re.I)


@dataclass
class ArmResponse:
    status: int
    body: dict
    request_id: str | None
    headers: dict[str, str]
    from_cache: bool = False


def classify(method: str, path: str) -> str:
    for m, rx, kind in ALLOWED:
        if m == method.upper() and rx.match(path):
            return kind
    raise ReadOnlyViolation(
        f"{method.upper()} {path} is not on the read-only allowlist; the FinOps server never changes Azure resources.",
    )


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    vals = []
    for k, v in headers.items():
        if _RETRY_HEADER_RE.search(k):
            try:
                vals.append(float(v))
            except ValueError:
                continue
    return max(vals) if vals else None


class _TokenProvider:
    def __init__(self, credential):
        self.credential = credential
        self._token: str | None = None
        self._expires = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._expires - 300:
            return self._token
        tok = self.credential.get_token(ARM_SCOPE)
        self._token, self._expires = tok.token, float(tok.expires_on)
        return self._token


class ArmClient:
    def __init__(
        self,
        allowed_subscriptions: list[str],
        credential=None,
        transport: httpx.BaseTransport | None = None,
        limiter: QpuLimiter | None = None,
        audit: AuditLog | None = None,
        cache_dir: Path | None = None,
        cache_ttl: float = 900.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ):
        self.allowed = {s.lower() for s in allowed_subscriptions}
        self._tokens = _TokenProvider(credential) if credential is not None else None
        self.http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)
        self.limiter = limiter or QpuLimiter(sleep=sleep)
        self.audit = audit or AuditLog()
        self.cache_dir = cache_dir
        self.cache_ttl = cache_ttl
        self._mem: dict[str, tuple[float, ArmResponse]] = {}
        self.max_retries = max_retries
        self.sleep = sleep
        self.request_count = 0

    # -- guards ---------------------------------------------------------------------------------
    def check_scope(self, path: str, body: dict | None) -> None:
        m = re.match(rf"^/subscriptions/({_SUB})", path)
        if m and path != "/subscriptions" and m.group(1).lower() not in self.allowed:
            raise ScopeError(
                f"subscription {m.group(1)} is not configured in finops/config.yaml",
                "Add it to azure.subscriptions in finops/config.yaml (through a reviewed PR) if it should be in scope.",
            )
        if path.lower() == "/providers/microsoft.resourcegraph/resources":
            subs = (body or {}).get("subscriptions") or []
            if not subs:
                raise ScopeError("Resource Graph queries must name explicit subscriptions")
            bad = [s for s in subs if s.lower() not in self.allowed]
            if bad:
                raise ScopeError(f"subscriptions not configured: {', '.join(bad)}")
            if (body or {}).get("managementGroups"):
                raise ScopeError("management group scope is not supported in this version")

    # -- cache ----------------------------------------------------------------------------------
    def _key(self, method: str, url: str, body: dict | None) -> str:
        return hashlib.sha256(f"{method} {url} {json.dumps(body, sort_keys=True)}".encode()).hexdigest()

    def _cache_get(self, key: str) -> ArmResponse | None:
        if self.cache_ttl <= 0:
            return None
        now = time.time()
        hit = self._mem.get(key)
        if hit and now - hit[0] < self.cache_ttl:
            return ArmResponse(**{**hit[1].__dict__, "from_cache": True})
        if self.cache_dir:
            p = self.cache_dir / f"{key}.json"
            if p.exists() and now - p.stat().st_mtime < self.cache_ttl:
                d = json.loads(p.read_text(encoding="utf-8"))
                resp = ArmResponse(**d)
                self._mem[key] = (p.stat().st_mtime, resp)
                return ArmResponse(**{**d, "from_cache": True})
        return None

    def _cache_put(self, key: str, resp: ArmResponse) -> None:
        if self.cache_ttl <= 0:
            return
        self._mem[key] = (time.time(), resp)
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{key}.json").write_text(json.dumps(resp.__dict__), encoding="utf-8")

    # -- requests -------------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path_or_url: str,
        params: dict | None = None,
        body: dict | None = None,
        qpu: int = 1,
        scope_label: str | None = None,
    ) -> ArmResponse:
        method = method.upper()
        if path_or_url.startswith("https://") or path_or_url.startswith("http://"):
            parts = urlsplit(path_or_url)
            if parts.scheme != "https" or parts.netloc.lower() != ARM_HOST:
                raise ReadOnlyViolation(f"refusing to call non-ARM URL {parts.scheme}://{parts.netloc}")
            path = parts.path
            params = {**dict(parse_qsl(parts.query)), **(params or {})}
        else:
            path = path_or_url
        path = path.rstrip("/") or "/"
        kind = classify(method, path)
        self.check_scope(path, body)
        url = f"{ARM}{path}?{urlencode(params or {})}"
        key = self._key(method, url, body)
        cached = self._cache_get(key)
        if cached:
            self.audit.write(
                "arm_request", method=method, path=path, cache="hit", request_id=cached.request_id
            )
            return cached

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._tokens:
            headers["Authorization"] = f"Bearer {self._tokens.get()}"
        attempt = 0
        while True:
            if kind == "cost":
                self.limiter.acquire(qpu)
            self.request_count += 1
            started = time.monotonic()
            try:
                r = self.http.request(method, url, headers=headers, json=body if method == "POST" else None)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self.sleep(min(30.0, 2**attempt + random.random()))
                    continue
                raise AzureApiError(f"network error calling {path}: {exc}") from exc
            rid = r.headers.get("x-ms-request-id") or r.headers.get("x-ms-correlation-request-id")
            self.audit.write(
                "arm_request",
                method=method,
                path=path,
                status=r.status_code,
                request_id=rid,
                ms=int((time.monotonic() - started) * 1000),
                attempt=attempt,
            )
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                attempt += 1
                wait = _retry_after_seconds(r.headers)
                self.sleep(min(120.0, wait if wait is not None else 2**attempt + random.random()))
                continue
            if r.status_code in (401, 403):
                scope = scope_label or _scope_from_path(path, body)
                raise PermissionDenied(
                    f"Azure denied {method} {path} ({r.status_code}): {_error_message(r)}",
                    missing=REMEDIATION[kind].split(" on ")[0] + f" on {scope}",
                    remediation=REMEDIATION[kind].format(scope=scope),
                )
            if r.status_code >= 400:
                raise AzureApiError(
                    f"{method} {path} failed ({r.status_code}): {_error_message(r)}", r.status_code, rid
                )
            data = r.json() if r.content else {}
            resp = ArmResponse(r.status_code, data, rid, dict(r.headers))
            self._cache_put(key, resp)
            return resp

    def get(self, path: str, params: dict | None = None, **kw) -> ArmResponse:
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, body: dict, params: dict | None = None, **kw) -> ArmResponse:
        return self.request("POST", path, params=params, body=body, **kw)

    def get_paged(self, path: str, params: dict | None = None, max_pages: int = 50) -> Iterator[ArmResponse]:
        resp = self.get(path, params)
        yield resp
        pages = 1
        while resp.body.get("nextLink") and pages < max_pages:
            resp = self.get(resp.body["nextLink"])
            pages += 1
            yield resp

    # -- higher level helpers -------------------------------------------------------------------
    def cost_query(self, scope: str, body: dict, qpu: int = 1, max_pages: int = 20) -> list[ArmResponse]:
        path = f"{scope.rstrip('/')}/providers/Microsoft.CostManagement/query"
        out: list[ArmResponse] = []
        last_err: AzureApiError | None = None
        for ver in dict.fromkeys(COST_API_VERSIONS):
            try:
                resp = self.post(path, body, {"api-version": ver}, qpu=qpu, scope_label=scope)
                break
            except AzureApiError as exc:
                if exc.status == 400 and "api-version" in exc.message.lower():
                    last_err = exc
                    continue
                raise
        else:
            raise last_err or AzureApiError("no supported Cost Management api-version")
        out.append(resp)
        while (resp.body.get("properties") or {}).get("nextLink") and len(out) < max_pages:
            resp = self.post(resp.body["properties"]["nextLink"], body, qpu=qpu, scope_label=scope)
            out.append(resp)
        return out

    def resource_graph(
        self, query: str, subscriptions: list[str], max_rows: int = 5000
    ) -> tuple[list[dict], list[str]]:
        rows: list[dict] = []
        rids: list[str] = []
        skip: str | None = None
        while True:
            opts: dict = {"resultFormat": "objectArray", "$top": min(1000, max_rows - len(rows))}
            if skip:
                opts["$skipToken"] = skip
            resp = self.post(
                "/providers/Microsoft.ResourceGraph/resources",
                {"subscriptions": subscriptions, "query": query, "options": opts},
                {"api-version": ARG_API_VERSION},
                scope_label=", ".join(f"/subscriptions/{s}" for s in subscriptions),
            )
            if resp.request_id:
                rids.append(resp.request_id)
            data = resp.body.get("data") or []
            if isinstance(data, dict):  # table format fallback
                cols = [c["name"] for c in data.get("columns", [])]
                data = [dict(zip(cols, r, strict=False)) for r in data.get("rows", [])]
            rows.extend(data)
            skip = resp.body.get("$skipToken")
            if not skip or len(rows) >= max_rows:
                return rows, rids


def _scope_from_path(path: str, body: dict | None) -> str:
    m = re.match(rf"^(/subscriptions/{_SUB}(?:{_RG})?)", path)
    if m:
        return m.group(1)
    subs = (body or {}).get("subscriptions") or []
    return ", ".join(f"/subscriptions/{s}" for s in subs) or path


def _error_message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ") or r.text[:300]
    except Exception:  # noqa: BLE001
        return r.text[:300]
