import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml
from conftest import PROD_SCOPE

from azure_finops.evals import (
    Case,
    Expect,
    extract_amounts,
    grade,
    load_artifacts,
    load_suite,
    run_case,
    run_suite,
    summarize,
    tool_numbers,
)
from azure_finops.simulator.synthetic import DEV

REPO = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).with_name("fake_copilot.py")


# -- amount extraction -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "tol"),
    [
        ("cost $1,180.50 per day", 1180.50, 0.005),
        ("about 1,180 USD/day", 1180, 0.5),
        ("USD 35.4k this month", 35400, 50),
        ("€2,000", 2000, 0.5),
        ("a $1.2M commitment", 1_200_000, 50_000),
        ("down -$12.30", 12.30, 0.005),
        ("saves 1635 usd", None, None),  # currency codes are upper case only
    ],
)
def test_extract_amounts(text, value, tol):
    found = extract_amounts(text)
    if value is None:
        assert found == []
    else:
        assert len(found) == 1
        assert found[0][1] == pytest.approx(value)
        assert found[0][2] == pytest.approx(tol)


def test_extract_amounts_ignores_plain_numbers_and_dedupes():
    text = "In 2026 we had 14 VMs. Spend was $5,189.15 (5,189.15 USD) on 3 services."
    found = extract_amounts(text)
    assert [round(v, 2) for _, v, _ in found] == [5189.15, 5189.15]


def test_tool_numbers_walks_results_and_skips_submit():
    rows = [
        {"tool": "query_costs", "result": {"totals": {"USD": 5189.15}, "note": "delta -1,234.5 vs prior"}},
        {"tool": "submit_result", "result": {"status": "accepted", "n": 999}},
    ]
    nums = tool_numbers(rows)
    assert 5189.15 in nums and 1234.5 in nums and 999 not in nums


# -- grading ---------------------------------------------------------------------------------------


def _art(summary="Spend was $5,189.15 [ev-001].", status="ok", ev=("ev-001",), tools=("query_costs",)):
    return {
        "result": {
            "status": status,
            "summary": summary,
            "evidence_ids": list(ev),
            "findings": [],
            "actions": [],
        },
        "run": {},
        "evidence_ids": {"ev-001", "ev-002"},
        "tool_calls": [{"event": "tool_call", "tool": t} for t in tools],
        "tool_results": [{"tool": "query_costs", "result": {"totals": {"USD": 5189.15}}}],
    }


def _failed(checks):
    return [c["name"] for c in checks if not c["passed"]]


def test_grade_passes_grounded_answer():
    exp = Expect(mentions_all=[["spend", "cost"]], tools_any=["query_costs"], max_actions=0)
    assert _failed(grade(exp, _art())) == []


def test_grade_flags_invented_number_and_unknown_evidence():
    art = _art(
        summary="Spend was $5,189.15 and will be $9,999.00 next month [ev-001].", ev=("ev-001", "ev-404")
    )
    checks = {c["name"]: c for c in grade(Expect(), art)}
    assert not checks["numbers_match"]["passed"] and "$9,999.00" in checks["numbers_match"]["detail"]
    assert not checks["cites_evidence"]["passed"] and "ev-404" in checks["cites_evidence"]["detail"]


def test_grade_requires_evidence_reference_in_summary():
    checks = {c["name"]: c for c in grade(Expect(), _art(summary="Spend was $5,189.15."))}
    assert not checks["cites_evidence"]["passed"]


def test_grade_rounding_tolerance():
    assert _failed(grade(Expect(), _art(summary="About $5,190 [ev-001]."))) == []
    assert _failed(grade(Expect(), _art(summary="About $5.2k [ev-001]."))) == []
    assert _failed(grade(Expect(), _art(summary="About $5,300 [ev-001]."))) == ["numbers_match"]


def test_grade_status_mentions_and_tools():
    exp = Expect(
        status=["no_action"],
        mentions_all=[["gpu"]],
        mentions_none=["spend"],
        tools_any=["detect_anomalies"],
        tools_none=["query_costs"],
        max_data_tool_calls=0,
    )
    failed = _failed(grade(exp, _art()))
    assert failed == [
        "status",
        "mentions 'gpu'",
        "doesn't mention 'spend'",
        "tools_any",
        "tools_none",
        "max_data_tool_calls",
    ]


def test_meta_tools_dont_count_as_data_calls():
    art = _art(tools=("get_capabilities", "submit_result"))
    exp = Expect(cites_evidence=False, numbers_match=False, max_data_tool_calls=0)
    assert _failed(grade(exp, art)) == []


def test_summarize_lists_failed_checks_once():
    runs = [
        {"case": "a", "passed": True, "checks": [{"name": "status", "passed": True, "detail": "got 'ok'"}]},
        {
            "case": "a",
            "passed": False,
            "checks": [{"name": "status", "passed": False, "detail": "got 'failed'"}],
        },
    ]
    md = summarize(runs, None)
    assert "| `a` | 1/2 | status (got 'failed') |" in md
    assert "Runs passed: **1/2**" in md


# -- suite file ------------------------------------------------------------------------------------


def test_repo_cases_file_is_valid():
    suite = load_suite(REPO / "evals" / "cases.yaml")
    assert len(suite.cases) >= 6
    assert {"prod-spend-increase", "refuse-remediation", "missing-cost-access"} <= {c.id for c in suite.cases}


def test_suite_rejects_duplicates_and_unknown_keys(tmp_path):
    p = tmp_path / "cases.yaml"
    p.write_text(
        yaml.safe_dump({"cases": [{"id": "a", "question": "why?"}, {"id": "a", "question": "how?"}]})
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_suite(p)
    p.write_text(yaml.safe_dump({"cases": [{"id": "a", "question": "why?", "expect": {"bogus": 1}}]}))
    with pytest.raises(ValueError):
        load_suite(p)


# -- end to end with a scripted stand-in for the Copilot CLI ---------------------------------------


@pytest.fixture
def fake_copilot(tmp_path):
    exe = tmp_path / "bin" / "copilot"
    exe.parent.mkdir()
    # The agent gets an allow-listed environment, so the wrapper supplies the script and record paths.
    exe.write_text(
        "#!/bin/sh\n"
        f'FAKE_COPILOT_SCRIPT="{tmp_path / "script.json"}" FAKE_COPILOT_RECORD="{tmp_path / "record.json"}" '
        f'exec "{sys.executable}" "{FAKE}" "$@"\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _env(tmp_path, script: dict) -> dict:
    (tmp_path / "script.json").write_text(json.dumps(script))
    env = dict(os.environ)
    env |= {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")]),
        "GH_TOKEN": "gh-secret",
        "GITHUB_TOKEN": "gh-secret",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret",
        "FINOPS_CONFIG": "/nonexistent/config.yaml",
    }
    return env


def test_run_case_end_to_end(repo, tmp_path, fake_copilot):
    (repo / "finops" / "allowlist.yaml").write_text(
        yaml.safe_dump(
            {
                "servers": {
                    "azure-mcp": {
                        "command": "npx",
                        "args": ["-y", "@azure/mcp@3.0.0", "server", "start", "--read-only"],
                        "tools": ["advisor_recommendation_list"],
                    }
                }
            }
        )
    )
    script = {
        "calls": [
            {
                "tool": "query_costs",
                "args": {"scope": PROD_SCOPE, "start_date": "2026-03-01", "end_date": "2026-03-10"},
            }
        ],
        "result": {
            "status": "ok",
            "summary": "contoso-prod spent ${0.totals.USD:,.2f} from 1 to 10 March ({ev}).",
            "evidence_ids": "$EV",
        },
    }
    case = Case(
        id="e2e",
        question="How much did contoso-prod cost in early March?",
        expect=Expect(mentions_all=[["contoso-prod"]], tools_any=["query_costs"], max_actions=0),
    )
    run_dir = tmp_path / "run"
    r = run_case(case, run_dir, repo, _env(tmp_path, script), copilot=str(fake_copilot), timeout_s=120)
    assert r["passed"], r["checks"]
    assert r["agent_exit_code"] == 0 and r["tool_calls"] == 2

    result = json.loads((run_dir / "result.json").read_text())
    assert "$5,189.15" in result["summary"] and result["evidence_ids"] == ["ev-001"]
    task = json.loads((run_dir / "task.json").read_text())
    assert task["kind"] == "manual" and task["initiator"]["actor"] == "finops-evals"

    # Fixture runs never reach real Azure: allow-listed servers are left out and denied by name.
    cfg = json.loads((run_dir / "mcp-config.json").read_text())
    assert list(cfg["mcpServers"]) == ["azure-finops"]
    server_env = cfg["mcpServers"]["azure-finops"]["env"]
    assert server_env["FINOPS_FIXTURE_MODE"] == "synthetic" and "FINOPS_CONFIG" not in server_env
    rec = json.loads((tmp_path / "record.json").read_text())
    argv = rec["argv"]
    assert argv[argv.index("--agent") + 1] == "azure-finops"
    assert any(argv[i : i + 2] == ["--deny-tool", "azure-mcp"] for i in range(len(argv)))
    assert not any(a.startswith("azure-mcp(") for a in argv)
    # Tokens from the parent environment never reach the agent process.
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "FINOPS_CONFIG"):
        assert key not in rec["env"]
    assert (run_dir / "tool-results.jsonl").exists()


def test_run_suite_reports_missing_access_and_no_submit(repo, tmp_path, fake_copilot, monkeypatch):
    script = {
        "calls": [
            {
                "tool": "query_costs",
                "args": {
                    "scope": f"/subscriptions/{DEV}",
                    "start_date": "2026-02-01",
                    "end_date": "2026-02-28",
                },
            }
        ],
        "exit_code": 1,
    }
    for k, v in _env(tmp_path, script).items():
        monkeypatch.setenv(k, v)
    suite = load_suite_from(
        tmp_path,
        [
            {
                "id": "denied",
                "question": "How much did contoso-dev cost last month?",
                "env": {"FINOPS_SYNTHETIC_DENY": f"cost:{DEV}"},
                "expect": {"status": ["failed"], "cites_evidence": False, "numbers_match": False},
            }
        ],
    )
    results = run_suite(suite, tmp_path / "out", repo, copilot=str(fake_copilot), timeout_s=120)
    # The agent never submitted, so finalize wrote a failed stub, which is what this case expects.
    assert results[0]["passed"] and results[0]["agent_exit_code"] == 1
    art = load_artifacts(tmp_path / "out" / "denied" / "run-1")
    assert "without submitting" in art["result"]["summary"]
    denied = json.loads(
        (tmp_path / "out" / "denied" / "run-1" / "tool-results.jsonl").read_text().splitlines()[0]
    )
    assert denied["result"]["status"] != "ok"
    assert (tmp_path / "out" / "summary.md").read_text().startswith("# FinOps agent evals")
    assert json.loads((tmp_path / "out" / "results.json").read_text())["results"][0]["case"] == "denied"


def load_suite_from(tmp_path, cases):
    p = tmp_path / "cases.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "cases": cases}))
    return load_suite(p)
