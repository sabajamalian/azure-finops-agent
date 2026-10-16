import json
import time

import httpx
import pytest
from fake_github import API, FakeGitHub

from azure_finops.github import GitHub, GitHubError


def make(handler, **kw) -> tuple[GitHub, list[float]]:
    sleeps: list[float] = []
    gh = GitHub(
        "tok-123", "octo/finops", API, transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )
    return gh, sleeps


def sequence(*responses):
    """Handler that returns the given responses in order and records requests."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return r

    return handler, seen


# -- construction and headers ----------------------------------------------------------------------


def test_requires_owner_slash_name():
    with pytest.raises(ValueError, match="owner/name"):
        GitHub("tok", "finops")


def test_auth_and_api_headers():
    handler, seen = sequence(httpx.Response(200, json={"private": True}))
    gh, _ = make(handler)
    assert gh.repo_info() == {"private": True}
    req = seen[0]
    assert req.headers["Authorization"] == "Bearer tok-123"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert req.headers["User-Agent"] == "azure-finops-agent"
    assert str(req.url) == f"{API}/repos/octo/finops"


def test_enterprise_api_url_prefix_and_trailing_slash():
    handler, seen = sequence(httpx.Response(200, json={}))
    gh = GitHub("t", "o/r", "https://ghe.example.com/api/v3/", transport=httpx.MockTransport(handler))
    gh.repo_info()
    gh.request("GET", "repos/o/r/issues/1")  # leading slash is optional
    assert [r.url.path for r in seen] == ["/api/v3/repos/o/r", "/api/v3/repos/o/r/issues/1"]


# -- retries and errors ----------------------------------------------------------------------------


def test_retries_server_errors_with_backoff():
    handler, seen = sequence(httpx.Response(502), httpx.Response(503), httpx.Response(200, json={"ok": 1}))
    gh, sleeps = make(handler)
    assert gh.request("GET", "/x").json() == {"ok": 1}
    assert len(seen) == 3
    assert 1 <= sleeps[0] < 2 and 2 <= sleeps[1] < 3


def test_gives_up_after_max_retries():
    handler, seen = sequence(httpx.Response(500, json={"message": "boom"}))
    gh, sleeps = make(handler, max_retries=2)
    with pytest.raises(GitHubError) as ei:
        gh.request("GET", "/x")
    assert ei.value.status == 500 and "boom" in str(ei.value) and "/x" in str(ei.value)
    assert len(seen) == 3 and len(sleeps) == 2


def test_retry_after_header_is_honored():
    handler, seen = sequence(
        httpx.Response(403, headers={"retry-after": "7"}, json={"message": "slow down"}),
        httpx.Response(200, json={}),
    )
    gh, sleeps = make(handler)
    gh.request("GET", "/x")
    assert sleeps == [7.0] and len(seen) == 2


def test_retry_after_is_capped():
    handler, _ = sequence(httpx.Response(429, headers={"retry-after": "9999"}), httpx.Response(200, json={}))
    gh, sleeps = make(handler)
    gh.request("GET", "/x")
    assert sleeps == [120.0]


def test_primary_rate_limit_waits_until_reset():
    reset = str(int(time.time()) + 30)
    handler, _ = sequence(
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
        httpx.Response(200, json={}),
    )
    gh, sleeps = make(handler)
    gh.request("GET", "/x")
    assert 25 <= sleeps[0] <= 31


def test_secondary_rate_limit_backs_off():
    handler, _ = sequence(
        httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."}),
        httpx.Response(200, json={}),
    )
    gh, sleeps = make(handler)
    gh.request("GET", "/x")
    assert sleeps == [60.0]


def test_plain_403_is_not_retried():
    handler, seen = sequence(httpx.Response(403, json={"message": "Resource not accessible by integration"}))
    gh, sleeps = make(handler)
    with pytest.raises(GitHubError, match="Resource not accessible") as ei:
        gh.request("POST", "/repos/octo/finops/issues", json={})
    assert ei.value.status == 403 and len(seen) == 1 and sleeps == []


def test_404_with_non_json_body_and_long_message():
    handler, _ = sequence(httpx.Response(404, text="x" * 2000))
    gh, sleeps = make(handler)
    with pytest.raises(GitHubError) as ei:
        gh.request("GET", "/missing")
    assert ei.value.status == 404 and sleeps == []
    assert str(ei.value).count("x") <= 501  # message capped at 500 chars


def test_transport_errors_retry_then_raise():
    handler, seen = sequence(httpx.ConnectError("refused"))
    gh, sleeps = make(handler, max_retries=2)
    with pytest.raises(GitHubError) as ei:
        gh.request("GET", "/x")
    assert ei.value.status == 0 and "refused" in str(ei.value)
    assert len(seen) == 3 and sleeps == [1, 2]


def test_transport_error_then_success():
    handler, _ = sequence(httpx.ReadTimeout("slow"), httpx.Response(200, json={"n": 1}))
    gh, sleeps = make(handler)
    assert gh.request("GET", "/x").json() == {"n": 1} and sleeps == [1]


# -- pagination ------------------------------------------------------------------------------------


def test_paged_follows_link_headers_and_sends_params_once():
    fake = FakeGitHub(page_size=2)
    for i in range(5):
        fake.add_issue(f"issue {i}", labels=["finops"])
    gh = fake.client()
    items = list(gh.paged(f"/repos/{fake.repo}/issues", {"labels": "finops", "state": "all"}))
    assert [i["title"] for i in items] == [f"issue {i}" for i in (4, 3, 2, 1, 0)]
    pages = [r for r in fake.requests if r.url.path.endswith("/issues")]
    assert len(pages) == 3
    assert pages[0].url.params["per_page"] == "100" and "page" not in pages[0].url.params
    # Later pages come from the Link header, which carries the original query.
    assert [r.url.params.get("page") for r in pages[1:]] == ["2", "3"]
    assert all(r.url.params["labels"] == "finops" for r in pages)


def test_paged_stops_at_max_pages():
    fake = FakeGitHub(page_size=1)
    for i in range(5):
        fake.add_issue(f"issue {i}")
    items = list(fake.client().paged(f"/repos/{fake.repo}/issues", {"state": "all"}, max_pages=2))
    assert len(items) == 2


def test_paged_with_enterprise_prefix():
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[{"n": 2}])
        nxt = "https://ghe.example.com/api/v3/repositories/1/issues?per_page=100&page=2"
        return httpx.Response(200, json=[{"n": 1}], headers={"Link": f'<{nxt}>; rel="next"'})

    gh = GitHub("t", "o/r", "https://ghe.example.com/api/v3", transport=httpx.MockTransport(handler))
    assert [x["n"] for x in gh.paged("/repos/o/r/issues")] == [1, 2]
    assert calls[1] == "https://ghe.example.com/api/v3/repositories/1/issues?per_page=100&page=2"


# -- issues and comments ---------------------------------------------------------------------------


def test_issues_with_label_filters_pull_requests_and_sends_params():
    fake = FakeGitHub()
    fake.add_issue("real", labels=["finops"])
    fake.add_issue("pr", labels=["finops"], pull_request=True)
    fake.add_issue("other", labels=["other"])
    out = fake.client().issues_with_label("finops", since="2020-01-01T00:00:00Z")
    assert [i["title"] for i in out] == ["real"]
    params = fake.requests[-1].url.params
    assert params["labels"] == "finops" and params["state"] == "all"
    assert params["sort"] == "updated" and params["direction"] == "desc"
    assert params["since"] == "2020-01-01T00:00:00Z"


def test_issue_and_comment_helpers():
    fake = FakeGitHub(page_size=1)
    gh = fake.client()
    issue = gh.create_issue("Title", "Body", ["finops", "x"])
    post = fake.writes[-1]
    assert post.method == "POST" and json.loads(post.content) == {
        "title": "Title",
        "body": "Body",
        "labels": ["finops", "x"],
    }
    n = issue["number"]
    assert gh.get_issue(n)["title"] == "Title"
    c1 = gh.comment(n, "first")
    gh.comment(n, "second")
    assert json.loads(fake.writes[-1].content) == {"body": "second"}
    assert [c["body"] for c in gh.comments(n)] == ["first", "second"]  # paged, one per page
    assert gh.get_comment(c1["id"])["body"] == "first"
    with pytest.raises(GitHubError) as ei:
        gh.get_issue(999)
    assert ei.value.status == 404


# -- labels ----------------------------------------------------------------------------------------


def test_ensure_label_creates_missing_label_once():
    fake = FakeGitHub()
    gh = fake.client()
    gh.ensure_label("finops-report", description="d" * 300)
    assert fake.labels["finops-report"]["color"] == "0e8a16"
    assert len(fake.labels["finops-report"]["description"]) == 100
    gh.ensure_label("finops-report")
    assert len(fake.writes) == 1


def test_ensure_label_quotes_name():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    gh = GitHub("t", "o/r", transport=httpx.MockTransport(handler))
    gh.ensure_label("needs review/x")
    assert seen[0].url.raw_path == b"/repos/o/r/labels/needs%20review%2Fx"


def test_ensure_label_tolerates_concurrent_create():
    handler, seen = sequence(httpx.Response(404), httpx.Response(422, json={"message": "already_exists"}))
    gh, _ = make(handler)
    gh.ensure_label("x")
    assert [r.method for r in seen] == ["GET", "POST"]


@pytest.mark.parametrize(
    ("responses", "status"),
    [
        ([httpx.Response(401, json={"message": "Bad credentials"})], 401),
        ([httpx.Response(404), httpx.Response(403, json={"message": "forbidden"})], 403),
    ],
)
def test_ensure_label_raises_other_errors(responses, status):
    handler, _ = sequence(*responses)
    gh, _ = make(handler)
    with pytest.raises(GitHubError) as ei:
        gh.ensure_label("x")
    assert ei.value.status == status


# -- actions ---------------------------------------------------------------------------------------


def test_dispatch_workflow_drops_empty_inputs():
    fake = FakeGitHub()
    fake.client().dispatch_workflow(
        "finops-run.yml", "main", {"kind": "schedule", "automation": "a", "slot": "", "issue": None}
    )
    req = fake.writes[-1]
    assert req.url.path == "/repos/octo/finops/actions/workflows/finops-run.yml/dispatches"
    assert json.loads(req.content) == {"ref": "main", "inputs": {"kind": "schedule", "automation": "a"}}


def test_workflow_runs_created_filter_and_pages():
    fake = FakeGitHub(page_size=1)
    fake.add_run("finops schedule a a@20260316T0900Z", created_at="2026-03-16T09:05:00Z")
    fake.add_run("finops schedule a a@20260309T0900Z", created_at="2026-03-09T09:05:00Z")
    gh = fake.client()
    runs = gh.workflow_runs("finops-run.yml", "2026-03-10T00:00:00Z")
    assert [r["display_title"].split()[-1] for r in runs] == ["a@20260316T0900Z"]
    assert fake.requests[-1].url.params["created"] == ">=2026-03-10T00:00:00Z"
    assert len(gh.workflow_runs("finops-run.yml")) == 2
    assert "created" not in fake.requests[-1].url.params
    assert len(gh.workflow_runs("finops-run.yml", max_pages=1)) == 1
