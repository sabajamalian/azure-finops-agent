"""Static check of the custom agent profile against finops/allowlist.yaml (run in CI)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from .task import SERVER_NAME, load_allowlist

PROFILE = Path(".github/agents/azure-finops.agent.md")
VSCODE_MCP = Path(".vscode/mcp.json")
WORKSPACE_MCP = (Path(".mcp.json"), Path(".github/mcp.json"))
ALLOWED_ALIASES = {"read", "search", "edit", "todo"}
MAX_BODY = 30_000


def parse_profile(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError("profile must start with YAML frontmatter between --- lines")
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def _tool_flags(args: list) -> list[str]:
    out = []
    for i, a in enumerate(args):
        if a == "--tool" and i + 1 < len(args):
            out.append(str(args[i + 1]))
        elif isinstance(a, str) and a.startswith("--tool="):
            out.append(a.split("=", 1)[1])
    return out


def _check_server(where: str, name: str, spec: dict, allowlist: dict) -> list[str]:
    errors = []
    if name == SERVER_NAME:
        command = re.sub(r"\.exe$", "", Path(str(spec.get("command", ""))).name, flags=re.I)
        args = [str(a) for a in spec.get("args", [])]
        runs_module = any(args[i : i + 2] == ["-m", "azure_finops.server"] for i in range(len(args)))
        if command != "azure-finops-mcp" and not runs_module:
            errors.append(f"{where}: server {SERVER_NAME!r} must run azure-finops-mcp")
        return errors
    allowed = allowlist["servers"].get(name)
    if allowed is None:
        return [f"{where}: MCP server {name!r} is not in finops/allowlist.yaml"]
    args = [str(a) for a in spec.get("args", [])]
    if "--read-only" not in args:
        errors.append(f"{where}: MCP server {name!r} must be started with --read-only")
    flags = _tool_flags(args)
    if not flags:
        errors.append(f"{where}: MCP server {name!r} must restrict tools with --tool flags")
    for t in flags:
        if t not in allowed["tools"]:
            errors.append(f"{where}: {name} --tool {t} is not allow-listed")
    for t in spec.get("tools", []) or []:
        if t == "*" or t not in allowed["tools"]:
            errors.append(f"{where}: {name} tools entry {t!r} is not allow-listed")
    pkg = allowed.get("args", [])
    pinned = [a for a in pkg if "@" in str(a)[1:]]
    for p in pinned:
        if p not in args:
            errors.append(f"{where}: {name} must use the pinned package {p}")
    return errors


def check_agent_profile(root: Path) -> list[str]:
    errors: list[str] = []
    try:
        allowlist = load_allowlist(root)
    except ValueError as exc:
        return [str(exc)]
    path = root / PROFILE
    if not path.exists():
        return [f"{PROFILE} not found"]
    try:
        fm, body = parse_profile(path.read_text(encoding="utf-8"))
    except (ValueError, yaml.YAMLError) as exc:
        return [f"{PROFILE}: {exc}"]
    if not fm.get("description"):
        errors.append(f"{PROFILE}: description is required")
    if fm.get("name") not in (None, SERVER_NAME):
        errors.append(f"{PROFILE}: name must be {SERVER_NAME!r}")
    if len(body) > MAX_BODY:
        errors.append(f"{PROFILE}: body is {len(body)} chars; the limit is {MAX_BODY}")
    tools = fm.get("tools")
    if not isinstance(tools, list) or not tools:
        errors.append(f"{PROFILE}: tools must be an explicit list (omitting it enables every tool)")
        tools = []
    for t in tools:
        t = str(t)
        if "/" in t:
            server, tool = t.split("/", 1)
            if server == SERVER_NAME:
                continue
            allowed = allowlist["servers"].get(server)
            if allowed is None:
                errors.append(f"{PROFILE}: tool {t!r} is from a server that isn't allow-listed")
            elif tool == "*" or tool not in allowed["tools"]:
                errors.append(
                    f"{PROFILE}: tool {t!r} is not in finops/allowlist.yaml (wildcards aren't allowed)"
                )
        elif t not in ALLOWED_ALIASES:
            errors.append(
                f"{PROFILE}: tool {t!r} is not allowed; use only {sorted(ALLOWED_ALIASES)} and MCP tools"
            )
    servers = fm.get("mcp-servers") or {}
    if SERVER_NAME not in servers:
        errors.append(f"{PROFILE}: mcp-servers must define {SERVER_NAME!r}")
    for name, spec in servers.items():
        errors += _check_server(str(PROFILE), name, spec or {}, allowlist)

    for p in WORKSPACE_MCP:
        if (root / p).exists():
            errors.append(
                f"{p}: Copilot CLI loads workspace MCP servers from this file; define servers in the agent profile "
                "and finops/allowlist.yaml instead"
            )

    vs = root / VSCODE_MCP
    if vs.exists():
        try:
            vservers = json.loads(vs.read_text(encoding="utf-8")).get("servers", {})
        except ValueError as exc:
            errors.append(f"{VSCODE_MCP}: {exc}")
            vservers = {}
        if set(vservers) != set(servers):
            errors.append(
                f"{VSCODE_MCP}: servers {sorted(vservers)} must match the profile's {sorted(servers)}"
            )
        for name, spec in vservers.items():
            errors += _check_server(str(VSCODE_MCP), name, spec or {}, allowlist)
    return errors
