"""`finops doctor`: check that config, automations, Azure access, and the GitHub side are ready."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


# The IDs shipped in finops/config.example.yaml.
PLACEHOLDER_IDS = {f"00000000-0000-0000-0000-00000000000{n}" for n in range(10)}
CAPS = ("cost_data", "resource_graph", "advisor", "activity_log")
VSCODE_VENV_PREFIX = "${workspaceFolder}/"


def _az_tenant() -> str | None:
    """Tenant of the current `az login`, or None if az isn't signed in or can't be run."""
    try:
        out = subprocess.run(
            [shutil.which("az") or "az", "account", "show", "--query", "tenantId", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip().lower() or None


def _placeholder_ids(config) -> list[str]:
    ids = [config.azure.tenant_id, *config.subscription_ids()]
    return [i for i in ids if i.lower() in PLACEHOLDER_IDS]


def _vscode_check(root: Path) -> Check | None:
    """VS Code starts the server from .vscode/mcp.json; warn if that command points at a missing file."""
    path = root / ".vscode" / "mcp.json"
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))["servers"]["azure-finops"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    command = str(spec.get("command", ""))
    if not command.startswith(VSCODE_VENV_PREFIX):
        return None
    target = root / command[len(VSCODE_VENV_PREFIX) :]
    if target.exists() or target.with_suffix(".exe").exists():
        return Check("vs code mcp server", "ok", str(target))
    return Check(
        "vs code mcp server",
        "warn",
        f".vscode/mcp.json runs {command}, which doesn't exist",
        "Create the virtual environment at .venv (python3 -m venv .venv && .venv/bin/pip install ./mcp-server), "
        "or edit the command in .vscode/mcp.json (on Windows: ${workspaceFolder}/.venv/Scripts/azure-finops-mcp.exe)",
    )


def _subscription_checks(res: dict, sub_id: str) -> list[Check]:
    """One line per data source, or a single line when nothing in the subscription is reachable."""
    failed = {cap: res[cap] for cap in CAPS if res[cap].get("status") != "available"}
    if len(failed) == len(CAPS):
        first = res["cost_data"]
        detail = first.get("missing") or first.get("error", "")
        errors = " ".join(str(r.get("error", "")) + str(r.get("missing", "")) for r in failed.values())
        if "SubscriptionNotFound" in errors or "could not be found" in errors:
            fix = (
                f"Your sign-in can't see subscription {sub_id}. Check the ID in finops/config.yaml against "
                "`az account list -o table`, and that `az login` used the configured tenant."
            )
        else:
            fix = (
                first.get("remediation") or "Check `az login` and the role assignments on this subscription."
            )
        return [Check(f"{res['name']}: all data sources", "fail", f"none reachable. {detail}", fix)]
    out = []
    for cap in CAPS:
        r = res[cap]
        ok = r.get("status") == "available"
        out.append(
            Check(
                f"{res['name']}: {cap}",
                "ok" if ok else "fail",
                "" if ok else r.get("missing") or r.get("error", ""),
                "" if ok else r.get("remediation", ""),
            )
        )
    return out


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
            Check(
                "config",
                "fail",
                str(exc),
                "Run `finops init` after `az login`, or copy finops/config.example.yaml to finops/config.yaml and edit it",
            )
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
        ("npx", "optional; the Azure MCP Server (Advisor and retail prices) won't start without Node.js"),
    ):
        found = shutil.which(cmd)
        checks.append(Check(f"{cmd} on PATH", "ok" if found else "warn", found or f"{why} not found"))
    vscode = _vscode_check(root)
    if vscode is not None:
        checks.append(vscode)
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

    # The runtime reads fixture mode from the process environment, so check both.
    fixture = bool(env.get("FINOPS_FIXTURE_MODE") or os.environ.get("FINOPS_FIXTURE_MODE"))
    placeholders = _placeholder_ids(config) if config is not None and not fixture else []
    if placeholders:
        checks.append(
            Check(
                "config ids",
                "fail",
                f"placeholder IDs from the example config: {', '.join(placeholders)}",
                "Run `finops init --force` to fill in your tenant and subscriptions from `az account list`",
            )
        )
    signed_in = None
    if config is not None and not skip_azure and not fixture and not placeholders and shutil.which("az"):
        signed_in = _az_tenant()
    tenant_mismatch = signed_in is not None and signed_in != config.azure.tenant_id.lower()
    if tenant_mismatch:
        checks.append(
            Check(
                "az tenant",
                "fail",
                f"az is signed in to tenant {signed_in}, but finops/config.yaml uses {config.azure.tenant_id}",
                f"az login --tenant {config.azure.tenant_id}",
            )
        )
    elif signed_in is not None:
        checks.append(Check("az tenant", "ok", signed_in))

    if skip_azure or config is None or placeholders or tenant_mismatch:
        reason = (
            "skipped"
            if skip_azure
            else "no config"
            if config is None
            else "fix the config ids first"
            if placeholders
            else "fix the az tenant first"
        )
        checks.append(Check("azure access", "skip", reason))
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
            for sub_id, res in caps["subscriptions"].items():
                checks += _subscription_checks(res, sub_id)
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
