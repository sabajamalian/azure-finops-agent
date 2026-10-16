"""Minimal GitHub REST client for the post, dispatch, and approve steps (no Azure access here)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import httpx

API = "https://api.github.com"
RETRY_STATUS = {500, 502, 503, 504}


class GitHubError(Exception):
    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"GitHub API {status} on {path}: {message}")
        self.status = status


class GitHub:
    def __init__(
        self,
        token: str,
        repo: str,
        api_url: str = API,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
    ):
        if "/" not in repo:
            raise ValueError("repo must be owner/name")
        self.repo = repo
        self.sleep = sleep
        self.max_retries = max_retries
        self.http = httpx.Client(
            base_url=api_url.rstrip("/"),
            transport=transport,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "azure-finops-agent",
            },
        )

    def _wait(self, r: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the response is final."""
        if r.status_code in RETRY_STATUS:
            return min(60.0, 2**attempt + random.random())
        if r.status_code in (403, 429):
            ra = r.headers.get("retry-after")
            if ra and ra.isdigit():
                return min(120.0, float(ra))
            if (
                r.headers.get("x-ratelimit-remaining") == "0"
                and r.headers.get("x-ratelimit-reset", "").isdigit()
            ):
                return max(1.0, min(120.0, int(r.headers["x-ratelimit-reset"]) - time.time()))
            if "secondary rate limit" in r.text.lower():
                return min(120.0, 60.0 * (attempt + 1))
        return None

    def request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None
    ) -> httpx.Response:
        url = path if path.startswith("/") else f"/{path}"
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.request(method, url, params=params, json=json)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise GitHubError(0, str(exc), url) from exc
                self.sleep(min(30.0, 2**attempt))
                continue
            wait = self._wait(r, attempt)
            if wait is None or attempt == self.max_retries:
                if r.status_code >= 400:
                    try:
                        msg = r.json().get("message", r.text)
                    except ValueError:
                        msg = r.text
                    raise GitHubError(r.status_code, str(msg)[:500], url)
                return r
            self.sleep(wait)
        raise AssertionError("unreachable")

    def paged(self, path: str, params: dict | None = None, max_pages: int = 20) -> Iterator[dict]:
        params = {"per_page": 100, **(params or {})}
        url: str | None = path
        for _ in range(max_pages):
            if url is None:
                return
            r = self.request("GET", url, params=params)
            data = r.json()
            yield from (data.get("workflow_runs", []) if isinstance(data, dict) else data)
            nxt = r.links.get("next", {}).get("url")
            url, params = (nxt.replace(str(self.http.base_url), "") if nxt else None), None

    # -- repo -------------------------------------------------------------------------------------
    def repo_info(self) -> dict:
        return self.request("GET", f"/repos/{self.repo}").json()

    # -- issues -----------------------------------------------------------------------------------
    def issues_with_label(self, label: str, state: str = "all", since: str | None = None) -> list[dict]:
        params = {"labels": label, "state": state, "sort": "updated", "direction": "desc"}
        if since:
            params["since"] = since
        return [i for i in self.paged(f"/repos/{self.repo}/issues", params) if "pull_request" not in i]

    def get_issue(self, number: int) -> dict:
        return self.request("GET", f"/repos/{self.repo}/issues/{number}").json()

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        return self.request(
            "POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels}
        ).json()

    def comments(self, number: int) -> list[dict]:
        return list(self.paged(f"/repos/{self.repo}/issues/{number}/comments"))

    def get_comment(self, comment_id: int) -> dict:
        return self.request("GET", f"/repos/{self.repo}/issues/comments/{comment_id}").json()

    def comment(self, number: int, body: str) -> dict:
        return self.request(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}
        ).json()

    def ensure_label(self, name: str, color: str = "0e8a16", description: str = "") -> None:
        try:
            self.request("GET", f"/repos/{self.repo}/labels/{quote(name, safe='')}")
        except GitHubError as exc:
            if exc.status != 404:
                raise
            try:
                self.request(
                    "POST",
                    f"/repos/{self.repo}/labels",
                    json={"name": name, "color": color, "description": description[:100]},
                )
            except GitHubError as exc2:
                if exc2.status != 422:  # created concurrently
                    raise

    # -- actions ----------------------------------------------------------------------------------
    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        self.request(
            "POST",
            f"/repos/{self.repo}/actions/workflows/{workflow}/dispatches",
            json={"ref": ref, "inputs": {k: v for k, v in inputs.items() if v not in (None, "")}},
        )

    def workflow_runs(
        self, workflow: str, created_since: str | None = None, max_pages: int = 3
    ) -> list[dict]:
        params = {"created": f">={created_since}"} if created_since else {}
        return list(self.paged(f"/repos/{self.repo}/actions/workflows/{workflow}/runs", params, max_pages))
