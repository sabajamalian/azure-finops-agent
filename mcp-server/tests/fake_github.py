"""In-memory GitHub REST API for tests, served through httpx.MockTransport.

Covers the endpoints azure_finops.github.GitHub uses: repo info, issues, comments, labels, workflow
dispatch and runs, commit provenance, and environments. State is kept in plain dicts so tests can seed it
and assert on it (for example, that a second post creates no duplicate issue).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx

from azure_finops.github import GitHub

API = "https://api.github.test"
BOT = "github-actions[bot]"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(
        self,
        repo: str = "octo/finops",
        *,
        private: bool = True,
        visibility: str | None = None,
        default_branch: str = "main",
        page_size: int | None = None,
        now: datetime | None = None,
    ):
        self.repo = repo
        self.info = {
            "full_name": repo,
            "private": private,
            "visibility": visibility or ("private" if private else "public"),
            "default_branch": default_branch,
        }
        self.issues: dict[int, dict] = {}
        self.issue_comments: dict[int, list[dict]] = {}
        self.labels: dict[str, dict] = {}
        self.runs: list[dict] = []
        self.dispatches: list[dict] = []
        self.commits: dict[str, list[dict]] = {}
        self.commit_pulls: dict[str, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.environments: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.page_size = page_size
        self.now = now or datetime.now(UTC)
        self.auto_runs = True
        self._next_number = 1
        self._next_comment = 9000

    # -- seeding ------------------------------------------------------------------------------------
    def client(self, **kw) -> GitHub:
        kw.setdefault("sleep", lambda _s: None)
        return GitHub("test-token", self.repo, API, transport=httpx.MockTransport(self.handle), **kw)

    def add_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | tuple[str, ...] = (),
        user: str = "alice",
        assoc: str = "OWNER",
        state: str = "open",
        pull_request: bool = False,
        updated_at: str | None = None,
    ) -> dict:
        n = self._next_number
        self._next_number += 1
        issue = {
            "number": n,
            "title": title,
            "body": body,
            "labels": [{"name": lbl} for lbl in labels],
            "state": state,
            "user": {"login": user},
            "author_association": assoc,
            "html_url": f"https://github.test/{self.repo}/issues/{n}",
            "created_at": _iso(self.now),
            "updated_at": updated_at or _iso(self.now),
        }
        if pull_request:
            issue["pull_request"] = {"url": f"{API}/repos/{self.repo}/pulls/{n}"}
        self.issues[n] = issue
        self.issue_comments[n] = []
        return issue

    def add_comment(self, number: int, body: str, user: str = "alice", assoc: str = "MEMBER") -> dict:
        cid = self._next_comment
        self._next_comment += 1
        c = {
            "id": cid,
            "body": body,
            "user": {"login": user},
            "author_association": assoc,
            "issue_url": f"{API}/repos/{self.repo}/issues/{number}",
            "html_url": f"https://github.test/{self.repo}/issues/{number}#issuecomment-{cid}",
            "created_at": _iso(self.now),
        }
        self.issue_comments.setdefault(number, []).append(c)
        return c

    def add_run(self, display_title: str, created_at: str | None = None, **kw) -> dict:
        run = {
            "id": len(self.runs) + 1,
            "display_title": display_title,
            "created_at": created_at or _iso(self.now),
            "status": "completed",
            "conclusion": "success",
            **kw,
        }
        self.runs.append(run)
        return run

    # -- inspection ---------------------------------------------------------------------------------
    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    def comments_on(self, number: int) -> list[dict]:
        return self.issue_comments.get(number, [])

    def bodies(self) -> list[str]:
        return [i["body"] for i in self.issues.values()]

    # -- transport ----------------------------------------------------------------------------------
    def _page(self, request: httpx.Request, items: list, wrap: str | None = None) -> httpx.Response:
        params = request.url.params
        per_page = int(params.get("per_page", 30))
        if self.page_size:
            per_page = min(per_page, self.page_size)
        page = int(params.get("page", 1))
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(items):
            nxt = request.url.copy_set_param("page", str(page + 1))
            headers["Link"] = f'<{nxt}>; rel="next"'
        body = {"total_count": len(items), wrap: chunk} if wrap else chunk
        return httpx.Response(200, json=body, headers=headers)

    def _not_found(self) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix = f"/repos/{self.repo}"
        path = request.url.path
        if not path.startswith(prefix):
            return self._not_found()
        rest = path[len(prefix) :]
        m, p = request.method, request.url.params
        body = json.loads(request.content) if request.content else None

        if rest == "" and m == "GET":
            return httpx.Response(200, json=self.info)

        if rest == "/issues" and m == "GET":
            want = [x for x in (p.get("labels") or "").split(",") if x]
            state = p.get("state", "open")
            since = p.get("since")
            items = [
                i
                for i in sorted(self.issues.values(), key=lambda i: -i["number"])
                if all(w in {lbl["name"] for lbl in i["labels"]} for w in want)
                and (state == "all" or i["state"] == state)
                and (not since or i["updated_at"] >= since)
            ]
            return self._page(request, items)
        if rest == "/issues" and m == "POST":
            issue = self.add_issue(body["title"], body.get("body", ""), body.get("labels", []), BOT, "NONE")
            for lbl in body.get("labels", []):
                self.labels.setdefault(lbl, {"name": lbl})
            return httpx.Response(201, json=issue)

        if mm := re.fullmatch(r"/issues/comments/(\d+)", rest):
            cid = int(mm.group(1))
            for cs in self.issue_comments.values():
                for c in cs:
                    if c["id"] == cid:
                        return httpx.Response(200, json=c)
            return self._not_found()

        if mm := re.fullmatch(r"/issues/(\d+)", rest):
            n = int(mm.group(1))
            return httpx.Response(200, json=self.issues[n]) if n in self.issues else self._not_found()

        if mm := re.fullmatch(r"/issues/(\d+)/comments", rest):
            n = int(mm.group(1))
            if n not in self.issues:
                return self._not_found()
            if m == "GET":
                return self._page(request, self.issue_comments[n])
            c = self.add_comment(n, body["body"], BOT, "NONE")
            return httpx.Response(201, json=c)

        if mm := re.fullmatch(r"/labels/(.+)", rest):
            name = mm.group(1)
            return httpx.Response(200, json=self.labels[name]) if name in self.labels else self._not_found()
        if rest == "/labels" and m == "POST":
            if body["name"] in self.labels:
                return httpx.Response(422, json={"message": "Validation Failed"})
            self.labels[body["name"]] = body
            return httpx.Response(201, json=body)

        if mm := re.fullmatch(r"/actions/workflows/([^/]+)/dispatches", rest):
            inputs = body.get("inputs", {})
            self.dispatches.append({"workflow": mm.group(1), "ref": body["ref"], "inputs": inputs})
            if self.auto_runs:
                self.add_run(
                    f"finops {inputs.get('kind')} {inputs.get('automation') or '-'} "
                    f"{inputs.get('dedupe_key') or '-'}"
                )
            return httpx.Response(204)
        if re.fullmatch(r"/actions/workflows/([^/]+)/runs", rest):
            created = p.get("created", "")
            since = created[2:] if created.startswith(">=") else ""
            runs = [r for r in reversed(self.runs) if r["created_at"] >= since]
            return self._page(request, runs, wrap="workflow_runs")

        if rest == "/commits":
            return httpx.Response(200, json=self.commits.get(p.get("path", ""), []))
        if mm := re.fullmatch(r"/commits/([^/]+)/pulls", rest):
            return httpx.Response(200, json=self.commit_pulls.get(mm.group(1), []))
        if mm := re.fullmatch(r"/pulls/(\d+)/reviews", rest):
            return httpx.Response(200, json=self.reviews.get(int(mm.group(1)), []))

        if mm := re.fullmatch(r"/environments/([^/]+)", rest):
            env = self.environments.get(mm.group(1))
            return httpx.Response(200, json=env) if env is not None else self._not_found()

        return self._not_found()
