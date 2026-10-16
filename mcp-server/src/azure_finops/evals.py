"""Evals: run the headless agent against synthetic Azure data and grade the answers.

Each case is a manual run with a question. The FinOps MCP server runs in synthetic fixture mode, so no real
Azure call is made, and it records every tool result so graders can check numbers in the answer. Graders are
pure functions over the run's artifacts (result.json, evidence.jsonl, audit.jsonl, tool-results.jsonl).

Each case costs real Copilot premium requests: one agent session per case and repeat.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Tools that don't read cost data; refusal cases may still call these.
META_TOOLS = {
    "get_capabilities",
    "list_scopes",
    "list_context",
    "read_context",
    "list_automations",
    "submit_result",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expect(_Strict):
    status: list[str] = Field(default_factory=lambda: ["ok"], description="Allowed result statuses")
    cites_evidence: bool = Field(default=True, description="Answer cites evidence ids that exist")
    numbers_match: bool = Field(default=True, description="Every currency amount appears in a tool result")
    mentions_all: list[list[str]] = Field(
        default_factory=list, description="Each group needs at least one of its phrases (case-insensitive)"
    )
    mentions_none: list[str] = Field(default_factory=list, description="Phrases that must not appear")
    tools_any: list[str] = Field(default_factory=list, description="At least one of these tools was called")
    tools_none: list[str] = Field(default_factory=list, description="None of these tools was called")
    max_data_tool_calls: int | None = Field(default=None, description="Cap on calls outside META_TOOLS")
    max_actions: int | None = None


class Case(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    question: str = Field(min_length=3)
    env: dict[str, str] = Field(default_factory=dict, description="Extra env, e.g. FINOPS_SYNTHETIC_DENY")
    expect: Expect = Field(default_factory=Expect)


class Suite(_Strict):
    version: int = 1
    cases: list[Case]


def load_suite(path: Path) -> Suite:
    suite = Suite.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    ids = [c.id for c in suite.cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate case ids: {dupes}")
    return suite


# -- artifacts -------------------------------------------------------------------------------------


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_artifacts(run_dir: Path) -> dict:
    result_path = run_dir / "result.json"
    run_path = run_dir / "run.json"
    return {
        "result": json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {},
        "run": json.loads(run_path.read_text(encoding="utf-8")) if run_path.exists() else {},
        "evidence_ids": {e["id"] for e in _jsonl(run_dir / "evidence.jsonl") if e.get("id")},
        "tool_calls": [r for r in _jsonl(run_dir / "audit.jsonl") if r.get("event") == "tool_call"],
        "tool_results": _jsonl(run_dir / "tool-results.jsonl"),
    }


# -- graders ---------------------------------------------------------------------------------------

_SCALE = {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "bn": 1e9, "B": 1e9}
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(k|K|M|bn|B)?\b"
_AMOUNT_RES = [
    re.compile(r"(?:\$|€|£|\b(?:USD|EUR|GBP|CAD|AUD|JPY|INR)\s?)\s?-?" + _NUM),
    re.compile(_NUM.removesuffix(r"\b") + r"\s?(?:USD|EUR|GBP|CAD|AUD|JPY|INR)\b"),
]


def extract_amounts(text: str) -> list[tuple[str, float, float]]:
    """Currency amounts in text as (as written, value, rounding tolerance). Values are absolute."""
    found: dict[tuple[int, int], tuple[str, float, float]] = {}
    for rx in _AMOUNT_RES:
        for m in rx.finditer(text):
            whole, frac, suffix = m.group(1), m.group(2) or "", m.group(3) or ""
            scale = _SCALE[suffix]
            value = float(whole.replace(",", "") + frac) * scale
            decimals = len(frac) - 1 if frac else 0
            tol = 0.5 * 10**-decimals * scale
            span = (m.start(1), m.end(1))
            if not any(s[0] <= span[0] < s[1] for s in found):
                found[span] = (m.group(0).strip(), value, tol)
    return [found[k] for k in sorted(found)]


_STR_NUM = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def _numbers(obj, out: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int | float):
        if math.isfinite(obj):
            out.append(abs(float(obj)))
    elif isinstance(obj, str):
        out.extend(abs(float(n.replace(",", ""))) for n in _STR_NUM.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers(v, out)


def tool_numbers(tool_results: list[dict]) -> list[float]:
    out: list[float] = []
    for r in tool_results:
        if r.get("tool") != "submit_result":
            _numbers(r.get("result"), out)
    return out


def _matches(value: float, tol: float, pool: list[float]) -> bool:
    # Allow the stated rounding plus 0.5% for "about 1,180"-style rounding of large numbers.
    slack = max(tol, abs(value) * 0.005, 0.01)
    return any(abs(v - value) <= slack for v in pool)


def check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def grade(expect: Expect, art: dict) -> list[dict]:
    result = art["result"]
    summary = result.get("summary") or ""
    text = summary.lower()
    called = [c.get("tool") for c in art["tool_calls"]]
    checks = [check("status", result.get("status") in expect.status, f"got {result.get('status')!r}")]

    if expect.cites_evidence:
        cited = set(result.get("evidence_ids") or [])
        for f in result.get("findings") or []:
            cited |= set(f.get("evidence_ids") or [])
        unknown = sorted(cited - art["evidence_ids"])
        ok = bool(cited) and not unknown and "ev-" in summary
        detail = f"cited {len(cited)}, unknown {unknown}" if cited else "no evidence ids cited"
        if cited and "ev-" not in summary:
            detail += "; summary doesn't reference evidence ids"
        checks.append(check("cites_evidence", ok, detail))

    if expect.numbers_match:
        amounts = extract_amounts(summary)
        pool = tool_numbers(art["tool_results"])
        unmatched = [a[0] for a in amounts if not _matches(a[1], a[2], pool)]
        checks.append(
            check(
                "numbers_match",
                not unmatched,
                f"{len(amounts) - len(unmatched)}/{len(amounts)} amounts found in tool results"
                + (f"; unmatched {unmatched[:10]}" if unmatched else ""),
            )
        )

    for group in expect.mentions_all:
        hit = next((p for p in group if p.lower() in text), None)
        checks.append(check(f"mentions {group[0]!r}", hit is not None, f"matched {hit!r}" if hit else ""))
    for phrase in expect.mentions_none:
        checks.append(check(f"doesn't mention {phrase!r}", phrase.lower() not in text))

    if expect.tools_any:
        hit = sorted(set(expect.tools_any) & set(called))
        checks.append(
            check("tools_any", bool(hit), f"called {hit}" if hit else f"none of {expect.tools_any}")
        )
    if expect.tools_none:
        bad = sorted(set(expect.tools_none) & set(called))
        checks.append(check("tools_none", not bad, f"called {bad}" if bad else ""))
    if expect.max_data_tool_calls is not None:
        n = sum(1 for t in called if t not in META_TOOLS)
        checks.append(check("max_data_tool_calls", n <= expect.max_data_tool_calls, f"{n} data tool calls"))
    if expect.max_actions is not None:
        n = len(result.get("actions") or [])
        checks.append(check("max_actions", n <= expect.max_actions, f"{n} actions"))
    return checks


# -- runner ----------------------------------------------------------------------------------------


def run_case(
    case: Case,
    run_dir: Path,
    root: Path,
    env: dict[str, str] | None = None,
    copilot: str = "copilot",
    model: str | None = None,
    timeout_s: int = 900,
) -> dict:
    from .simulator.synthetic import synthetic_config
    from .task import TaskInputs, finalize, prepare, run_agent

    shutil.rmtree(run_dir, ignore_errors=True)
    run_env = dict(os.environ if env is None else env)
    # Never let an eval pick up a real config, cached Azure data, or GitHub credentials.
    for k in ("FINOPS_CONFIG", "FINOPS_CACHE_DIR", "GITHUB_TOKEN", "GH_TOKEN"):
        run_env.pop(k, None)
    run_env |= {
        "FINOPS_FIXTURE_MODE": "synthetic",
        "FINOPS_RECORD_TOOL_RESULTS": "1",
        "FINOPS_CACHE_TTL": "0",
        "GITHUB_ACTOR": "finops-evals",
    }
    run_env |= case.env
    if not shutil.which("azure-finops-mcp", path=run_env.get("PATH")):
        # Running from a virtualenv that isn't activated: the server entry point sits next to python.
        run_env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), run_env.get("PATH", "")])
    config = synthetic_config()
    prepare(TaskInputs(kind="manual", question=case.question), run_dir, config, root, None, run_env)
    code = run_agent(run_dir, config, root, run_env, copilot=copilot, timeout_s=timeout_s, model=model)
    run = finalize(run_dir)
    checks = grade(case.expect, load_artifacts(run_dir))
    return {
        "case": case.id,
        "passed": all(c["passed"] for c in checks),
        "agent_exit_code": code,
        "duration_s": run.get("agent_duration_s"),
        "tool_calls": run.get("tool_calls"),
        "checks": checks,
    }


def summarize(results: list[dict], model: str | None) -> str:
    cases: dict[str, list[dict]] = {}
    for r in results:
        cases.setdefault(r["case"], []).append(r)
    passed = sum(1 for r in results if r["passed"])
    lines = [
        "# FinOps agent evals",
        "",
        f"Model: `{model or 'default'}`. Runs passed: **{passed}/{len(results)}**.",
        "",
        "| Case | Passed | Failed checks |",
        "|---|---|---|",
    ]
    for cid, runs in cases.items():
        ok = sum(1 for r in runs if r["passed"])
        failed = sorted(
            {
                f"{c['name']} ({c['detail']})" if c["detail"] else c["name"]
                for r in runs
                for c in r["checks"]
                if not c["passed"]
            }
        )
        cell = "<br>".join(x.replace("|", "\\|") for x in failed) or "-"
        lines.append(f"| `{cid}` | {ok}/{len(runs)} | {cell} |")
    return "\n".join(lines) + "\n"


def run_suite(
    suite: Suite,
    out_dir: Path,
    root: Path,
    only: list[str] | None = None,
    repeat: int = 1,
    copilot: str = "copilot",
    model: str | None = None,
    timeout_s: int = 900,
) -> list[dict]:
    cases = [c for c in suite.cases if not only or c.id in only]
    missing = sorted(set(only or []) - {c.id for c in cases})
    if missing:
        raise ValueError(f"unknown case ids: {missing}")
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        for i in range(repeat):
            run_dir = out_dir / case.id / f"run-{i + 1}"
            r = run_case(case, run_dir, root, copilot=copilot, model=model, timeout_s=timeout_s)
            r["run"] = i + 1
            results.append(r)
            print(f"{'PASS' if r['passed'] else 'FAIL'} {case.id} run {i + 1}")
    report = {
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "repeat": repeat,
        "results": results,
    }
    (out_dir / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(summarize(results, model), encoding="utf-8")
    return results
