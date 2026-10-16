"""The result contract (result.json) between the headless agent run and the post job."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import Config
from .runtime import Runtime

# Labels that trigger or classify workflow behavior. Proposed issues can't carry them.
RESERVED_LABELS = frozenset({"finops-ask", "finops-failure", "finops-report", "finops-anomaly"})

MAX_SUMMARY_CHARS = 60_000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Impact(_Strict):
    amount: float
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    period: Literal["day", "month", "year", "one_time", "analysis_period"]


class Finding(_Strict):
    title: str = Field(min_length=3, max_length=200)
    impact: Impact | None = None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str] = Field(min_length=1, description="Evidence ids returned by FinOps tools")
    details: str = Field(default="", max_length=4000)


class CreateIssue(_Strict):
    type: Literal["create_issue"] = "create_issue"
    destination: str = Field(description="Destination id from finops/config.yaml")
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=1, max_length=50_000)
    labels: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Extra labels; the destination's labels are added automatically. "
        + ", ".join(sorted(RESERVED_LABELS))
        + " are reserved.",
    )
    fingerprint: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
        description="Stable id used to avoid duplicate issues",
    )
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("labels")
    @classmethod
    def _no_reserved_labels(cls, v: list[str]) -> list[str]:
        reserved = sorted({lbl for lbl in v if lbl.strip().lower() in RESERVED_LABELS})
        if reserved:
            raise ValueError(
                f"labels {reserved} are reserved for the workflows; use the destination's labels"
            )
        return v


class Result(_Strict):
    version: Literal[1] = 1
    status: Literal["ok", "no_action", "failed"]
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS, description="Markdown answer or report")
    findings: list[Finding] = Field(default_factory=list, max_length=50)
    actions: list[CreateIssue] = Field(default_factory=list, max_length=25)
    evidence_ids: list[str] = Field(default_factory=list)


def load_task(output_dir: Path | None) -> dict:
    if output_dir and (output_dir / "task.json").exists():
        return json.loads((output_dir / "task.json").read_text(encoding="utf-8"))
    return {}


def validate_result_data(
    data: dict, config: Config, known_evidence: set[str], allowed_destinations: set[str] | None = None
) -> tuple[Result | None, list[str]]:
    try:
        res = Result.model_validate(data)
    except ValidationError as exc:
        return None, [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
    errors: list[str] = []
    cited = set(res.evidence_ids)
    for f in res.findings:
        cited |= set(f.evidence_ids)
    for a in res.actions:
        cited |= set(a.evidence_ids)
    missing = sorted(cited - known_evidence)
    if missing:
        errors.append(
            f"unknown evidence ids {missing}; cite only ids returned by FinOps tools in this session"
        )
    configured = {d.id for d in config.destinations}
    allowed = configured & set(allowed_destinations) if allowed_destinations else configured
    for i, a in enumerate(res.actions):
        if a.destination not in allowed:
            errors.append(
                f"actions[{i}].destination {a.destination!r} is not allowed; allowed: {sorted(allowed)}"
            )
    if len(res.actions) > config.max_issues_per_run:
        errors.append(f"{len(res.actions)} actions exceed max_issues_per_run ({config.max_issues_per_run})")
    return (None if errors else res), errors


def validate_result(rt: Runtime, data: dict) -> tuple[Result | None, list[str]]:
    task = load_task(rt.output_dir)
    return validate_result_data(
        data, rt.config, rt.evidence.ids(), set(task.get("allowed_destinations") or [])
    )


def submit_result(rt: Runtime, result: dict) -> dict:
    res, errors = validate_result(rt, result)
    rt.audit.write("submit_result", valid=not errors, errors=errors)
    if errors:
        return {"status": "invalid", "errors": errors, "hint": "Fix the errors and call submit_result again."}
    assert res is not None
    if not rt.output_dir:
        return {
            "status": "valid",
            "written": False,
            "note": "Interactive session: nothing was written or posted. Show proposed issues to the user and get "
            "explicit approval before creating anything.",
        }
    rt.output_dir.mkdir(parents=True, exist_ok=True)
    path = rt.output_dir / "result.json"
    path.write_text(res.model_dump_json(indent=2), encoding="utf-8")
    return {
        "status": "accepted",
        "written": True,
        "path": str(path),
        "actions": len(res.actions),
        "note": "The post job validates this again and performs any approved actions. You are done.",
    }
