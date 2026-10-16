import base64
import json
import re
from pathlib import Path

import pytest
from fake_github import BOT, FakeGitHub

from azure_finops.post import (
    FAILURES_MARKER,
    Poster,
    PostError,
    approve,
    decode_proposals,
    fp_marker,
    post,
    report_fp,
    report_title,
    run_marker,
    safe_fp,
    sanitize,
    step_summary,
)
from azure_finops.simulator.synthetic import PROD, synthetic_config

RUN = {"GITHUB_RUN_ID": "77"}
RUN_URL = "https://github.com/octo/finops/actions/runs/77"
PROPOSALS_RE = re.compile(r"<!-- finops:proposals=([A-Za-z0-9+/=]+) -->")


def make_task(kind: str = "manual", **kw) -> dict:
    task = {
        "version": 1,
        "task_id": f"{kind}-77",
        "kind": kind,
        "automation": None,
        "slot": None,
        "question": None,
        "anomaly": None,
        "issue": None,
        "subscriptions": [PROD],
        "allowed_destinations": ["finops-team", "platform-team"],
        "initiator": {"ref": "refs/heads/main", "run_id": "77", "run_url": RUN_URL},
    }
    if kind == "manual":
        task["question"] = "How much did prod cost last week?\nMore detail here."
    if kind == "schedule":
        task["automation"] = {"name": "weekly", "file": "finops/automations/weekly.yaml"}
        task["slot"] = "weekly@20260316T0900Z"
    if kind == "anomaly":
        task["automation"] = {"name": "spikes", "file": "finops/automations/spikes.yaml"}
        task["anomaly"] = {
            "fingerprint": "0123456789ab",
            "title": "New spend in rg-ml",
            "subscription_name": "contoso-prod",
            "start_date": "2026-03-12",
        }
    return task | kw


def action(title: str = "Delete idle disks", **kw) -> dict:
    return {
        "destination": "platform-team",
        "title": title,
        "body": f"{title}: details.",
        "labels": ["cleanup"],
        "evidence_ids": ["ev-001"],
    } | kw


def make_result(status: str = "ok", summary: str = "Spend rose 12% because of GPUs.", **kw) -> dict:
    return {"version": 1, "status": status, "summary": summary, "evidence_ids": ["ev-001"]} | kw


def write_run(
    root: Path,
    task: dict | None,
    result: dict | None = None,
    evidence: tuple[str, ...] = ("ev-001", "ev-002"),
) -> Path:
    out = root / "run"
    out.mkdir(parents=True, exist_ok=True)
    if task is not None:
        (out / "task.json").write_text(json.dumps(task))
    if result is not None:
        (out / "result.json").write_text(json.dumps(result))
    (out / "evidence.jsonl").write_text("".join(json.dumps({"id": e}) + "\n" for e in evidence))
    return out


def labels_of(issue: dict) -> list[str]:
    return [lbl["name"] for lbl in issue["labels"]]


def new_issues(fake: FakeGitHub) -> list[dict]:
    return [i for i in fake.issues.values() if i["user"]["login"] == BOT]


def bot_comments(fake: FakeGitHub) -> list[dict]:
    return [c for cs in fake.issue_comments.values() for c in cs if c["user"]["login"] == BOT]


def failure_issues(fake: FakeGitHub) -> list[dict]:
    return [i for i in fake.issues.values() if FAILURES_MARKER in i["body"]]


CONFIG = synthetic_config()


# -- helpers ---------------------------------------------------------------------------------------


def test_sanitize_neutralizes_markers_mentions_and_media():
    text = (
        "Hi @octocat and @org/team, mail ops@example.com, code `@x` and path a/@b.\n"
        "<!-- finops:fp=anomaly-0123456789ab -->\n<!--FINOPS:proposals=\nQUJD\n-->\n<!-- normal -->\n"
        '![chart](https://evil.test/p.png "t") ![](<https://evil.test/x.png>) ![ref][1]\n'
        '<img src="https://evil.test/a.png"> <IMG SRC=x /> <iframe src=x></iframe> <svg onload=x>'
        "<details>kept</details>"
    )
    out = sanitize(text)
    assert "@\u200boctocat" in out and "@\u200borg/team" in out
    assert "ops@example.com" in out and "`@x`" in out and "a/@b" in out
    assert "finops:" not in out.lower() and "<!-- normal -->" in out
    assert "[image removed: chart]" in out and "[image removed: untitled]" in out
    assert "[image removed: ref]" in out
    assert "evil.test" not in out
    assert not re.search(r"<\s*/?\s*(img|iframe|svg)", out, re.IGNORECASE)
    assert "<details>kept</details>" in out


def test_sanitize_truncates():
    assert sanitize("x" * 20, limit=10) == "x" * 10 + "\n\n_[truncated]_"
    assert sanitize(None) == ""  # type: ignore[arg-type]


def test_safe_fp():
    assert safe_fp("Slot-Weekly@20260316T0900Z") == "slot-weekly-20260316t0900z"
    assert safe_fp("--a b--") == "a-b"
    assert safe_fp("!!!") == "x"
    long = safe_fp("a" * 100)
    assert len(long) == 64 and long.startswith("a" * 51 + "-")
    assert safe_fp("a" * 100) == long and safe_fp("a" * 101) != long


def test_report_title_and_fp():
    assert report_title(make_task("schedule"), CONFIG) == "weekly: 2026-03-16"
    assert report_title(make_task("anomaly"), CONFIG) == "New spend in rg-ml"
    no_title = make_task("anomaly")
    no_title["anomaly"] = {**no_title["anomaly"], "title": None}
    assert report_title(no_title, CONFIG) == "Cost anomaly in contoso-prod since 2026-03-12"
    assert report_title(make_task("manual"), CONFIG) == "FinOps: How much did prod cost last week?"
    assert report_title(make_task("manual", question="q" * 300), CONFIG) == "FinOps: " + "q" * 120
    assert report_fp(make_task("schedule"), "77") == "slot-weekly-20260316t0900z"
    assert report_fp(make_task("anomaly"), "77") == "anomaly-0123456789ab"
    assert report_fp(make_task("manual"), "77") == "run-77"


def test_step_summary():
    text = step_summary(
        {
            "status": "ok",
            "errors": ["bad thing"],
            "posted": [
                {"action": "created", "number": 3, "url": "https://x/3"},
                {"action": "commented", "number": 4},
                {"action": "would_create", "title": "T"},
            ],
        }
    )
    assert text == (
        "### FinOps post: ok\n\n- error: bad thing\n- created: https://x/3\n- commented: #4\n- would_create: T\n"
    )


# -- post: guards ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kw", [{"private": False}, {"private": True, "visibility": "public"}])
def test_post_refuses_public_repo(tmp_path, kw):
    fake = FakeGitHub(**kw)
    out = write_run(tmp_path, make_task(), make_result())
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "refused" and "repository is public" in res["errors"][0]
    assert fake.writes == [] and res["posted"] == []


def test_post_public_repo_allowed_by_config(tmp_path):
    fake = FakeGitHub(private=False)
    out = write_run(tmp_path, make_task(), make_result())
    cfg = CONFIG.model_copy(update={"allow_public_repo_output": True})
    res = post(out, cfg, fake.client(), env=RUN)
    assert res["status"] == "ok" and len(new_issues(fake)) == 1


def test_post_missing_task_reports_failure(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, None)
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "failed" and res["kind"] is None and res["run_id"] == "77"
    [issue] = new_issues(fake)
    assert issue["title"] == "FinOps automation failures"
    assert labels_of(issue) == ["finops", "finops-failure"]
    assert FAILURES_MARKER in issue["body"] and "task.json is missing" in issue["body"]
    assert run_marker("77") in issue["body"]
    assert fake.labels["finops-failure"]["color"] == "d93f0b"
    assert fake.labels["finops"]["color"] == "0e8a16"
    assert res["posted"] == [
        {"action": "created", "number": issue["number"], "url": issue["html_url"], "failure": True}
    ]


def test_failure_report_comments_on_open_failures_issue(tmp_path):
    fake = FakeGitHub()
    closed = fake.add_issue(
        "old failures", f"x\n{FAILURES_MARKER}", ["finops", "finops-failure"], state="closed"
    )
    open_ = fake.add_issue(
        "FinOps automation failures", f"x\n{FAILURES_MARKER}", ["finops", "finops-failure"]
    )
    out = write_run(tmp_path, None)
    post(out, CONFIG, fake.client(), env=RUN)
    assert new_issues(fake) == []
    assert fake.comments_on(closed["number"]) == []
    [c] = fake.comments_on(open_["number"])
    assert (
        "**Run 77 (unknown task) failed:** task.json is missing" in c["body"]
        and run_marker("77") in c["body"]
    )
    # A rerun of the same workflow run doesn't comment twice.
    post(out, CONFIG, fake.client(), env=RUN)
    assert len(fake.comments_on(open_["number"])) == 1


def test_failure_report_rerun_does_not_comment_on_issue_it_created(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, None)
    post(out, CONFIG, fake.client(), env=RUN)
    res = post(out, CONFIG, fake.client(), env=RUN)
    [issue] = new_issues(fake)
    assert fake.comments_on(issue["number"]) == []
    assert res["posted"][0]["action"] == "skipped"
    # A different run comments on the open failures issue.
    post(out, CONFIG, fake.client(), env={"GITHUB_RUN_ID": "78"})
    assert len(fake.comments_on(issue["number"])) == 1 and len(new_issues(fake)) == 1


@pytest.mark.parametrize("kind", ["schedule", "anomaly"])
@pytest.mark.parametrize("ref", ["refs/heads/feature", "refs/tags/v1", None])
def test_post_scheduled_runs_must_come_from_default_branch(tmp_path, kind, ref):
    fake = FakeGitHub()
    task = make_task(kind)
    task["initiator"]["ref"] = ref
    out = write_run(tmp_path, task, make_result())
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "refused"
    assert res["errors"] == [f"{kind} runs must come from refs/heads/main, got {ref}"]
    [issue] = new_issues(fake)
    assert FAILURES_MARKER in issue["body"] and f"came from {ref}, not refs/heads/main" in issue["body"]


def test_post_default_branch_comes_from_repo(tmp_path):
    fake = FakeGitHub(default_branch="trunk")
    out = write_run(tmp_path, make_task("schedule"), make_result())
    assert post(out, CONFIG, fake.client(), env=RUN)["status"] == "refused"
    task = make_task("schedule")
    task["initiator"]["ref"] = "refs/heads/trunk"
    out = write_run(tmp_path, task, make_result())
    assert post(out, CONFIG, fake.client(), env=RUN)["status"] == "ok"


def test_post_manual_runs_from_any_branch(tmp_path):
    fake = FakeGitHub()
    task = make_task("manual")
    task["initiator"]["ref"] = "refs/heads/feature"
    out = write_run(tmp_path, task, make_result())
    assert post(out, CONFIG, fake.client(), env=RUN)["status"] == "ok"


# -- post: validation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "allowed", "fragment"),
    [
        (make_result(evidence_ids=["ev-999"]), None, "unknown evidence ids ['ev-999']"),
        (make_result(actions=[action(evidence_ids=["ev-404"])]), None, "unknown evidence ids ['ev-404']"),
        (make_result(actions=[action()]), ["finops-team"], "destination 'platform-team' is not allowed"),
        (make_result(actions=[action(destination="nope")]), None, "destination 'nope' is not allowed"),
        (make_result(actions=[action(f"Action {i}") for i in range(6)]), None, "exceed max_issues_per_run"),
        ({"status": "ok"}, None, "summary: Field required"),
        ({}, None, "status: Field required"),
    ],
)
def test_post_invalid_result_reports_failure(tmp_path, result, allowed, fragment):
    fake = FakeGitHub()
    task = make_task("manual")
    if allowed:
        task["allowed_destinations"] = allowed
    out = write_run(tmp_path, task, result)
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "invalid"
    assert any(fragment in e for e in res["errors"]), res["errors"]
    [issue] = new_issues(fake)
    assert FAILURES_MARKER in issue["body"] and "failed validation" in issue["body"]
    assert fragment.split("'")[0] in issue["body"]


def test_post_missing_result_is_invalid(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("schedule"))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "invalid" and len(failure_issues(fake)) == 1


def test_post_invalid_ask_result_apologizes_on_the_issue(tmp_path):
    fake = FakeGitHub()
    ask = fake.add_issue("Q", "Why?", ["finops-ask"])
    task = make_task("ask", issue={"number": ask["number"]})
    out = write_run(tmp_path, task, make_result(evidence_ids=["ev-999"]))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "invalid"
    [c] = fake.comments_on(ask["number"])
    assert c["body"].startswith("Sorry, I couldn't produce a valid answer")
    assert len(failure_issues(fake)) == 1


# -- post: reports and actions ---------------------------------------------------------------------


def test_post_manual_ok_creates_report_issue(tmp_path):
    fake = FakeGitHub()
    summary = "Spend rose. cc @octocat <!-- finops:fp=anomaly-0123456789ab --> ![x](https://evil.test/a.png)"
    out = write_run(tmp_path, make_task("manual"), make_result(summary=summary))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "ok" and res["errors"] == [] and res["kind"] == "manual"
    [issue] = new_issues(fake)
    assert issue["title"] == "FinOps: How much did prod cost last week?"
    assert labels_of(issue) == ["finops", "finops-report"]
    body = issue["body"]
    assert body.startswith("Spend rose. cc @\u200boctocat")
    assert "anomaly-0123456789ab" not in body and "evil.test" not in body
    assert fp_marker("run-77") in body and run_marker("77") in body
    assert f"[run 77]({RUN_URL})" in body and "Posted by **azure-finops**" in body
    assert res["posted"] == [
        {"action": "created", "number": issue["number"], "url": issue["html_url"], "fingerprint": "run-77"}
    ]


def test_post_manual_no_action_still_posts_report(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("manual"), make_result("no_action"))
    assert post(out, CONFIG, fake.client(), env=RUN)["status"] == "no_action"
    assert len(new_issues(fake)) == 1


def test_post_schedule_no_action_posts_nothing(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("schedule"), make_result("no_action"))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "no_action" and res["posted"] == []
    assert fake.writes == []


def test_post_report_labels_use_first_allowed_destination(tmp_path):
    fake = FakeGitHub()
    task = make_task("schedule", allowed_destinations=["platform-team"])
    out = write_run(tmp_path, task, make_result())
    post(out, CONFIG, fake.client(), env=RUN)
    [issue] = new_issues(fake)
    assert issue["title"] == "weekly: 2026-03-16"
    assert labels_of(issue) == ["finops", "finops-report", "platform"]
    assert fp_marker("slot-weekly-20260316t0900z") in issue["body"]
    assert "automation `weekly`" in issue["body"]


def test_post_anomaly_report(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("anomaly"), make_result())
    post(out, CONFIG, fake.client(), env=RUN)
    [issue] = new_issues(fake)
    assert issue["title"] == "New spend in rg-ml"
    assert labels_of(issue) == ["finops", "finops-anomaly"]
    assert fp_marker("anomaly-0123456789ab") in issue["body"]


def test_post_failed_status_reports_failure_only(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("schedule"), make_result("failed", summary="Cost API kept failing"))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "failed"
    [issue] = new_issues(fake)
    assert FAILURES_MARKER in issue["body"]
    assert "**Run 77 (weekly) failed:** the agent reported status failed" in issue["body"]
    assert "- Cost API kept failing" in issue["body"] and f"Artifacts and logs: {RUN_URL}" in issue["body"]


def test_post_actions_are_upserted_by_fingerprint(tmp_path):
    fake = FakeGitHub()
    actions = [
        action(
            "Delete idle disks", fingerprint="idle-disks", labels=["cleanup", "bad label!", "-x", "cleanup"]
        ),
        action("Resize VMs", destination="finops-team", labels=[]),
    ]
    out = write_run(tmp_path, make_task("schedule"), make_result(actions=actions))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert [p["action"] for p in res["posted"]] == ["created", "created", "created"]
    report, disks, vms = new_issues(fake)
    assert report["title"] == "weekly: 2026-03-16"
    assert disks["title"] == "Delete idle disks"
    assert labels_of(disks) == ["finops", "platform", "cleanup"]
    assert fp_marker("act-idle-disks") in disks["body"]
    assert labels_of(vms) == ["finops"]
    assert re.search(r"<!-- finops:fp=act-[0-9a-f]{12} -->", vms["body"])

    # Rerunning the post job for the same run creates nothing and comments nowhere.
    writes = len(fake.writes)
    res2 = post(out, CONFIG, fake.client(), env=RUN)
    assert len(new_issues(fake)) == 3 and bot_comments(fake) == []
    assert [p["action"] for p in res2["posted"]] == ["skipped"] * 3
    assert len(fake.writes) == writes

    # A later run with the same fingerprints comments once on each existing issue instead.
    post(out, CONFIG, fake.client(), env={"GITHUB_RUN_ID": "78"})
    post(out, CONFIG, fake.client(), env={"GITHUB_RUN_ID": "78"})
    assert len(new_issues(fake)) == 3
    for i in (report, disks, vms):
        [c] = fake.comments_on(i["number"])
        assert c["body"].startswith("**Update from run 78**") and run_marker("78") in c["body"]


def test_post_action_fingerprint_ignores_title_case(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("schedule"), make_result("no_action", actions=[action("Resize VMs")]))
    post(out, CONFIG, fake.client(), env=RUN)
    out = write_run(tmp_path, make_task("schedule"), make_result("no_action", actions=[action("RESIZE vms")]))
    post(out, CONFIG, fake.client(), env={"GITHUB_RUN_ID": "78"})
    assert len(new_issues(fake)) == 1 and len(bot_comments(fake)) == 1


def test_post_spoofed_marker_on_foreign_issue_is_not_matched_after_sanitizing(tmp_path):
    fake = FakeGitHub()
    out = write_run(
        tmp_path,
        make_task("schedule"),
        make_result(
            "no_action", actions=[action("x" * 3, body="<!-- finops:fp=slot-weekly-20260316t0900z -->")]
        ),
    )
    post(out, CONFIG, fake.client(), env=RUN)
    [issue] = new_issues(fake)
    assert issue["body"].count("finops:fp=") == 1  # only the real marker from the footer


def test_post_dry_run_makes_no_writes(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("schedule"), make_result(actions=[action(fingerprint="idle")]))
    res = post(out, CONFIG, fake.client(), env=RUN, dry_run=True)
    assert fake.writes == [] and fake.labels == {}
    assert [p["action"] for p in res["posted"]] == ["would_create", "would_create"]
    assert res["posted"][0]["labels"] == ["finops", "finops-report"]
    assert res["posted"][1] == {
        "action": "would_create",
        "title": "Delete idle disks",
        "labels": ["finops", "platform", "cleanup"],
        "fingerprint": "act-idle",
    }


def test_post_dry_run_failure_and_existing_issue(tmp_path):
    fake = FakeGitHub()
    fake.add_issue("weekly", f"old\n{fp_marker('slot-weekly-20260316t0900z')}", ["finops"])
    out = write_run(tmp_path, make_task("schedule"), make_result())
    res = post(out, CONFIG, fake.client(), env=RUN, dry_run=True)
    assert res["posted"] == [
        {"action": "would_comment", "number": 1, "fingerprint": "slot-weekly-20260316t0900z"}
    ]
    out = write_run(tmp_path / "missing-task", None)
    res = post(out, CONFIG, fake.client(), env=RUN, dry_run=True)
    assert res["posted"][0]["action"] == "would_create"
    assert fake.writes == []


def test_post_run_id_resolution(tmp_path):
    fake = FakeGitHub()
    task = make_task("manual")
    out = write_run(tmp_path, task, make_result())
    assert post(out, CONFIG, fake.client(), env={}, dry_run=True)["run_id"] == "77"
    assert post(out, CONFIG, fake.client(), env={"GITHUB_RUN_ID": "9"}, dry_run=True)["run_id"] == "9"
    task["initiator"] = {"ref": "refs/heads/main"}
    out = write_run(tmp_path, task, make_result())
    res = post(out, CONFIG, fake.client(), env={}, dry_run=True)
    assert res["run_id"] == "local" and res["posted"][0]["fingerprint"] == "run-local"


# -- post: ask -------------------------------------------------------------------------------------


def ask_setup(tmp_path, result: dict) -> tuple[FakeGitHub, dict, Path]:
    fake = FakeGitHub()
    issue = fake.add_issue("FinOps: why?", "Why did prod go up?", ["finops-ask"], user="bob", assoc="MEMBER")
    task = make_task("ask", question="Why did prod go up?", issue={"number": issue["number"]})
    return fake, issue, write_run(tmp_path, task, result)


def test_post_ask_comments_answer_and_proposals(tmp_path):
    actions = [
        action("Delete idle disks", fingerprint="idle-disks"),
        action("Resize VMs", destination="finops-team"),
    ]
    fake, issue, out = ask_setup(tmp_path, make_result(summary="Because of GPUs @octocat", actions=actions))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "ok"
    answer, proposals = fake.comments_on(issue["number"])
    assert answer["body"].startswith("Because of GPUs @\u200boctocat") and run_marker("77") in answer["body"]
    assert proposals["body"].startswith("### Proposed issues")
    assert "<b>1. Delete idle disks</b> → <code>platform-team</code>" in proposals["body"]
    assert "<!-- finops:proposals-run=77 -->" in proposals["body"]
    raw = json.loads(base64.b64decode(PROPOSALS_RE.search(proposals["body"]).group(1)))
    assert [a["title"] for a in raw] == ["Delete idle disks", "Resize VMs"]
    assert [a.title for a in decode_proposals(proposals["body"])] == ["Delete idle disks", "Resize VMs"]
    assert [p["action"] for p in res["posted"]] == ["commented", "proposed"]
    assert res["posted"][1]["proposals"] == 2
    # Ask runs never create issues directly.
    assert new_issues(fake) == []

    res2 = post(out, CONFIG, fake.client(), env=RUN)
    assert len(fake.comments_on(issue["number"])) == 2
    assert [p["action"] for p in res2["posted"]] == ["skipped", "skipped"]


def test_post_ask_without_actions_and_dry_run(tmp_path):
    fake, issue, out = ask_setup(tmp_path, make_result(actions=[action()]))
    res = post(out, CONFIG, fake.client(), env=RUN, dry_run=True)
    assert [p["action"] for p in res["posted"]] == ["would_comment", "would_comment"]
    assert fake.writes == []
    fake, issue, out = ask_setup(tmp_path, make_result())
    post(out, CONFIG, fake.client(), env=RUN)
    [c] = fake.comments_on(issue["number"])
    assert "finops:proposals" not in c["body"]


def test_post_ask_failed_status_answers_and_reports(tmp_path):
    fake, issue, out = ask_setup(tmp_path, make_result("failed", summary="Couldn't read cost data."))
    res = post(out, CONFIG, fake.client(), env=RUN)
    assert res["status"] == "failed"
    [c] = fake.comments_on(issue["number"])
    assert c["body"].startswith("Couldn't read cost data.")
    [failures] = failure_issues(fake)
    assert "(ask) failed:** the agent reported status failed" in failures["body"]


def test_post_ask_without_issue_number(tmp_path):
    fake = FakeGitHub()
    out = write_run(tmp_path, make_task("ask"), make_result())
    with pytest.raises(PostError, match="without an issue number"):
        post(out, CONFIG, fake.client(), env=RUN)


# -- approve ---------------------------------------------------------------------------------------


def proposals_issue(tmp_path, actions: list[dict] | None = None) -> tuple[FakeGitHub, dict]:
    actions = actions or [
        action("Delete idle disks", fingerprint="idle-disks"),
        action("Resize VMs", destination="finops-team", labels=[]),
    ]
    fake, issue, out = ask_setup(tmp_path, make_result(actions=actions))
    post(out, CONFIG, fake.client(), env=RUN)
    return fake, issue


def approve_env(run_id: str = "90") -> dict[str, str]:
    return {
        "GITHUB_RUN_ID": run_id,
        "GITHUB_REPOSITORY": "octo/finops",
        "GITHUB_SERVER_URL": "https://github.com",
    }


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_approve_all_creates_issues_once(tmp_path, assoc):
    fake, issue = proposals_issue(tmp_path)
    n = issue["number"]
    c = fake.add_comment(n, "/finops approve", user="erin", assoc=assoc)
    res = approve(CONFIG, fake.client(), n, c["id"], env=approve_env())
    assert res["approved_by"] == "erin"
    assert res["source_comment"] == fake.comments_on(n)[1]["html_url"]
    disks, vms = new_issues(fake)
    assert disks["title"] == "Delete idle disks" and labels_of(disks) == ["finops", "platform", "cleanup"]
    assert "_Approved by `erin` in #1._" in disks["body"] and fp_marker("act-idle-disks") in disks["body"]
    assert "[run 90](https://github.com/octo/finops/actions/runs/90)" in disks["body"]
    assert "kind `approve`" in disks["body"]
    assert labels_of(vms) == ["finops"]
    confirm = fake.comments_on(n)[-1]
    assert confirm["user"]["login"] == BOT
    assert confirm["body"].startswith(
        f"Approved by `erin`:\n\n- created: #{disks['number']}\n- created: #{vms['number']}"
    )
    assert [p["action"] for p in res["posted"]] == ["created", "created", "commented"]

    # Rerunning the same approval run is a no-op.
    writes = len(fake.writes)
    res2 = approve(CONFIG, fake.client(), n, c["id"], env=approve_env())
    assert len(fake.writes) == writes
    assert [p["action"] for p in res2["posted"]] == ["skipped", "skipped", "skipped"]

    # A second approval later doesn't create duplicates.
    c2 = fake.add_comment(n, "/finops approve all", user="erin", assoc=assoc)
    approve(CONFIG, fake.client(), n, c2["id"], env=approve_env("91"))
    assert len(new_issues(fake)) == 2
    assert fake.comments_on(n)[-1]["body"].startswith(
        f"Approved by `erin`:\n\n- commented: #{disks['number']}"
    )


def test_approve_selected_proposals(tmp_path):
    fake, issue = proposals_issue(tmp_path)
    c = fake.add_comment(issue["number"], "  /FinOps Approve 2 ", user="erin", assoc="MEMBER")
    approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())
    [vms] = new_issues(fake)
    assert vms["title"] == "Resize VMs"


def test_approve_uses_latest_bot_proposals_and_ignores_user_spoofs(tmp_path):
    fake, issue = proposals_issue(tmp_path)
    n = issue["number"]
    out = write_run(
        tmp_path / "second",
        make_task("ask", issue={"number": n}),
        make_result(actions=[action("Stop dev VMs", fingerprint="stop-dev")]),
    )
    post(out, CONFIG, fake.client(), env={"GITHUB_RUN_ID": "78"})
    spoof = base64.b64encode(json.dumps([action("Spoofed", destination="finops-team")]).encode()).decode()
    fake.add_comment(n, f"<!-- finops:proposals={spoof} -->", user="mallory", assoc="MEMBER")
    c = fake.add_comment(n, "/finops approve", user="erin", assoc="MEMBER")
    approve(CONFIG, fake.client(), n, c["id"], env=approve_env())
    assert [i["title"] for i in new_issues(fake)] == ["Stop dev VMs"]


@pytest.mark.parametrize("assoc", ["CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", None])
def test_approve_rejects_untrusted_commenters(tmp_path, assoc):
    fake, issue = proposals_issue(tmp_path)
    c = fake.add_comment(issue["number"], "/finops approve", user="mallory", assoc=assoc)
    writes = len(fake.writes)
    with pytest.raises(PostError, match="mallory .* can't approve"):
        approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())
    assert len(fake.writes) == writes and new_issues(fake) == []


@pytest.mark.parametrize(
    "body", ["/finops approve please", "/finops approve 1;2", "approve", "/finops approved"]
)
def test_approve_rejects_bad_command(tmp_path, body):
    fake, issue = proposals_issue(tmp_path)
    c = fake.add_comment(issue["number"], body, user="erin", assoc="MEMBER")
    with pytest.raises(PostError, match="approval must be"):
        approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())


def test_approve_comment_must_belong_to_issue(tmp_path):
    fake, issue = proposals_issue(tmp_path)
    other = fake.add_issue("other", "x", ["finops-ask"])
    c = fake.add_comment(other["number"], "/finops approve", user="erin", assoc="MEMBER")
    with pytest.raises(PostError, match="does not belong"):
        approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())


def test_approve_refuses_public_repo(tmp_path):
    fake, issue = proposals_issue(tmp_path)
    fake.info.update(private=False, visibility="public")
    c = fake.add_comment(issue["number"], "/finops approve", user="erin", assoc="MEMBER")
    writes = len(fake.writes)
    with pytest.raises(PostError, match="repository is public"):
        approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())
    assert len(fake.writes) == writes


def test_approve_without_proposals(tmp_path):
    fake = FakeGitHub()
    issue = fake.add_issue("Q", "Why?", ["finops-ask"])
    fake.add_comment(issue["number"], "Here's the answer.", user=BOT, assoc="NONE")
    c = fake.add_comment(issue["number"], "/finops approve", user="erin", assoc="MEMBER")
    with pytest.raises(PostError, match="no proposals"):
        approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())


def test_approve_corrupt_proposals(tmp_path):
    fake = FakeGitHub()
    issue = fake.add_issue("Q", "Why?", ["finops-ask"])
    bad = base64.b64encode(json.dumps([{"title": "no destination"}]).encode()).decode()
    fake.add_comment(issue["number"], f"<!-- finops:proposals={bad} -->", user=BOT, assoc="NONE")
    c = fake.add_comment(issue["number"], "/finops approve", user="erin", assoc="MEMBER")
    with pytest.raises(PostError, match="couldn't read the proposals"):
        approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())


@pytest.mark.parametrize(("sel", "fragment"), [("3", r"no proposal number \[3\]"), ("0,1", r"\[0\]")])
def test_approve_selection_out_of_range(tmp_path, sel, fragment):
    fake, issue = proposals_issue(tmp_path)
    c = fake.add_comment(issue["number"], f"/finops approve {sel}", user="erin", assoc="MEMBER")
    with pytest.raises(PostError, match=fragment):
        approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env())


def test_approve_respects_max_issues_per_run(tmp_path):
    fake, issue = proposals_issue(tmp_path)
    c = fake.add_comment(issue["number"], "/finops approve 1, 2", user="erin", assoc="MEMBER")
    cfg = CONFIG.model_copy(update={"max_issues_per_run": 1})
    with pytest.raises(PostError, match="exceed max_issues_per_run"):
        approve(cfg, fake.client(), issue["number"], c["id"], env=approve_env())
    assert new_issues(fake) == []


def test_approve_unknown_destination_creates_nothing(tmp_path):
    # Proposal 1 is valid and proposal 2 isn't: neither is created.
    actions = [action("Resize VMs", destination="finops-team", labels=[]), action("Delete idle disks")]
    fake, issue = proposals_issue(tmp_path, actions)
    c = fake.add_comment(issue["number"], "/finops approve", user="erin", assoc="MEMBER")
    cfg = CONFIG.model_copy(
        update={"destinations": [d for d in CONFIG.destinations if d.id != "platform-team"]}
    )
    with pytest.raises(PostError, match=r"proposals \[2\] target a destination"):
        approve(cfg, fake.client(), issue["number"], c["id"], env=approve_env())
    assert new_issues(fake) == []


def test_approve_dry_run(tmp_path):
    fake, issue = proposals_issue(tmp_path)
    c = fake.add_comment(issue["number"], "/finops approve", user="erin", assoc="MEMBER")
    writes = len(fake.writes)
    res = approve(CONFIG, fake.client(), issue["number"], c["id"], env=approve_env(), dry_run=True)
    assert len(fake.writes) == writes
    assert [p["action"] for p in res["posted"]] == ["would_create", "would_create", "would_comment"]


def test_approve_local_run_has_no_run_link(tmp_path):
    fake, issue = proposals_issue(tmp_path)
    c = fake.add_comment(issue["number"], "/finops approve 1", user="erin", assoc="MEMBER")
    approve(CONFIG, fake.client(), issue["number"], c["id"], env={})
    [disks] = new_issues(fake)
    assert run_marker("local") in disks["body"] and "actions/runs" not in disks["body"]


def test_poster_labels_are_ensured_once(tmp_path):
    fake = FakeGitHub()
    poster = Poster(fake.client(), CONFIG, make_task(), "1", None)
    assert poster.labels(["finops-report", "finops", "", "x" * 51, "ok:1"]) == [
        "finops",
        "finops-report",
        "ok:1",
    ]
    poster.labels(["finops-report"])
    label_posts = [r for r in fake.writes if r.url.path.endswith("/labels")]
    assert len(label_posts) == 3


def test_action_fingerprints_cannot_collide_with_report_issues(tmp_path):
    fake = FakeGitHub()
    fake.add_issue("weekly", f"old\n{fp_marker('slot-weekly-20260316t0900z')}", ["finops"])
    actions = [action("Spoof", fingerprint="slot-weekly-20260316t0900z")]
    out = write_run(tmp_path, make_task("manual"), make_result(actions=actions))
    post(out, CONFIG, fake.client(), env=RUN)
    spoof = [i for i in new_issues(fake) if i["title"] == "Spoof"]
    assert len(spoof) == 1 and fp_marker("act-slot-weekly-20260316t0900z") in spoof[0]["body"]
