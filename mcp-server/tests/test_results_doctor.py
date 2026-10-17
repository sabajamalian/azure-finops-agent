import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import DEV_SCOPE, PROD_SCOPE
from fake_github import API, FakeGitHub

import azure_finops.doctor as doctor
import azure_finops.github as github_mod
from azure_finops import cli
from azure_finops.doctor import Check, render, run_doctor
from azure_finops.github import GitHub
from azure_finops.results import (
    MAX_SUMMARY_CHARS,
    Result,
    load_task,
    submit_result,
    validate_result,
    validate_result_data,
)
from azure_finops.schemas import export
from azure_finops.simulator.synthetic import PROD, synthetic_config

REAL_ROOT = Path(__file__).resolve().parents[2]
CONFIG = synthetic_config()


def action(dest: str = "finops-team", **kw) -> dict:
    return {"destination": dest, "title": "Delete idle disk", "body": "It costs money.", **kw}


def finding(*ids: str, **kw) -> dict:
    return {"title": "GPU VM added", "confidence": "high", "evidence_ids": list(ids), **kw}


def result(**kw) -> dict:
    return {"status": "ok", "summary": "Spend went up.", **kw}


# -- validate_result_data --------------------------------------------------------------------------


def test_minimal_result_is_valid():
    res, errors = validate_result_data(result(), CONFIG, set())
    assert errors == []
    assert isinstance(res, Result)
    assert res.version == 1 and res.findings == [] and res.actions == []


def test_full_result_is_valid():
    data = result(
        evidence_ids=["ev-001"],
        findings=[finding("ev-002", impact={"amount": 1180.5, "currency": "USD", "period": "day"})],
        actions=[action(evidence_ids=["ev-001"], fingerprint="gpu-vm", labels=["cost"])],
    )
    res, errors = validate_result_data(data, CONFIG, {"ev-001", "ev-002"}, {"finops-team"})
    assert errors == []
    assert res.actions[0].type == "create_issue"
    assert res.findings[0].impact.amount == 1180.5


@pytest.mark.parametrize("label", ["finops-ask", "FINOPS-FAILURE", "finops-report", " finops-anomaly"])
def test_actions_cannot_use_reserved_labels(label):
    res, errors = validate_result_data(result(actions=[action(labels=["cost", label])]), CONFIG, set())
    assert res is None
    assert len(errors) == 1 and "reserved for the workflows" in errors[0]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"status": "maybe", "summary": "x"}, "status: Input should be 'ok', 'no_action' or 'failed'"),
        ({"status": "ok", "summary": ""}, "summary: String should have at least 1 character"),
        ({"status": "ok"}, "summary: Field required"),
        (result(extra=1), "extra: Extra inputs are not permitted"),
        (result(version=2), "version: Input should be 1"),
        (
            result(summary="x" * (MAX_SUMMARY_CHARS + 1)),
            "summary: String should have at most 60000 characters",
        ),
        (result(actions=[{"title": "abc", "body": "b"}]), "actions.0.destination: Field required"),
        (
            result(actions=[action(fingerprint="Bad FP")]),
            "actions.0.fingerprint: String should match pattern",
        ),
        (result(actions=[action(type="close_issue")]), "actions.0.type: Input should be 'create_issue'"),
        (
            result(actions=[action(labels=[str(i) for i in range(11)])]),
            "actions.0.labels: List should have at most",
        ),
        (result(findings=[finding()]), "findings.0.evidence_ids: List should have at least 1 item"),
        (result(findings=[finding("ev-001", confidence="sure")]), "findings.0.confidence: Input should be"),
        (
            result(findings=[finding("ev-001", impact={"amount": 1, "currency": "usd", "period": "day"})]),
            "findings.0.impact.currency: String should match pattern",
        ),
        (result(actions=[action()] * 26), "actions: List should have at most 25 items"),
    ],
)
def test_schema_errors_are_flattened(data, expected):
    res, errors = validate_result_data(data, CONFIG, {"ev-001"})
    assert res is None
    assert any(e.startswith(expected) for e in errors), errors


def test_unknown_evidence_ids_from_every_level():
    data = result(
        evidence_ids=["ev-001", "ev-009"],
        findings=[finding("ev-007", "ev-001")],
        actions=[action(evidence_ids=["ev-008"])],
    )
    res, errors = validate_result_data(data, CONFIG, {"ev-001"})
    assert res is None
    assert errors == [
        "unknown evidence ids ['ev-007', 'ev-008', 'ev-009']; cite only ids returned by FinOps tools in this session"
    ]


def test_destination_must_be_configured():
    res, errors = validate_result_data(result(actions=[action("ghost")]), CONFIG, set())
    assert res is None
    assert errors == [
        "actions[0].destination 'ghost' is not allowed; allowed: ['finops-team', 'platform-team']"
    ]


def test_destination_must_be_allowed_by_the_task():
    data = result(actions=[action("finops-team"), action("platform-team")])
    res, errors = validate_result_data(data, CONFIG, set(), {"finops-team", "ghost"})
    assert res is None
    # Unconfigured ids in the task's list don't widen the allowed set.
    assert errors == ["actions[1].destination 'platform-team' is not allowed; allowed: ['finops-team']"]


@pytest.mark.parametrize("allowed", [None, set()])
def test_no_task_destinations_means_all_configured(allowed):
    data = result(actions=[action("finops-team"), action("platform-team")])
    assert validate_result_data(data, CONFIG, set(), allowed)[1] == []


def test_actions_capped_by_max_issues_per_run():
    assert validate_result_data(result(actions=[action()] * 5), CONFIG, set())[1] == []
    res, errors = validate_result_data(result(actions=[action()] * 6), CONFIG, set())
    assert res is None
    assert errors == ["6 actions exceed max_issues_per_run (5)"]
    lower = CONFIG.model_copy(update={"max_issues_per_run": 1})
    assert validate_result_data(result(actions=[action()] * 2), lower, set())[1] == [
        "2 actions exceed max_issues_per_run (1)"
    ]


def test_semantic_errors_accumulate():
    data = result(evidence_ids=["ev-404"], actions=[action("ghost")] * 6)
    _, errors = validate_result_data(data, CONFIG, set())
    assert len(errors) == 1 + 6 + 1
    assert errors[0].startswith("unknown evidence ids ['ev-404']")
    assert errors[-1] == "6 actions exceed max_issues_per_run (5)"


# -- load_task / validate_result / submit_result ---------------------------------------------------


def test_load_task(tmp_path):
    assert load_task(None) == {}
    assert load_task(tmp_path) == {}
    (tmp_path / "task.json").write_text(json.dumps({"kind": "manual"}))
    assert load_task(tmp_path) == {"kind": "manual"}


def test_validate_result_uses_runtime_evidence_and_task(rt_out):
    ev = rt_out.evidence.new(tool="get_costs", source="cost_management", scope=[PROD_SCOPE])
    assert ev.id == "ev-001"
    data = result(evidence_ids=["ev-001"], actions=[action("platform-team")])
    assert validate_result(rt_out, data)[1] == []
    (rt_out.output_dir / "task.json").write_text(json.dumps({"allowed_destinations": ["finops-team"]}))
    assert validate_result(rt_out, data)[1] == [
        "actions[0].destination 'platform-team' is not allowed; allowed: ['finops-team']"
    ]


def test_submit_result_interactive_writes_nothing(rt, repo):
    ev = rt.evidence.new(tool="get_costs", source="cost_management", scope=[DEV_SCOPE])
    res = submit_result(rt, result(evidence_ids=[ev.id], actions=[action()]))
    assert res["status"] == "valid" and res["written"] is False
    assert "nothing was written or posted" in res["note"]
    assert list(repo.rglob("result.json")) == []
    assert rt.audit.records[-1]["event"] == "submit_result"
    assert rt.audit.records[-1]["valid"] is True


def test_submit_result_headless_writes_result_json(rt_out):
    ev = rt_out.evidence.new(tool="get_costs", source="cost_management", scope=[PROD_SCOPE])
    (rt_out.output_dir / "task.json").write_text(json.dumps({"allowed_destinations": ["finops-team"]}))
    data = result(findings=[finding(ev.id)], actions=[action(fingerprint="gpu-vm", evidence_ids=[ev.id])])
    res = submit_result(rt_out, data)
    path = rt_out.output_dir / "result.json"
    assert res == {
        "status": "accepted",
        "written": True,
        "path": str(path),
        "actions": 1,
        "note": "The post job validates this again and performs any approved actions. You are done.",
    }
    written = json.loads(path.read_text())
    assert written["version"] == 1 and written["actions"][0]["type"] == "create_issue"
    assert Result.model_validate(written).actions[0].fingerprint == "gpu-vm"
    audit = [json.loads(x) for x in (rt_out.output_dir / "audit.jsonl").read_text().splitlines()]
    assert [a for a in audit if a["event"] == "submit_result"][-1]["valid"] is True


def test_submit_result_invalid_writes_nothing(rt_out):
    res = submit_result(rt_out, result(evidence_ids=["ev-001"]))
    assert res["status"] == "invalid"
    assert res["errors"][0].startswith("unknown evidence ids ['ev-001']")
    assert res["hint"] == "Fix the errors and call submit_result again."
    assert not (rt_out.output_dir / "result.json").exists()
    rec = rt_out.audit.records[-1]
    assert rec["event"] == "submit_result" and rec["valid"] is False and rec["errors"] == res["errors"]


def test_resubmit_replaces_result(rt_out):
    submit_result(rt_out, result(summary="first"))
    submit_result(rt_out, result(summary="second", status="no_action"))
    written = json.loads((rt_out.output_dir / "result.json").read_text())
    assert (written["summary"], written["status"]) == ("second", "no_action")


# -- doctor ----------------------------------------------------------------------------------------


@pytest.fixture
def which(monkeypatch):
    found = {"azure-finops-mcp", "copilot", "az", "npx"}
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd in found else None)
    monkeypatch.setattr(doctor, "_az_tenant", lambda: None)
    return found


@pytest.fixture
def healthy(repo, which):
    """A repo where every local check passes: config, automation, agent profile, and schemas."""
    (repo / "finops" / "config.yaml").write_text(yaml.safe_dump(CONFIG.model_dump(mode="json")))
    auto = {
        "name": "weekly",
        "trigger": {"type": "schedule", "schedule": {"natural": "Mondays 9 UTC", "cron": "0 9 * * 1"}},
        "instructions": "Summarize weekly spend.",
        "destinations": ["finops-team"],
    }
    (repo / "finops" / "automations" / "weekly.yaml").write_text(yaml.safe_dump(auto))
    (repo / "finops" / "automations" / "paused.yaml").write_text(
        yaml.safe_dump({**auto, "name": "paused", "enabled": False})
    )
    for rel in (".github/agents/azure-finops.agent.md", "finops/allowlist.yaml", ".vscode/mcp.json"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL_ROOT / rel, repo / rel)
    venv_exe = repo / ".venv" / "bin" / "azure-finops-mcp"
    venv_exe.parent.mkdir(parents=True)
    venv_exe.touch()
    export(repo)
    return repo


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_doctor_unconfigured_repo(repo, which):
    checks = by_name(run_doctor(repo, env={}))
    assert checks["config"].status == "fail"
    assert "config.yaml not found" in checks["config"].detail
    assert "finops init" in checks["config"].fix
    assert "automations" not in checks
    assert checks["agent profile"].status == "fail"
    assert checks["schemas"].status == "warn" and checks["schemas"].fix == "finops schemas export"
    assert (checks["azure access"].status, checks["azure access"].detail) == ("skip", "no config")
    assert checks["github"].status == "skip"
    assert "copilot token" not in checks


def test_doctor_healthy_repo_in_fixture_mode(healthy, monkeypatch):
    monkeypatch.setenv("FINOPS_FIXTURE_MODE", "synthetic")
    checks = run_doctor(healthy, skip_github=True, env={})
    named = by_name(checks)
    assert named["config"].detail.endswith(": 2 subscription(s), 2 destination(s)")
    assert named["automations"].detail == "2 automation(s), 1 enabled"
    assert named["agent profile"].detail == "tools match finops/allowlist.yaml"
    assert named["schemas"].detail == "current"
    assert named["copilot on PATH"].detail == "/usr/bin/copilot"
    for sub in ("contoso-prod", "contoso-dev"):
        for cap in ("cost_data", "resource_graph", "advisor", "activity_log"):
            assert named[f"{sub}: {cap}"].status == "ok"
    assert named["azure mode"].status == "warn"
    assert [c.name for c in checks if c.status != "ok"] == ["azure mode"]
    assert not any(c.name.startswith("github") for c in checks)


def test_doctor_reports_missing_azure_permissions(healthy, monkeypatch):
    monkeypatch.setenv("FINOPS_FIXTURE_MODE", "synthetic")
    monkeypatch.setenv("FINOPS_SYNTHETIC_DENY", f"cost:{PROD}")
    checks = by_name(run_doctor(healthy, skip_github=True, env={}))
    bad = checks["contoso-prod: cost_data"]
    assert bad.status == "fail" and bad.detail and bad.fix
    assert checks["contoso-dev: cost_data"].status == "ok"
    assert checks["contoso-prod: resource_graph"].status == "ok"


def test_doctor_skip_azure(healthy):
    checks = by_name(run_doctor(healthy, skip_azure=True, skip_github=True, env={}))
    assert (checks["azure access"].status, checks["azure access"].detail) == ("skip", "skipped")
    assert "azure mode" not in checks


def test_doctor_azure_failure_is_reported(healthy, monkeypatch):
    import azure_finops.runtime as runtime_mod

    def boom(**kw):
        raise RuntimeError("no credential")

    monkeypatch.setattr(runtime_mod, "build_runtime", boom)
    check = by_name(run_doctor(healthy, skip_github=True, env={}))["azure access"]
    assert (check.status, check.detail) == ("fail", "RuntimeError: no credential")
    assert "az login" in check.fix


def test_doctor_invalid_automations(healthy):
    (healthy / "finops" / "automations" / "bad.yaml").write_text("name: bad\n")
    check = by_name(run_doctor(healthy, skip_azure=True, skip_github=True, env={}))["automations"]
    assert check.status == "fail" and "bad.yaml" in check.detail


def test_doctor_tools_on_path(healthy, which):
    which.discard("az")
    checks = by_name(run_doctor(healthy, skip_azure=True, skip_github=True, env={}))
    assert (
        checks["az on PATH"].status == "warn" and checks["az on PATH"].detail == "Azure CLI login not found"
    )
    assert checks["azure-finops-mcp on PATH"].status == "ok"


def test_doctor_npx_is_optional(healthy, which):
    which.discard("npx")
    check = by_name(run_doctor(healthy, skip_azure=True, skip_github=True, env={}))["npx on PATH"]
    assert check.status == "warn" and "optional" in check.detail


def test_doctor_flags_placeholder_ids_and_skips_azure(healthy):
    cfg = CONFIG.model_dump(mode="json")
    cfg["azure"]["tenant_id"] = "00000000-0000-0000-0000-000000000000"
    cfg["azure"]["subscriptions"][0]["id"] = "00000000-0000-0000-0000-000000000001"
    (healthy / "finops" / "config.yaml").write_text(yaml.safe_dump(cfg))
    checks = by_name(run_doctor(healthy, skip_github=True, env={}))
    assert checks["config ids"].status == "fail"
    assert "00000000-0000-0000-0000-000000000001" in checks["config ids"].detail
    assert "finops init" in checks["config ids"].fix
    assert (checks["azure access"].status, checks["azure access"].detail) == (
        "skip",
        "fix the config ids first",
    )


def test_doctor_flags_az_tenant_mismatch(healthy, monkeypatch):
    import azure_finops.runtime as runtime_mod

    monkeypatch.setattr(doctor, "_az_tenant", lambda: "99999999-9999-9999-9999-999999999999")
    monkeypatch.setattr(runtime_mod, "build_runtime", lambda **kw: pytest.fail("probed Azure"))
    checks = by_name(run_doctor(healthy, skip_github=True, env={}))
    assert checks["az tenant"].status == "fail"
    assert checks["az tenant"].fix == f"az login --tenant {CONFIG.azure.tenant_id}"
    assert checks["azure access"].detail == "fix the az tenant first"


def test_doctor_matching_tenant_is_ok(healthy, monkeypatch):
    import azure_finops.runtime as runtime_mod

    monkeypatch.setattr(doctor, "_az_tenant", lambda: CONFIG.azure.tenant_id.upper().lower())

    def boom(**kw):
        raise RuntimeError("stop here")

    monkeypatch.setattr(runtime_mod, "build_runtime", boom)
    checks = by_name(run_doctor(healthy, skip_github=True, env={}))
    assert checks["az tenant"].status == "ok"
    assert checks["azure access"].detail == "RuntimeError: stop here"


def test_doctor_collapses_unreachable_subscription(healthy, monkeypatch):
    monkeypatch.setenv("FINOPS_FIXTURE_MODE", "synthetic")
    monkeypatch.setenv("FINOPS_SYNTHETIC_DENY", f"cost:{PROD},arg:{PROD},activity:{PROD}")
    checks = by_name(run_doctor(healthy, skip_github=True, env={}))
    bad = checks["contoso-prod: all data sources"]
    assert bad.status == "fail" and bad.detail.startswith("none reachable.") and bad.fix
    assert not any(n.startswith("contoso-prod: cost_data") for n in checks)
    assert checks["contoso-dev: cost_data"].status == "ok"


def test_subscription_checks_explain_subscription_not_found():
    err = {"status": "error", "error": "failed (404): SubscriptionNotFound: could not be found."}
    res = {"name": "prod", **{cap: err for cap in doctor.CAPS}}
    [check] = doctor._subscription_checks(res, "abc")
    assert check.name == "prod: all data sources"
    assert "az account list" in check.fix and "abc" in check.fix


def test_doctor_vscode_mcp_command(healthy):
    exe = healthy / ".venv" / "bin" / "azure-finops-mcp"
    exe.unlink()
    check = by_name(run_doctor(healthy, skip_azure=True, skip_github=True, env={}))["vs code mcp server"]
    assert check.status == "warn" and ".venv" in check.fix
    exe.write_text("")
    check = by_name(run_doctor(healthy, skip_azure=True, skip_github=True, env={}))["vs code mcp server"]
    assert check.status == "ok"


@pytest.mark.parametrize(
    ("env", "status", "detail"),
    [
        ({}, "fail", "missing"),
        ({"GITHUB_TOKEN": "a"}, "ok", "from GITHUB_TOKEN"),
        ({"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}, "ok", "from GH_TOKEN"),
        ({"GITHUB_TOKEN": "a", "COPILOT_GITHUB_TOKEN": "c"}, "ok", "from COPILOT_GITHUB_TOKEN"),
        ({"COPILOT_GITHUB_TOKEN": ""}, "fail", "missing"),
    ],
)
def test_doctor_copilot_token_in_actions(healthy, env, status, detail):
    check = by_name(
        run_doctor(healthy, skip_azure=True, skip_github=True, env={"GITHUB_ACTIONS": "true", **env})
    )["copilot token"]
    assert (check.status, check.detail) == (status, detail)
    assert bool(check.fix) == (status == "fail")


# -- doctor: GitHub checks -------------------------------------------------------------------------


@pytest.fixture
def gh(monkeypatch):
    """Route doctor's GitHub client to a FakeGitHub, recording constructor args."""
    fake = FakeGitHub()
    fake.calls = []
    fake.handler = fake.handle

    def factory(token, repo, api_url):
        fake.calls.append((token, repo, api_url))
        return GitHub(
            token, repo, API, transport=httpx.MockTransport(lambda r: fake.handler(r)), sleep=lambda _s: None
        )

    monkeypatch.setattr(github_mod, "GitHub", factory)
    return fake


GH_ENV = {"GH_TOKEN": "tok", "GITHUB_REPOSITORY": "octo/finops"}


def github_checks(root, env) -> list[Check]:
    return [
        c
        for c in run_doctor(root, skip_azure=True, env=env)
        if c.name.startswith(("github", "repo", "environ"))
    ]


@pytest.mark.parametrize("env", [{}, {"GH_TOKEN": "t"}, {"GITHUB_REPOSITORY": "octo/finops"}])
def test_github_checks_skip_without_credentials(healthy, gh, env):
    assert [(c.name, c.status) for c in github_checks(healthy, env)] == [("github", "skip")]
    assert gh.calls == []


@pytest.mark.parametrize(
    ("policy", "status"),
    [
        ({"protected_branches": True, "custom_branch_policies": False}, "ok"),
        ({"protected_branches": False, "custom_branch_policies": True}, "ok"),
        ({"protected_branches": False, "custom_branch_policies": False}, "warn"),
        (None, "warn"),
    ],
)
def test_github_environment_branch_policy(healthy, gh, policy, status):
    gh.environments["finops"] = {"name": "finops", "deployment_branch_policy": policy}
    checks = github_checks(healthy, {**GH_ENV, "GITHUB_API_URL": "https://ghe.example/api/v3"})
    assert gh.calls == [("tok", "octo/finops", "https://ghe.example/api/v3")]
    assert [(c.name, c.status) for c in checks] == [("repo visibility", "ok"), ("environment finops", status)]
    assert checks[0].detail == "private"


def test_github_token_precedence(healthy, gh):
    github_checks(healthy, {**GH_ENV, "GITHUB_TOKEN": "first"})
    assert gh.calls[0][0] == "first"
    assert gh.calls[0][2] == "https://api.github.com"


def test_github_missing_environment_fails(healthy, gh):
    checks = github_checks(healthy, GH_ENV)
    env_check = checks[-1]
    assert (env_check.name, env_check.status) == ("environment finops", "fail")
    assert "404" in env_check.detail and env_check.fix == "Run scripts/setup/setup.sh"


def test_github_environment_forbidden_warns(healthy, gh):
    def handler(request):
        if request.url.path.endswith("/environments/finops"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return gh.handle(request)

    gh.handler = handler
    env_check = github_checks(healthy, GH_ENV)[-1]
    assert (env_check.status, env_check.detail) == (
        "warn",
        "GitHub API 403 on /repos/octo/finops/environments/finops: Resource not accessible by integration",
    )


def test_github_public_repo_fails(healthy, gh):
    gh.info.update(private=False, visibility="public")
    gh.environments["finops"] = {"deployment_branch_policy": {"protected_branches": True}}
    checks = by_name(github_checks(healthy, GH_ENV))
    assert checks["repo visibility"].status == "fail"
    assert checks["repo visibility"].detail == "octo/finops is public"
    assert checks["environment finops"].status == "ok"


def test_github_repo_unreachable(healthy, gh):
    checks = github_checks(healthy, {**GH_ENV, "GITHUB_REPOSITORY": "octo/elsewhere"})
    assert [(c.name, c.status) for c in checks] == [("github repo", "fail")]
    assert "404" in checks[0].detail and checks[0].fix == "Check the token and GITHUB_REPOSITORY"


# -- render and CLI --------------------------------------------------------------------------------


def test_render():
    text = render(
        [
            Check("config", "ok", "2 subscription(s)"),
            Check("schemas", "ok", "", "ignored when ok"),
            Check("az on PATH", "warn", "Azure CLI login not found", "install az"),
            Check("github", "skip", "not set", "never shown"),
            Check("config", "fail", "missing", "copy it"),
        ]
    )
    assert text.splitlines() == [
        "[PASS] config: 2 subscription(s)",
        "[PASS] schemas",
        "[WARN] az on PATH: Azure CLI login not found",
        "       fix: install az",
        "[SKIP] github: not set",
        "[FAIL] config: missing",
        "       fix: copy it",
    ]


def test_cli_doctor_exit_code(healthy, monkeypatch, capsys):
    for k in ("GITHUB_ACTIONS", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["doctor", "--skip-azure", "--skip-github"]) == 0
    assert "[PASS] config:" in capsys.readouterr().out
    (healthy / "finops" / "config.yaml").unlink()
    assert cli.main(["doctor", "--skip-azure", "--skip-github"]) == 1
    assert "[FAIL] config:" in capsys.readouterr().out
