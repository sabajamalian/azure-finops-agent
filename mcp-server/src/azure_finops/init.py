"""`finops init`: write finops/config.yaml from the subscriptions your `az login` can see."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .config import Config

AzRunner = Callable[[list[str]], object]


class InitError(Exception):
    pass


def run_az(args: list[str]) -> object:
    try:
        az = shutil.which("az") or "az"  # az.cmd on Windows
        out = subprocess.run([az, *args, "-o", "json"], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise InitError("the Azure CLI (az) isn't installed; see https://aka.ms/azcli") from exc
    if out.returncode != 0:
        msg = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "az failed"
        raise InitError(f"`az {' '.join(args)}` failed: {msg}. Sign in with `az login` first.")
    return json.loads(out.stdout or "null")


def local_timezone() -> str | None:
    """Best-effort IANA name of the machine's time zone."""
    candidates = [os.environ.get("TZ", "")]
    try:
        candidates.append(os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1])
    except (IndexError, OSError):
        pass
    for name in candidates:
        name = name.lstrip(":")
        if not name:
            continue
        try:
            ZoneInfo(name)
        except Exception:  # noqa: BLE001, S112 - try the next candidate
            continue
        return name
    return None


def choose(subs: list[dict], picks: list[str]) -> list[dict]:
    """Match each pick against a subscription id, name, or 1-based list number."""
    chosen: list[dict] = []
    for pick in picks:
        p = pick.strip()
        if not p:
            continue
        match = None
        if p.isdigit() and 1 <= int(p) <= len(subs):
            match = subs[int(p) - 1]
        else:
            match = next((s for s in subs if p.lower() in (s["id"].lower(), s["name"].lower())), None)
        if match is None:
            raise InitError(f"no subscription matches {p!r}; run `az account list -o table` to see them")
        if match not in chosen:
            chosen.append(match)
    return chosen


def render_config(example: str | None, tenant: str, subs: list[dict], timezone: str | None) -> str:
    """Fill the example config's tenant, subscriptions, and time zone, keeping its comments."""
    block = "  subscriptions:\n" + "".join(
        f"    - id: {s['id'].lower()}\n      name: {json.dumps(s['name'])}\n" for s in subs
    )
    text = example or ""
    patterns = [
        (r"(?m)^  tenant_id: .*$", f"  tenant_id: {tenant.lower()}"),
        (r"(?ms)^  subscriptions:\n.*?(?=^  \S)", block),
    ]
    if timezone:
        patterns.append((r"(?m)^timezone: \S+", f"timezone: {timezone}"))
    for pattern, repl in patterns:
        text, n = re.subn(pattern, lambda _m, r=repl: r, text, count=1)
        if n != 1:
            text = ""
            break
    if not text:
        data = {
            "version": 1,
            "azure": {
                "tenant_id": tenant.lower(),
                "subscriptions": [{"id": s["id"].lower(), "name": s["name"]} for s in subs],
            },
            "destinations": [
                {"id": "finops-reports", "type": "github_issue", "labels": ["finops-report"]},
                {"id": "finops-actions", "type": "github_issue", "labels": ["finops-action"]},
            ],
        }
        if timezone:
            data["timezone"] = timezone
        text = "# yaml-language-server: $schema=./schemas/config.schema.json\n" + yaml.safe_dump(
            data, sort_keys=False
        )
    Config.model_validate(yaml.safe_load(text))
    return text


def init_config(
    root: Path,
    *,
    tenant: str | None = None,
    picks: list[str] | None = None,
    all_subs: bool = False,
    timezone: str | None = None,
    force: bool = False,
    az: AzRunner | None = None,
    ask: Callable[[str], str] | None = None,
    out=sys.stdout,
) -> Path:
    az = az or run_az
    path = root / "finops" / "config.yaml"
    if path.exists() and not force:
        raise InitError(f"{path} already exists; pass --force to overwrite it")
    if timezone:
        try:
            ZoneInfo(timezone)
        except Exception as exc:  # noqa: BLE001
            raise InitError(f"unknown time zone {timezone!r}; use an IANA name like Europe/London") from exc

    if not tenant:
        account = az(["account", "show"])
        tenant = (account or {}).get("tenantId")
        if not tenant:
            raise InitError("can't tell which tenant you're signed in to; pass --tenant or run `az login`")
    tenant = tenant.lower()
    subs = [
        {"id": s["id"], "name": s.get("name") or s["id"]}
        for s in az(["account", "list"]) or []
        if str(s.get("tenantId", "")).lower() == tenant and s.get("state", "Enabled") == "Enabled"
    ]
    if not subs:
        raise InitError(
            f"your sign-in sees no enabled subscriptions in tenant {tenant}; run `az login --tenant {tenant}`"
        )

    if all_subs:
        chosen = subs
    elif picks:
        chosen = choose(subs, picks)
    elif ask is not None:
        print(f"Subscriptions in tenant {tenant}:", file=out)
        for i, s in enumerate(subs, 1):
            print(f"  {i:>2}. {s['name']}  ({s['id']})", file=out)
        answer = ask(
            "Which ones should the agent analyze? Numbers, names, or IDs, comma-separated, or 'all': "
        )
        chosen = subs if answer.strip().lower() == "all" else choose(subs, answer.split(","))
    else:
        raise InitError(
            "pass --subscription (repeatable) or --all, or run in a terminal to pick interactively"
        )
    if not chosen:
        raise InitError("no subscriptions selected")

    example_path = root / "finops" / "config.example.yaml"
    example = example_path.read_text(encoding="utf-8") if example_path.exists() else None
    text = render_config(example, tenant, chosen, timezone or local_timezone())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
