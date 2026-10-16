"""`finops doctor`: check that config, automations, Azure access, and the GitHub side are ready."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .automations import AutomationError, load_automations
from .config import ConfigError, config_path, load_config
from .profile import check_agent_profile
from .schemas import export as export_schemas


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str = ""
    fix: str = ""


def _github_checks(env: dict[str, str]) -> list[Check]:
    from .github import GitHub, GitHubError

    token, repo = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN"), env.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return [Check("github", "skip", "GITHUB_TOKEN/GH_TOKEN and GITHUB_REPOSITORY not set")]
    gh = GitHub(token, repo, env.get("GITHUB_API_URL", "https://api.github.com"))
    out = []
    try:
        info = gh.repo_info()
    except GitHubError as exc:
        return [Check("github repo", "fail", str(exc), "Check the token and GITHUB_REPOSITORY")]
    if info.get("private") is False:
        out.append(
            Check(
                "repo visibility",
                "fail",
                f"{repo} is public",
                "Use a private repository; issues and artifacts contain spend data",
            )
        )
    else:
        out.append(Check("repo visibility", "ok", info.get("visibility", "private")))
    try:
        env_info = gh.request("GET", f"/repos/{repo}/environments/finops").json()
        policy = env_info.get("deployment_branch_policy") or {}
        if policy.get("protected_branches") or policy.get("custom_branch_policies"):
            out.append(Check("environment finops", "ok", "deployment branches restricted"))
        else:
            out.append(
                Check(
                    "environment finops",
                    "warn",
                    "any branch can deploy to it",
                    "Restrict the finops environment to the default branch (scripts/setup/setup.sh does this)",
                )
            )
    except GitHubError as exc:
        status = "fail" if exc.status == 404 else "warn"
        out.append(Check("environment finops", status, str(exc), "Run scripts/setup/setup.sh"))
    return out


def run_doctor(
    root: Path, skip_azure: bool = False, skip_github: bool = False, env: dict | None = None
) -> list[Check]:
    env = dict(os.environ if env is None else env)
    checks: list[Check] = []
    config = None
    try:
        config = load_config()
        checks.append(
            Check(
                "config",
                "ok",
                f"{config_path(root)}: {len(config.subscription_ids())} subscription(s), "
                f"{len(config.destinations)} destination(s)",
            )
        )
    except ConfigError as exc:
        checks.append(
            Check("config", "fail", str(exc), "cp finops/config.example.yaml finops/config.yaml and edit it")
        )

    if config is not None:
        try:
            autos = load_automations(root, config)
            enabled = sum(1 for a in autos if a.automation.enabled)
            checks.append(Check("automations", "ok", f"{len(autos)} automation(s), {enabled} enabled"))
        except AutomationError as exc:
            checks.append(Check("automations", "fail", str(exc), "Fix the files in finops/automations/"))

    errs = check_agent_profile(root)
    checks.append(
        Check(
            "agent profile", "fail" if errs else "ok", "; ".join(errs) or "tools match finops/allowlist.yaml"
        )
    )
    stale = export_schemas(root, check=True)
    checks.append(
        Check(
            "schemas",
            "warn" if stale else "ok",
            f"out of date: {stale}" if stale else "current",
            "finops schemas export" if stale else "",
        )
    )

    for cmd, why in (
        ("azure-finops-mcp", "MCP server"),
        ("copilot", "Copilot CLI"),
        ("az", "Azure CLI login"),
    ):
        found = shutil.which(cmd)
        checks.append(Check(f"{cmd} on PATH", "ok" if found else "warn", found or f"{why} not found"))
    if env.get("GITHUB_ACTIONS") == "true":
        # Same precedence as Copilot CLI. The workflows pass the PAT secret or the job token as the first.
        source = next((k for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN") if env.get(k)), None)
        checks.append(
            Check(
                "copilot token",
                "ok" if source else "fail",
                f"from {source}" if source else "missing",
                ""
                if source
                else "Grant the job copilot-requests: write and pass github.token as COPILOT_GITHUB_TOKEN, or "
                "store a fine-grained PAT with the Copilot Requests permission as the COPILOT_GITHUB_TOKEN "
                "secret on the finops environment",
            )
        )

    if skip_azure or config is None:
        checks.append(Check("azure access", "skip", "skipped" if skip_azure else "no config"))
    else:
        from .runtime import build_runtime
        from .scopes import get_capabilities

        try:
            rt = build_runtime(config=config)
            caps = get_capabilities(rt, refresh=True)
        except Exception as exc:  # noqa: BLE001 - doctor reports every failure
            checks.append(
                Check(
                    "azure access",
                    "fail",
                    f"{type(exc).__name__}: {exc}",
                    "Sign in with `az login` (locally) or check the azure/login step and role assignments",
                )
            )
        else:
            for res in caps["subscriptions"].values():
                for cap in ("cost_data", "resource_graph", "advisor", "activity_log"):
                    r = res[cap]
                    ok = r.get("status") == "available"
                    checks.append(
                        Check(
                            f"{res['name']}: {cap}",
                            "ok" if ok else "fail",
                            "" if ok else r.get("missing") or r.get("error", ""),
                            "" if ok else r.get("remediation", ""),
                        )
                    )
            if rt.fixture:
                checks.append(
                    Check("azure mode", "warn", "FINOPS_FIXTURE_MODE is on: synthetic data, not real Azure")
                )

    if not skip_github:
        checks += _github_checks(env)
    return checks


def render(checks: list[Check]) -> str:
    icon = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    lines = []
    for c in checks:
        lines.append(f"[{icon[c.status]}] {c.name}: {c.detail}".rstrip(": "))
        if c.fix and c.status in ("warn", "fail"):
            lines.append(f"       fix: {c.fix}")
    return "\n".join(lines)
