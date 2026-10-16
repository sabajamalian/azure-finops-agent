import json
import os
from pathlib import Path

import pytest
import yaml
from fake_github import FakeGitHub

from azure_finops import __version__, cli
from azure_finops.post import FAILURES_MARKER
from azure_finops.simulator.synthetic import synthetic_config

REAL_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REAL_ROOT / "finops" / "config.example.yaml"
CLEAR = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_REPOSITORY",
    "GITHUB_OUTPUT",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_REF_NAME",
    "GITHUB_RUN_ID",
    "GITHUB_SERVER_URL",
    "FINOPS_DISPATCH_REF",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k in CLEAR or k.startswith("INPUT_"):
            monkeypatch.delenv(k)


@pytest.fixture
def configured(repo) -> Path:
    (repo / "finops" / "config.yaml").write_text(yaml.safe_dump(synthetic_config().model_dump(mode="json")))
    return repo


@pytest.fixture
def gh_out(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "github_output"
    p.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(p))
    return p


@pytest.fixture
def fake(monkeypatch) -> FakeGitHub:
    f = FakeGitHub()
    monkeypatch.setattr(cli, "_gh", lambda required=True: f.client())
    return f


def outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def write_automation(root: Path, name: str, **kw) -> None:
    data = {
        "name": name,
        "trigger": {"type": "schedule", "schedule": {"natural": "Mondays 9 UTC", "cron": "0 9 * * 1"}},
        "instructions": "Summarize weekly spend.",
        "destinations": ["finops-team"],
        **kw,
    }
    (root / "finops" / "automations" / f"{name}.yaml").write_text(yaml.safe_dump(data))


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"finops {__version__}"


def test_subcommand_required(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


# -- unconfigured template repo --------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["dispatch"],
        ["dispatch", "--dry-run", "--now", "2026-03-16T10:00:00+00:00"],
        ["detect", "--output", "detection.json"],
        ["anomaly-dispatch", "--anomalies", "does-not-exist.json"],
    ],
)
def test_unconfigured_repo_exits_ok(repo, capsys, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert f"{repo / 'finops' / 'config.yaml'} not found; nothing to do" in out
    assert not (repo / "detection.json").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["automations", "validate"],
        ["config"],
        ["task", "prepare", "--output-dir", "out"],
        ["post", "--output-dir", "out"],
    ],
)
def test_other_commands_need_config(repo, capsys, argv, monkeypatch):
    monkeypatch.chdir(repo)
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "config.yaml not found" in err


def test_dispatch_requires_github_credentials(configured):
    with pytest.raises(SystemExit) as exc:
        cli.main(["dispatch"])
    assert exc.value.code == "error: set GITHUB_TOKEN (or GH_TOKEN) and GITHUB_REPOSITORY"


def test_gh_reads_token_and_repo(monkeypatch):
    assert cli._gh(required=False) is None
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/finops")
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example/api/v3/")
    gh = cli._gh()
    assert gh.repo == "octo/finops"


def test_ref(monkeypatch):
    assert cli._ref() == "main"
    monkeypatch.setenv("FINOPS_DISPATCH_REF", "release")
    assert cli._ref() == "release"
    monkeypatch.setenv("GITHUB_REF_NAME", "trunk")
    assert cli._ref() == "trunk"


# -- task ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inputs", "fragment"),
    [
        ({"INPUT_KIND": "deploy"}, "kind must be one of"),
        ({"INPUT_KIND": "manual"}, "question or an automation"),
        ({"INPUT_KIND": "schedule", "INPUT_AUTOMATION": "nope"}, "automation 'nope' not found"),
        ({"INPUT_KIND": "ask", "INPUT_ISSUE": "1"}, "GITHUB_TOKEN"),
        ({"INPUT_KIND": "anomaly", "INPUT_AUTOMATION": "weekly", "INPUT_ANOMALY": "{"}, "anomaly automation"),
    ],
)
def test_task_prepare_error_writes_prepare_error_json(
    configured, gh_out, monkeypatch, capsys, inputs, fragment
):
    write_automation(configured, "weekly")
    for k, v in inputs.items():
        monkeypatch.setenv(k, v)
    out = configured / "out"
    assert cli.main(["task", "prepare", "--output-dir", str(out)]) == 2
    err = json.loads((out / "prepare-error.json").read_text())["error"]
    assert fragment in err
    assert f"error: {err}" in capsys.readouterr().err
    assert not (out / "task.json").exists()
    assert outputs(gh_out) == {"retention_days": "30"}


def test_task_prepare_success(configured, gh_out, monkeypatch, capsys):
    monkeypatch.setenv("INPUT_KIND", "manual")
    monkeypatch.setenv("INPUT_QUESTION", "How much did prod cost?")
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    out = configured / "out"
    assert cli.main(["task", "prepare", "--output-dir", str(out)]) == 0
    assert outputs(gh_out) == {"retention_days": "30", "kind": "manual", "task_id": "manual-555"}
    assert json.loads((out / "task.json").read_text())["question"] == "How much did prod cost?"
    assert (out / "prompt.md").exists() and not (out / "prepare-error.json").exists()
    assert "prepared manual-555: 2 subscription(s)" in capsys.readouterr().out


def test_task_finalize_prints_run_record(tmp_path, capsys):
    out = tmp_path / "run"
    assert cli.main(["task", "finalize", "--output-dir", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads((out / "run.json").read_text())
    assert printed["result_status"] == "failed"


# -- post / approve --------------------------------------------------------------------------------


def test_post_reports_prepare_error(configured, fake, monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_RUN_ID", "88")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/finops")
    out = configured / "out"
    out.mkdir()
    (out / "prepare-error.json").write_text(json.dumps({"error": "invalid slot 'x' for automation 'weekly'"}))
    assert cli.main(["post", "--output-dir", str(out)]) == 1
    [issue] = fake.issues.values()
    assert FAILURES_MARKER in issue["body"]
    assert "**Run 88 (prepare) failed:** the task could not be prepared" in issue["body"]
    assert "invalid slot 'x' for automation 'weekly'" in issue["body"]
    assert "https://github.com/octo/finops/actions/runs/88" in issue["body"]
    res = json.loads((out / "post.json").read_text())
    assert res["status"] == "failed" and res["posted"][0]["action"] == "created"
    assert summary.read_text().startswith("### FinOps post: failed")
    assert json.loads(capsys.readouterr().out) == res


def test_post_dry_run_ok(configured, fake, capsys):
    out = configured / "out"
    out.mkdir()
    task = {
        "kind": "manual",
        "question": "q?",
        "allowed_destinations": ["finops-team"],
        "initiator": {"ref": "refs/heads/main", "run_id": "1"},
    }
    (out / "task.json").write_text(json.dumps(task))
    (out / "result.json").write_text(json.dumps({"status": "no_action", "summary": "Nothing to report."}))
    assert cli.main(["post", "--output-dir", str(out), "--dry-run"]) == 0
    assert fake.writes == []
    assert json.loads((out / "post.json").read_text())["posted"][0]["action"] == "would_create"


@pytest.mark.parametrize("dry_run", [False, True])
def test_approve_error_comments_unless_dry_run(configured, fake, capsys, dry_run):
    issue = fake.add_issue("Q", "Why?", ["finops-ask"])
    c = fake.add_comment(issue["number"], "/finops approve", user="mallory", assoc="CONTRIBUTOR")
    argv = ["approve", "--issue", str(issue["number"]), "--comment-id", str(c["id"])]
    assert cli.main(argv + (["--dry-run"] if dry_run else [])) == 1
    assert "can't approve" in capsys.readouterr().err
    comments = [x for x in fake.comments_on(issue["number"]) if x["id"] != c["id"]]
    if dry_run:
        assert comments == []
    else:
        [reply] = comments
        assert reply["body"].startswith("Couldn't approve: mallory (CONTRIBUTOR) can't approve")


# -- dispatchers -----------------------------------------------------------------------------------


def test_dispatch_command(configured, fake, monkeypatch, tmp_path, capsys):
    write_automation(configured, "weekly")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert cli.main(["dispatch", "--now", "2026-03-16T03:30:00-07:00"]) == 0
    assert [d["inputs"]["slot"] for d in fake.dispatches] == ["weekly@20260316T0900Z"]
    assert fake.dispatches[0]["ref"] == "main"
    assert summary.read_text() == "### FinOps schedule dispatch\n\n- weekly@20260316T0900Z: dispatched\n"
    assert json.loads(capsys.readouterr().out)[0]["action"] == "dispatched"

    assert cli.main(["dispatch", "--now", "2026-03-17T10:00:00+00:00", "--dry-run"]) == 0
    assert summary.read_text().endswith("- nothing due\n")


def test_detect_and_anomaly_dispatch_commands(configured, fake, monkeypatch, tmp_path, capsys):
    write_automation(
        configured,
        "spikes",
        trigger={"type": "anomaly", "filter": {"min_impact": 100}},
        destinations=["finops-team"],
    )
    monkeypatch.setenv("FINOPS_FIXTURE_MODE", "synthetic")
    detection = tmp_path / "detection.json"
    assert cli.main(["detect", "--output", str(detection), "--output-dir", str(tmp_path / "det")]) == 0
    res = json.loads(detection.read_text())
    assert res["status"] == "ok" and len(res["anomalies"]) == 2
    assert "2 anomalies, status ok" in capsys.readouterr().out
    assert (tmp_path / "det" / "evidence.jsonl").exists()

    assert cli.main(["anomaly-dispatch", "--anomalies", str(detection), "--dry-run"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [p["action"] for p in printed] == ["would_dispatch", "would_dispatch"]
    assert fake.dispatches == []


# -- automations -----------------------------------------------------------------------------------


def test_automations_validate_example_config(monkeypatch, capsys):
    monkeypatch.setenv("FINOPS_REPO_ROOT", str(REAL_ROOT))
    monkeypatch.setenv("FINOPS_CONFIG", str(EXAMPLE_CONFIG))
    assert cli.main(["automations", "validate"]) == 0
    n = len([p for p in (REAL_ROOT / "finops" / "automations").glob("*.y*ml") if not p.name.startswith("_")])
    assert n > 0
    assert capsys.readouterr().out.strip() == f"{n} automation(s) valid"


def test_automations_validate_reports_errors(configured, capsys):
    write_automation(configured, "weekly", destinations=["nowhere"])
    (configured / "finops" / "automations" / "broken.yaml").write_text("name: other\n")
    assert cli.main(["automations", "validate"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("invalid automations:")
    assert "broken.yaml" in err and "destination 'nowhere'" in err


def test_automations_list_json(configured, fake, capsys):
    write_automation(configured, "weekly")
    write_automation(configured, "paused", enabled=False)
    fake.add_run("finops schedule weekly weekly@20260316T0900Z", conclusion="success")
    assert cli.main(["automations", "list", "--json"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["weekly"]["file"] == "finops/automations/weekly.yaml"
    assert rows["weekly"]["schedule"] == "Mondays 9 UTC (cron '0 9 * * 1' UTC)"
    assert len(rows["weekly"]["next_runs"]) == 3
    assert rows["weekly"]["last_triggered"].endswith("(success)")
    assert rows["paused"]["next_runs"] == [] and rows["paused"]["last_triggered"] is None


# -- maintenance -----------------------------------------------------------------------------------


def test_evals_list(tmp_path, capsys):
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "cases": [{"id": "one", "question": "What changed?"}, {"id": "two", "question": "Why?"}],
            }
        )
    )
    assert cli.main(["evals", "list", "--cases", str(cases)]) == 0
    assert capsys.readouterr().out == "one: What changed?\ntwo: Why?\n"


def test_schemas_command(repo, capsys):
    assert cli.main(["schemas", "export", "--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    assert cli.main(["schemas", "export"]) == 0
    assert cli.main(["schemas", "export", "--check"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "schemas are current"


def test_check_agent_profile_command(repo, monkeypatch, capsys):
    assert cli.main(["check-agent-profile"]) == 1
    assert "azure-finops.agent.md not found" in capsys.readouterr().err
    monkeypatch.setenv("FINOPS_REPO_ROOT", str(REAL_ROOT))
    assert cli.main(["check-agent-profile"]) == 0
    assert capsys.readouterr().out.strip() == "agent profile OK"


def test_config_command(configured, capsys):
    assert cli.main(["config", "tenant"]) == 0
    assert capsys.readouterr().out.strip() == synthetic_config().azure.tenant_id
    assert cli.main(["config", "subscriptions"]) == 0
    assert capsys.readouterr().out.split() == synthetic_config().subscription_ids()
    assert cli.main(["config"]) == 0
    assert json.loads(capsys.readouterr().out)["max_issues_per_run"] == 5
