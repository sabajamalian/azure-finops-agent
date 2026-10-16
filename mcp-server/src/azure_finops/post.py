"""Post job: validate a headless run's result and publish it to GitHub issues.

Runs without Azure access and only with `issues: write`. Everything the agent produced is treated as
untrusted: it's revalidated against config and the run's evidence log, sanitized, and written with
hidden markers so reruns comment instead of creating duplicates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from .config import Config
from .github import GitHub, GitHubError
from .results import CreateIssue, validate_result_data
from .task import BOT_LOGIN, TRUSTED_ASSOCIATIONS

BASE_LABEL = "finops"
FAILURE_LABEL = "finops-failure"
KIND_LABELS = {
    "schedule": "finops-report",
    "manual": "finops-report",
    "anomaly": "finops-anomaly",
    "ask": None,
}
FAILURES_MARKER = "<!-- finops:failures -->"
MAX_BODY = 60_000
ACCURACY_NOTE = (
    "Figures come from read-only Azure Cost Management, Resource Graph, Advisor, and Activity Log queries. "
    "Recent days may be incomplete and correlation isn't proof of cause. Verify before acting."
)

_MENTION = re.compile(r"(?<![\w`/])@(?=[A-Za-z0-9][A-Za-z0-9-]*)")
_MARKER = re.compile(r"<!--\s*finops:.*?-->", re.IGNORECASE | re.DOTALL)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HTML_MEDIA = re.compile(
    r"<\s*/?\s*(img|picture|source|video|audio|iframe|object|embed|svg|link|meta|base|style|script)\b[^>]*>",
    re.IGNORECASE,
)
_MD_REF_IMAGE = re.compile(r"!\[([^\]]*)\]\[[^\]]*\]")
_FP_SAFE = re.compile(r"[^a-z0-9-]+")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}$")
_APPROVE = re.compile(r"^\s*/finops\s+approve(?:\s+(all|\d+(?:\s*,\s*\d+)*))?\s*$", re.IGNORECASE)


class PostError(Exception):
    pass


def fp_marker(fp: str) -> str:
    return f"<!-- finops:fp={fp} -->"


def run_marker(run_id: str) -> str:
    return f"<!-- finops:run={run_id} -->"


def action_fp(a: CreateIssue) -> str:
    """Proposed issues live in their own `act-` namespace so they can't collide with report or anomaly issues."""
    key = a.fingerprint or hashlib.sha1(a.title.lower().encode()).hexdigest()[:12]  # noqa: S324 - identifier
    return key if key.startswith("act-") else f"act-{key}"


def sanitize(text: str, limit: int = MAX_BODY) -> str:
    """Neutralize mentions, spoofed markers, and auto-loading media; then cap length."""
    text = _MARKER.sub("", text or "")
    text = _MD_IMAGE.sub(lambda m: f"[image removed: {m.group(1) or 'untitled'}]", text)
    text = _MD_REF_IMAGE.sub(lambda m: f"[image removed: {m.group(1) or 'untitled'}]", text)
    text = _HTML_MEDIA.sub("", text)
    text = _MENTION.sub("@\u200b", text)
    if len(text) > limit:
        text = text[:limit] + "\n\n_[truncated]_"
    return text


def safe_fp(value: str) -> str:
    v = _FP_SAFE.sub("-", value.lower()).strip("-")
    if len(v) > 64:
        v = v[:51] + "-" + hashlib.sha1(v.encode()).hexdigest()[:12]  # noqa: S324 - identifier
    return v or "x"


def footer(config: Config, task: dict, run_url: str | None, run_id: str, fp: str | None = None) -> str:
    bits = [f"Posted by **{config.agent.name}**", f"kind `{task.get('kind')}`"]
    if task.get("automation"):
        bits.append(f"automation `{task['automation']['name']}`")
    if run_url:
        bits.append(f"[run {run_id}]({run_url})")
    lines = ["", "---", "<sub>" + " · ".join(bits) + ". " + ACCURACY_NOTE + "</sub>"]
    if fp:
        lines.append(fp_marker(fp))
    lines.append(run_marker(run_id))
    return "\n".join(lines)


class Poster:
    def __init__(
        self, gh: GitHub, config: Config, task: dict, run_id: str, run_url: str | None, dry_run: bool = False
    ):
        self.gh = gh
        self.config = config
        self.task = task
        self.run_id = run_id
        self.run_url = run_url
        self.dry_run = dry_run
        self.posted: list[dict] = []
        self._issues: list[dict] | None = None
        self._labels_ok: set[str] = set()

    # -- helpers ------------------------------------------------------------------------------------
    def existing(self) -> list[dict]:
        if self._issues is None:
            since = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._issues = self.gh.issues_with_label(BASE_LABEL, state="all", since=since)
        return self._issues

    def find_by_fp(self, fp: str) -> dict | None:
        m = fp_marker(fp)
        return next((i for i in self.existing() if m in (i.get("body") or "")), None)

    def already_commented(self, number: int) -> bool:
        m = run_marker(self.run_id)
        return any(m in (c.get("body") or "") for c in self.gh.comments(number))

    def labels(self, extra: list[str]) -> list[str]:
        out: list[str] = []
        for lbl in [BASE_LABEL, *extra]:
            if lbl and _LABEL_RE.match(lbl) and lbl not in out:
                out.append(lbl)
        for lbl in out:
            if lbl not in self._labels_ok and not self.dry_run:
                self.gh.ensure_label(
                    lbl,
                    color="d93f0b" if lbl == FAILURE_LABEL else "0e8a16",
                    description="Created by the Azure FinOps agent",
                )
            self._labels_ok.add(lbl)
        return out

    def _record(self, action: str, **kw) -> dict:
        rec = {"action": action, **kw}
        self.posted.append(rec)
        return rec

    # -- operations ---------------------------------------------------------------------------------
    def upsert_issue(self, fp: str, title: str, body: str, labels: list[str]) -> dict:
        """Create an issue for `fp`, or comment on the existing one (once per run)."""
        fp = safe_fp(fp)
        title = sanitize(title, 200).replace("\n", " ").strip() or "FinOps report"
        body = sanitize(body)
        existing = self.find_by_fp(fp)
        if existing:
            n = existing["number"]
            if run_marker(self.run_id) in (existing.get("body") or "") or self.already_commented(n):
                return self._record(
                    "skipped", reason="already posted by this run", number=n, url=existing.get("html_url")
                )
            text = f"**Update from run {self.run_id}**\n\n{body}{footer(self.config, self.task, self.run_url, self.run_id)}"
            if self.dry_run:
                return self._record("would_comment", number=n, fingerprint=fp)
            c = self.gh.comment(n, text)
            return self._record("commented", number=n, url=c.get("html_url"), fingerprint=fp)
        labels = self.labels(labels)
        text = body + footer(self.config, self.task, self.run_url, self.run_id, fp)
        if self.dry_run:
            return self._record("would_create", title=title, labels=labels, fingerprint=fp)
        issue = self.gh.create_issue(title, text, labels)
        if self._issues is not None:
            self._issues.insert(0, issue)
        return self._record("created", number=issue["number"], url=issue.get("html_url"), fingerprint=fp)

    def comment_once(self, number: int, body: str) -> dict:
        if self.already_commented(number):
            return self._record("skipped", reason="already posted by this run", number=number)
        text = sanitize(body) + footer(self.config, self.task, self.run_url, self.run_id)
        if self.dry_run:
            return self._record("would_comment", number=number)
        c = self.gh.comment(number, text)
        return self._record("commented", number=number, url=c.get("html_url"))

    def report_failure(self, reason: str, details: list[str] | None = None) -> dict:
        kind = self.task.get("kind")
        what = (self.task.get("automation") or {}).get("name") or kind or "unknown task"
        lines = [f"**Run {self.run_id} ({what}) failed:** {reason}"]
        if details:
            lines += ["", *[f"- {sanitize(d, 500)}" for d in details[:20]]]
        if self.run_url:
            lines += ["", f"Artifacts and logs: {self.run_url}"]
        body = "\n".join(lines)
        issues = [
            i
            for i in self.existing()
            if FAILURES_MARKER in (i.get("body") or "") and i.get("state") == "open"
        ]
        if issues:
            if run_marker(self.run_id) in (issues[0].get("body") or ""):
                return self._record(
                    "skipped", reason="already posted by this run", number=issues[0]["number"]
                )
            return self.comment_once(issues[0]["number"], body)
        labels = self.labels([FAILURE_LABEL])
        text = (
            "Failed headless FinOps runs are recorded here. Close this issue once they're resolved; the next "
            f"failure opens a new one.\n\n{FAILURES_MARKER}\n\n{body}"
            + footer(self.config, self.task, self.run_url, self.run_id)
        )
        if self.dry_run:
            return self._record("would_create", title="FinOps automation failures", labels=labels)
        issue = self.gh.create_issue("FinOps automation failures", text, labels)
        if self._issues is not None:
            self._issues.insert(0, issue)
        return self._record("created", number=issue["number"], url=issue.get("html_url"), failure=True)


# -- proposals (ask runs) --------------------------------------------------------------------------


def encode_proposals(actions: list[CreateIssue]) -> str:
    raw = json.dumps([a.model_dump() for a in actions], separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


def decode_proposals(body: str) -> list[CreateIssue]:
    m = re.search(r"<!-- finops:proposals=([A-Za-z0-9+/=]+) -->", body or "")
    if not m:
        return []
    return [CreateIssue.model_validate(x) for x in json.loads(base64.b64decode(m.group(1)))]


def proposal_comment(actions: list[CreateIssue]) -> str:
    lines = [
        "### Proposed issues",
        "",
        "The agent suggests creating these issues. A collaborator can reply `/finops approve` (all), "
        "or `/finops approve 1,3` to create specific ones.",
        "",
    ]
    for i, a in enumerate(actions, 1):
        lines += [
            f"<details><summary><b>{i}. {sanitize(a.title, 200)}</b> → <code>{a.destination}</code></summary>",
            "",
            sanitize(a.body, 8_000),
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines)


# -- entry points ----------------------------------------------------------------------------------


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _evidence_ids(p: Path) -> set[str]:
    if not p.exists():
        return set()
    return {json.loads(line)["id"] for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def _date_label(task: dict, config: Config) -> str:
    slot = task.get("slot") or ""
    m = re.search(r"@(\d{8})T", slot)
    if m:
        d = m.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return datetime.now(UTC).date().isoformat()


def report_title(task: dict, config: Config) -> str:
    kind = task.get("kind")
    if kind == "anomaly" and task.get("anomaly"):
        a = task["anomaly"]
        return a.get("title") or f"Cost anomaly in {a.get('subscription_name')} since {a.get('start_date')}"
    if task.get("automation"):
        return f"{task['automation']['name']}: {_date_label(task, config)}"
    q = (task.get("question") or "FinOps question").strip().splitlines()[0]
    return f"FinOps: {q[:120]}"


def report_fp(task: dict, run_id: str) -> str:
    if task.get("kind") == "anomaly" and task.get("anomaly"):
        return f"anomaly-{task['anomaly']['fingerprint']}"
    if task.get("slot"):
        return safe_fp(f"slot-{task['slot']}")
    return f"run-{run_id}"


def post(
    output_dir: Path,
    config: Config,
    gh: GitHub,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    env = dict(os.environ if env is None else env)
    task = _load_json(output_dir / "task.json")
    run_id = env.get("GITHUB_RUN_ID") or (task.get("initiator") or {}).get("run_id") or "local"
    run_url = (task.get("initiator") or {}).get("run_url")
    poster = Poster(gh, config, task, run_id, run_url, dry_run)
    out: dict = {
        "run_id": run_id,
        "kind": task.get("kind"),
        "status": None,
        "errors": [],
        "posted": poster.posted,
    }

    repo = gh.repo_info()
    if repo.get("private") is False or repo.get("visibility") == "public":
        if not config.allow_public_repo_output:
            out["status"] = "refused"
            out["errors"].append(
                "repository is public; set allow_public_repo_output: true to allow posting spend data"
            )
            return out
    if not task:
        out["status"] = "failed"
        poster.report_failure("task.json is missing; the run job failed before preparing the task")
        return out

    kind = task["kind"]
    default_ref = f"refs/heads/{repo.get('default_branch', 'main')}"
    ref = (task.get("initiator") or {}).get("ref")
    if kind in ("schedule", "anomaly") and ref != default_ref:
        out["status"] = "refused"
        out["errors"].append(f"{kind} runs must come from {default_ref}, got {ref}")
        poster.report_failure(f"refused: {kind} run came from {ref}, not {default_ref}")
        return out

    data = _load_json(output_dir / "result.json")
    res, errors = validate_result_data(
        data,
        config,
        _evidence_ids(output_dir / "evidence.jsonl"),
        set(task.get("allowed_destinations") or []),
    )
    issue_no = (task.get("issue") or {}).get("number")
    if res is None:
        out["status"] = "invalid"
        out["errors"] = errors
        poster.report_failure("the agent's result failed validation", errors)
        if kind == "ask" and issue_no:
            poster.comment_once(
                issue_no,
                "Sorry, I couldn't produce a valid answer for this question. A maintainer "
                "can check the failures issue and the run artifacts.",
            )
        return out

    out["status"] = res.status
    base_labels = [lbl for lbl in [KIND_LABELS.get(kind)] if lbl]

    if kind == "ask":
        if not issue_no:
            raise PostError("ask task without an issue number")
        poster.comment_once(issue_no, res.summary)
        if res.actions:
            marker = f"<!-- finops:proposals-run={run_id} -->"
            body = (
                proposal_comment(res.actions)
                + f"\n<!-- finops:proposals={encode_proposals(res.actions)} -->\n"
                + marker
            )
            if any(marker in (c.get("body") or "") for c in gh.comments(issue_no)):
                poster._record("skipped", reason="proposals already posted by this run", number=issue_no)
            elif dry_run:
                poster._record("would_comment", number=issue_no, proposals=len(res.actions))
            else:
                c = gh.comment(issue_no, body)
                poster._record("proposed", number=issue_no, url=c.get("html_url"), proposals=len(res.actions))
        if res.status == "failed":
            poster.report_failure("the agent reported status failed", [res.summary[:500]])
        return out

    if res.status == "failed":
        poster.report_failure("the agent reported status failed", [res.summary[:1000]])
    elif res.status == "ok" or kind == "manual":
        dest = config.destination(task["allowed_destinations"][0])
        poster.upsert_issue(
            report_fp(task, run_id),
            report_title(task, config),
            res.summary,
            [*base_labels, *(dest.labels if dest else [])],
        )

    for a in res.actions:
        dest = config.destination(a.destination)
        poster.upsert_issue(action_fp(a), a.title, a.body, [*(dest.labels if dest else []), *a.labels])
    return out


def approve(
    config: Config,
    gh: GitHub,
    issue_number: int,
    comment_id: int,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Create proposed issues from an ask run after a collaborator replies `/finops approve [n,...|all]`."""
    env = dict(os.environ if env is None else env)
    run_id = env.get("GITHUB_RUN_ID", "local")
    server, repo_name = (
        env.get("GITHUB_SERVER_URL", "https://github.com"),
        env.get("GITHUB_REPOSITORY", gh.repo),
    )
    run_url = f"{server}/{repo_name}/actions/runs/{run_id}" if run_id != "local" else None
    comment = gh.get_comment(comment_id)
    if not str(comment.get("issue_url", "")).endswith(f"/issues/{issue_number}"):
        raise PostError("approval comment does not belong to the issue")
    approver, assoc = comment["user"]["login"], comment.get("author_association")
    if assoc not in TRUSTED_ASSOCIATIONS:
        raise PostError(f"{approver} ({assoc}) can't approve; needs OWNER, MEMBER, or COLLABORATOR")
    m = _APPROVE.match(comment.get("body") or "")
    if not m:
        raise PostError("approval must be `/finops approve`, `/finops approve all`, or `/finops approve 1,2`")

    repo = gh.repo_info()
    if (
        repo.get("private") is False or repo.get("visibility") == "public"
    ) and not config.allow_public_repo_output:
        raise PostError("repository is public; refusing to post spend data")

    proposals: list[CreateIssue] = []
    source = None
    for c in reversed(gh.comments(issue_number)):
        if c["user"]["login"] == BOT_LOGIN and "finops:proposals=" in (c.get("body") or ""):
            try:
                proposals = decode_proposals(c["body"])
            except (ValueError, ValidationError) as exc:
                raise PostError(f"couldn't read the proposals comment: {exc}") from exc
            source = c
            break
    if not proposals:
        raise PostError("no proposals from the agent found on this issue")

    sel = (m.group(1) or "all").lower()
    idx = list(range(1, len(proposals) + 1)) if sel == "all" else sorted({int(x) for x in sel.split(",")})
    bad = [i for i in idx if i < 1 or i > len(proposals)]
    if bad:
        raise PostError(f"no proposal number {bad}; choose 1 to {len(proposals)}")
    if len(idx) > config.max_issues_per_run:
        raise PostError(f"{len(idx)} issues exceed max_issues_per_run ({config.max_issues_per_run})")

    unknown = [i for i in idx if config.destination(proposals[i - 1].destination) is None]
    if unknown:
        raise PostError(f"proposals {unknown} target a destination that isn't in finops/config.yaml")

    task = {"kind": "approve", "automation": None}
    poster = Poster(gh, config, task, run_id, run_url, dry_run)
    created = []
    for i in idx:
        a = proposals[i - 1]
        dest = config.destination(a.destination)
        body = a.body + f"\n\n_Approved by `{approver}` in #{issue_number}._"
        created.append(poster.upsert_issue(action_fp(a), a.title, body, [*dest.labels, *a.labels]))
    links = [f"- {r['action']}: #{r['number']}" for r in created if r.get("number")]
    poster.comment_once(
        issue_number, f"Approved by `{approver}`:\n\n" + "\n".join(links or ["- nothing to do"])
    )
    return {
        "approved_by": approver,
        "source_comment": source.get("html_url") if source else None,
        "posted": poster.posted,
    }


def step_summary(result: dict) -> str:
    lines = [f"### FinOps post: {result.get('status')}", ""]
    for e in result.get("errors", []):
        lines.append(f"- error: {e}")
    for p in result.get("posted", []):
        target = p.get("url") or (f"#{p['number']}" if p.get("number") else p.get("title", ""))
        lines.append(f"- {p['action']}: {target}")
    return "\n".join(lines) + "\n"


__all__ = ["GitHubError", "Poster", "approve", "post", "sanitize", "step_summary"]
