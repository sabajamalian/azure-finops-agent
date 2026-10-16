"""`finops` command line: headless task steps, posting, dispatchers, and maintenance commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .config import ConfigError, config_path, load_config, repo_root


def _gh(required: bool = True):
    from .github import GitHub

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        if required:
            raise SystemExit("error: set GITHUB_TOKEN (or GH_TOKEN) and GITHUB_REPOSITORY")
        return None
    return GitHub(token, repo, os.environ.get("GITHUB_API_URL", "https://api.github.com"))


def _ref() -> str:
    return os.environ.get("GITHUB_REF_NAME") or os.environ.get("FINOPS_DISPATCH_REF") or "main"


def _write_output(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def _config_or_exit_ok():
    """Unconfigured template repos exit 0 so scheduled workflows don't fail before setup."""
    if not config_path().exists():
        print(f"{config_path()} not found; nothing to do until the agent is configured (see docs/setup.md).")
        raise SystemExit(0)
    return load_config()


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# -- task ------------------------------------------------------------------------------------------


def cmd_task_prepare(a) -> int:
    from .task import TaskError, TaskInputs, prepare

    out = Path(a.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = load_config()
    _write_output(retention_days=config.retention_days)
    try:
        task = prepare(TaskInputs.from_env(), out, config, repo_root(), _gh(required=False))
    except TaskError as exc:
        (out / "prepare-error.json").write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _write_output(kind=task["kind"], task_id=task["task_id"])
    print(
        f"prepared {task['task_id']}: {len(task['subscriptions'])} subscription(s), "
        f"destinations {task['allowed_destinations']}"
    )
    return 0


def cmd_task_run_agent(a) -> int:
    from .task import run_agent

    code = run_agent(
        Path(a.output_dir), load_config(), repo_root(), copilot=a.copilot, timeout_s=a.timeout, model=a.model
    )
    print(f"copilot exited with {code}")
    return code


def cmd_task_finalize(a) -> int:
    from .task import finalize

    run = finalize(Path(a.output_dir))
    _print(run)
    return 0


# -- post / approve --------------------------------------------------------------------------------


def cmd_post(a) -> int:
    from .post import post, step_summary

    out = Path(a.output_dir)
    err = out / "prepare-error.json"
    config = load_config()
    if err.exists() and not (out / "task.json").exists():
        from .post import Poster

        gh = _gh()
        p = Poster(
            gh, config, {"kind": "prepare"}, os.environ.get("GITHUB_RUN_ID", "local"), _run_url(), a.dry_run
        )
        p.report_failure("the task could not be prepared", [json.loads(err.read_text())["error"]])
        res = {"status": "failed", "errors": ["prepare failed"], "posted": p.posted}
    else:
        res = post(out, config, _gh(), dry_run=a.dry_run)
    (out / "post.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    _summary(step_summary(res))
    _print(res)
    return 0 if res["status"] in ("ok", "no_action") else 1


def _run_url() -> str | None:
    server, repo, run = (
        os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    return f"{server or 'https://github.com'}/{repo}/actions/runs/{run}" if repo and run else None


def cmd_approve(a) -> int:
    from .post import PostError, approve

    gh = _gh()
    try:
        res = approve(load_config(), gh, a.issue, a.comment_id, dry_run=a.dry_run)
    except PostError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if not a.dry_run:
            gh.comment(a.issue, f"Couldn't approve: {exc}")
        return 1
    _print(res)
    return 0


# -- dispatchers -----------------------------------------------------------------------------------


def cmd_dispatch(a) -> int:
    from .dispatch import dispatch_schedules

    config = _config_or_exit_ok()
    now = datetime.fromisoformat(a.now).astimezone(UTC) if a.now else None
    res = dispatch_schedules(
        config, repo_root(), _gh(), _ref(), now, timedelta(hours=a.window_hours), a.dry_run
    )
    _summary(
        "### FinOps schedule dispatch\n\n"
        + ("\n".join(f"- {r['slot']}: {r['action']}" for r in res) or "- nothing due")
        + "\n"
    )
    _print(res)
    return 0


def cmd_detect(a) -> int:
    from .anomaly import run_detection
    from .runtime import build_runtime

    config = _config_or_exit_ok()
    rt = build_runtime(config=config, output_dir=Path(a.output_dir) if a.output_dir else None)
    res = run_detection(rt)
    Path(a.output).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    lines = [f"- {x['title']} (fp {x['fingerprint']})" for x in res["anomalies"]]
    _summary(
        f"### FinOps anomaly detection: {res['status']}\n\nWindow {res['window']['from']} to "
        f"{res['window']['to']}\n\n" + ("\n".join(lines) or "- no anomalies") + "\n"
    )
    print(f"{len(res['anomalies'])} anomalies, status {res['status']}; wrote {a.output}")
    for e in res["errors"]:
        print(f"warning: {e}", file=sys.stderr)
    return 0 if res["status"] in ("ok", "partial") else 1


def cmd_anomaly_dispatch(a) -> int:
    from .dispatch import anomaly_dispatch

    config = _config_or_exit_ok()
    detection = json.loads(Path(a.anomalies).read_text(encoding="utf-8"))
    res = anomaly_dispatch(config, repo_root(), _gh(), _ref(), detection, dry_run=a.dry_run)
    _summary(
        "### FinOps anomaly dispatch\n\n"
        + (
            "\n".join(f"- {r['fingerprint']}: {r['action']} {r.get('reason', '')}" for r in res)
            or "- nothing to investigate"
        )
        + "\n"
    )
    _print(res)
    return 0


# -- automations -----------------------------------------------------------------------------------


def cmd_automations(a) -> int:
    from zoneinfo import ZoneInfo

    from .automations import AutomationError, ScheduleTrigger, load_automations, next_fire_times

    config = load_config()
    try:
        autos = load_automations(repo_root(), config)
    except AutomationError as exc:
        print(f"invalid automations:\n{exc}", file=sys.stderr)
        return 1
    if a.action == "validate":
        print(f"{len(autos)} automation(s) valid")
        return 0
    last: dict[str, str] = {}
    gh = _gh(required=False)
    if gh is not None:
        since = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%d")
        for r in gh.workflow_runs("finops-run.yml", since):
            parts = (r.get("display_title") or "").split()
            if len(parts) >= 4 and parts[2] not in last:
                last[parts[2]] = f"{r.get('created_at')} ({r.get('conclusion') or r.get('status')})"
    now = datetime.now(UTC)
    rows = []
    for la in autos:
        au = la.automation
        row = {
            "name": au.name,
            "enabled": au.enabled,
            "trigger": au.trigger.type,
            "destinations": au.destinations,
            "file": str(la.path.relative_to(repo_root())),
            "last_triggered": last.get(au.name),
        }
        if isinstance(au.trigger, ScheduleTrigger):
            s = au.trigger.schedule
            tz = ZoneInfo(s.timezone)
            row["schedule"] = (
                f"{s.natural} (cron '{s.cron}' {s.timezone}{', ' + s.business_day + ' business day' if s.business_day else ''})"
            )
            row["next_runs"] = (
                [t.astimezone(tz).isoformat() for t in next_fire_times(s, now)] if au.enabled else []
            )
        else:
            row["filter"] = au.trigger.filter.model_dump(exclude_defaults=True)
        rows.append(row)
    if a.json:
        _print(rows)
    else:
        for r in rows:
            print(
                f"{r['name']}  [{'enabled' if r['enabled'] else 'disabled'}]  {r['trigger']}  -> {', '.join(r['destinations'])}"
            )
            if "schedule" in r:
                print(f"    {r['schedule']}")
                for t in r["next_runs"]:
                    print(f"    next: {t}")
            else:
                print(f"    filter: {r['filter']}")
            print(f"    last triggered: {r['last_triggered'] or 'unknown (needs GITHUB_TOKEN) or never'}")
    return 0


# -- maintenance -----------------------------------------------------------------------------------


def cmd_doctor(a) -> int:
    from .doctor import render, run_doctor

    checks = run_doctor(repo_root(), skip_azure=a.skip_azure, skip_github=a.skip_github)
    print(render(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def cmd_schemas(a) -> int:
    from .schemas import export

    stale = export(repo_root(), check=a.check)
    if a.check:
        if stale:
            print(f"out of date: {stale}; run `finops schemas export`", file=sys.stderr)
            return 1
        print("schemas are current")
    else:
        print("wrote finops/schemas/")
    return 0


def cmd_check_profile(a) -> int:
    from .profile import check_agent_profile

    errors = check_agent_profile(repo_root())
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if not errors:
        print("agent profile OK")
    return 1 if errors else 0


def cmd_config(a) -> int:
    config = load_config()
    if a.field == "tenant":
        print(config.azure.tenant_id)
    elif a.field == "subscriptions":
        print("\n".join(config.subscription_ids()))
    else:
        _print(config.model_dump(mode="json"))
    return 0


def cmd_evals(a) -> int:
    from .evals import load_suite, run_suite

    root = repo_root()
    suite = load_suite(Path(a.cases) if a.cases else root / "evals" / "cases.yaml")
    if a.action == "list":
        for c in suite.cases:
            print(f"{c.id}: {c.question}")
        return 0
    out = Path(a.out) if a.out else root / ".finops" / "evals"
    results = run_suite(
        suite,
        out,
        root,
        only=a.case or None,
        repeat=a.repeat,
        copilot=a.copilot,
        model=a.model,
        timeout_s=a.timeout,
    )
    summary = (out / "summary.md").read_text(encoding="utf-8")
    print(summary)
    _summary(summary)
    return 0 if all(r["passed"] for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="finops", description="Azure FinOps agent tooling")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("task", help="headless task steps used by finops-run.yml")
    tsub = t.add_subparsers(dest="task_cmd", required=True)
    tp = tsub.add_parser("prepare", help="build task.json and prompt.md from INPUT_* env vars")
    tp.add_argument("--output-dir", required=True)
    tp.set_defaults(fn=cmd_task_prepare)
    tr = tsub.add_parser("run-agent", help="run Copilot CLI headless with the FinOps agent")
    tr.add_argument("--output-dir", required=True)
    tr.add_argument("--copilot", default="copilot")
    tr.add_argument("--timeout", type=int, default=1800)
    tr.add_argument("--model", default=None)
    tr.set_defaults(fn=cmd_task_run_agent)
    tf = tsub.add_parser("finalize", help="ensure result.json exists and write run.json")
    tf.add_argument("--output-dir", required=True)
    tf.set_defaults(fn=cmd_task_finalize)

    po = sub.add_parser("post", help="validate result.json and publish to GitHub issues")
    po.add_argument("--output-dir", required=True)
    po.add_argument("--dry-run", action="store_true")
    po.set_defaults(fn=cmd_post)

    ap = sub.add_parser("approve", help="create issues proposed by an ask run")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--comment-id", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.set_defaults(fn=cmd_approve)

    d = sub.add_parser("dispatch", help="dispatch due scheduled automations")
    d.add_argument("--window-hours", type=float, default=3)
    d.add_argument("--now", help="ISO timestamp (testing)")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(fn=cmd_dispatch)

    de = sub.add_parser("detect", help="run the anomaly detector")
    de.add_argument("--output", required=True)
    de.add_argument("--output-dir", help="write evidence and audit logs here")
    de.set_defaults(fn=cmd_detect)

    ad = sub.add_parser("anomaly-dispatch", help="start investigations for new anomalies")
    ad.add_argument("--anomalies", required=True)
    ad.add_argument("--dry-run", action="store_true")
    ad.set_defaults(fn=cmd_anomaly_dispatch)

    au = sub.add_parser("automations", help="list or validate automations")
    au.add_argument("action", choices=["list", "validate"])
    au.add_argument("--json", action="store_true")
    au.set_defaults(fn=cmd_automations)

    doc = sub.add_parser("doctor", help="check configuration and access")
    doc.add_argument("--skip-azure", action="store_true")
    doc.add_argument("--skip-github", action="store_true")
    doc.set_defaults(fn=cmd_doctor)

    sc = sub.add_parser("schemas", help="export JSON schemas to finops/schemas/")
    sc.add_argument("action", choices=["export"])
    sc.add_argument("--check", action="store_true")
    sc.set_defaults(fn=cmd_schemas)

    cf = sub.add_parser("config", help="print the validated finops/config.yaml (used by setup scripts)")
    cf.add_argument("field", nargs="?", choices=["all", "tenant", "subscriptions"], default="all")
    cf.set_defaults(fn=cmd_config)

    ev = sub.add_parser("evals", help="run agent evals against synthetic Azure data (uses premium requests)")
    ev.add_argument("action", choices=["run", "list"])
    ev.add_argument("--cases", help="cases file (default: evals/cases.yaml)")
    ev.add_argument("--out", help="output directory (default: .finops/evals)")
    ev.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ev.add_argument("--repeat", type=int, default=1, help="runs per case, to measure consistency")
    ev.add_argument("--copilot", default="copilot")
    ev.add_argument("--model", default=None)
    ev.add_argument("--timeout", type=int, default=900)
    ev.set_defaults(fn=cmd_evals)

    cp = sub.add_parser("check-agent-profile", help="check the agent profile against finops/allowlist.yaml")
    cp.set_defaults(fn=cmd_check_profile)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
