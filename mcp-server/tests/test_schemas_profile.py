import json
import shutil
from pathlib import Path

import pytest
import yaml

from azure_finops.profile import MAX_BODY, PROFILE, check_agent_profile, parse_profile
from azure_finops.schemas import SCHEMAS, export, render

REAL_ROOT = Path(__file__).resolve().parents[2]
AZURE_MCP_ARGS = [
    "-y",
    "@azure/mcp@3.0.0",
    "server",
    "start",
    "--read-only",
    "--tool",
    "advisor_recommendation_list",
    "--tool",
    "pricing_get",
]


# -- schemas ---------------------------------------------------------------------------------------


def test_real_schemas_are_up_to_date():
    assert export(REAL_ROOT, check=True) == []


def test_export_writes_and_checks(tmp_path):
    assert export(tmp_path, check=True) == list(SCHEMAS)
    assert not (tmp_path / "finops").exists()  # check never writes

    assert export(tmp_path) == []
    d = tmp_path / "finops" / "schemas"
    assert sorted(p.name for p in d.iterdir()) == sorted(SCHEMAS)
    for name in SCHEMAS:
        data = json.loads((d / name).read_text())
        assert data["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert data["title"] == SCHEMAS[name][1]
        assert (d / name).read_text() == render(name)
    assert export(tmp_path, check=True) == []


def test_export_check_detects_drift(tmp_path):
    export(tmp_path)
    d = tmp_path / "finops" / "schemas"
    (d / "result.schema.json").write_text(render("result.schema.json").replace("Markdown", "Plain"))
    (d / "config.schema.json").unlink()
    assert export(tmp_path, check=True) == ["config.schema.json", "result.schema.json"]
    export(tmp_path)
    assert export(tmp_path, check=True) == []


def test_rendered_schemas_describe_the_models():
    result = json.loads(render("result.schema.json"))
    assert result["properties"]["status"]["enum"] == ["ok", "no_action", "failed"]
    assert result["additionalProperties"] is False
    automation = json.loads(render("automation.schema.json"))
    assert {"name", "trigger", "instructions", "destinations"} <= set(automation["required"])
    config = json.loads(render("config.schema.json"))
    assert "destinations" in config["properties"]


# -- agent profile ---------------------------------------------------------------------------------


def test_real_agent_profile_passes():
    assert check_agent_profile(REAL_ROOT) == []


ALLOWLIST = {
    "version": 1,
    "servers": {
        "azure-mcp": {
            "command": "npx",
            "args": ["-y", "@azure/mcp@3.0.0", "server", "start", "--read-only"],
            "tools": ["advisor_recommendation_list", "pricing_get"],
        }
    },
}


def frontmatter(**over) -> dict:
    fm = {
        "name": "azure-finops",
        "description": "Read-only Azure FinOps analyst.",
        "tools": ["read", "search", "edit", "todo", "azure-finops/*", "azure-mcp/pricing_get"],
        "mcp-servers": {
            "azure-finops": {"type": "local", "command": "azure-finops-mcp", "args": [], "tools": ["*"]},
            "azure-mcp": {
                "type": "local",
                "command": "npx",
                "args": list(AZURE_MCP_ARGS),
                "tools": ["advisor_recommendation_list", "pricing_get"],
            },
        },
    }
    for k, v in over.items():
        if v is None:
            fm.pop(k.replace("_", "-"), None)
        else:
            fm[k.replace("_", "-")] = v
    return fm


def write_profile(root: Path, fm: dict | None = None, body: str = "You are azure-finops.\n") -> Path:
    p = root / PROFILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm or frontmatter(), sort_keys=False) + "---\n\n" + body)
    return p


@pytest.fixture
def root(tmp_path) -> Path:
    (tmp_path / "finops").mkdir()
    (tmp_path / "finops" / "allowlist.yaml").write_text(yaml.safe_dump(ALLOWLIST))
    write_profile(tmp_path)
    return tmp_path


def servers(**over) -> dict:
    s = frontmatter()["mcp-servers"]
    s["azure-mcp"] = {**s["azure-mcp"], **over}
    return s


def test_valid_profile(root):
    assert check_agent_profile(root) == []


def test_copy_of_real_files_passes(tmp_path):
    for rel in (PROFILE, Path("finops/allowlist.yaml"), Path(".vscode/mcp.json")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL_ROOT / rel, tmp_path / rel)
    assert check_agent_profile(tmp_path) == []


def test_parse_profile():
    assert parse_profile("---\nname: x\n---\nbody") == ({"name": "x"}, "body")
    assert parse_profile("---\r\nname: x\r\n---\r\nbody") == ({"name": "x"}, "body")
    assert parse_profile("---\n\n---\n") == ({}, "")
    with pytest.raises(ValueError, match="frontmatter"):
        parse_profile("name: x\n")


def test_missing_profile(tmp_path):
    assert check_agent_profile(tmp_path) == [f"{PROFILE} not found"]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("no frontmatter here", "must start with YAML frontmatter"),
        ("---\nname: [unclosed\n---\nbody", "while parsing"),
    ],
)
def test_bad_frontmatter(root, text, fragment):
    (root / PROFILE).write_text(text)
    [err] = check_agent_profile(root)
    assert err.startswith(f"{PROFILE}: ") and fragment in err


def test_invalid_allowlist_is_reported_alone(root):
    (root / "finops" / "allowlist.yaml").write_text(yaml.safe_dump({"servers": {"x": {"tools": ["*"]}}}))
    assert check_agent_profile(root) == [
        "finops/allowlist.yaml: servers.x.tools can't use '*'; list read-only tools"
    ]


def test_body_too_long(root):
    # write_profile adds a blank line after the frontmatter, which counts toward the body.
    write_profile(root, body="x" * (MAX_BODY - 1))
    assert check_agent_profile(root) == []
    write_profile(root, body="x" * MAX_BODY)
    assert check_agent_profile(root) == [f"{PROFILE}: body is {MAX_BODY + 1} chars; the limit is {MAX_BODY}"]


def test_description_and_name(root):
    write_profile(root, frontmatter(description="", name="other"))
    assert check_agent_profile(root) == [
        f"{PROFILE}: description is required",
        f"{PROFILE}: name must be 'azure-finops'",
    ]
    write_profile(root, frontmatter(name=None))
    assert check_agent_profile(root) == []


@pytest.mark.parametrize("tools", [None, [], "read", {"read": True}])
def test_tools_must_be_an_explicit_list(root, tools):
    fm = frontmatter()
    if tools is None:
        del fm["tools"]
    else:
        fm["tools"] = tools
    write_profile(root, fm)
    assert check_agent_profile(root) == [
        f"{PROFILE}: tools must be an explicit list (omitting it enables every tool)"
    ]


@pytest.mark.parametrize(
    ("tool", "fragment"),
    [
        ("shell", "tool 'shell' is not allowed; use only ['edit', 'read', 'search', 'todo'] and MCP tools"),
        ("execute", "tool 'execute' is not allowed"),
        ("web", "tool 'web' is not allowed"),
        ("github/search_code", "tool 'github/search_code' is from a server that isn't allow-listed"),
        ("azure-mcp/*", "tool 'azure-mcp/*' is not in finops/allowlist.yaml (wildcards aren't allowed)"),
        ("azure-mcp/advisor_recommendation_update", "is not in finops/allowlist.yaml"),
    ],
)
def test_tools_must_be_allow_listed(root, tool, fragment):
    fm = frontmatter()
    fm["tools"].append(tool)
    write_profile(root, fm)
    [err] = check_agent_profile(root)
    assert err == f"{PROFILE}: {fragment}" or (err.startswith(f"{PROFILE}: ") and fragment in err)


def test_profile_tools_need_allowlist_entry(root):
    (root / "finops" / "allowlist.yaml").unlink()
    errors = check_agent_profile(root)
    assert f"{PROFILE}: tool 'azure-mcp/pricing_get' is from a server that isn't allow-listed" in errors
    assert f"{PROFILE}: MCP server 'azure-mcp' is not in finops/allowlist.yaml" in errors


def test_azure_finops_server_is_required(root):
    s = frontmatter()["mcp-servers"]
    del s["azure-finops"]
    write_profile(root, frontmatter(mcp_servers=s))
    assert check_agent_profile(root) == [f"{PROFILE}: mcp-servers must define 'azure-finops'"]
    write_profile(root, frontmatter(mcp_servers=None))
    assert check_agent_profile(root) == [f"{PROFILE}: mcp-servers must define 'azure-finops'"]


def test_azure_finops_server_must_run_our_server(root):
    s = frontmatter()["mcp-servers"]
    s["azure-finops"] = {"command": "node", "args": ["evil.js"]}
    write_profile(root, frontmatter(mcp_servers=s))
    assert check_agent_profile(root) == [f"{PROFILE}: server 'azure-finops' must run azure-finops-mcp"]
    s["azure-finops"] = {"command": "python", "args": ["-m", "azure_finops.server"]}
    write_profile(root, frontmatter(mcp_servers=s))
    assert check_agent_profile(root) == []
    for cmd, args in (
        ("evil-azure-finops-mcp-x", []),
        ("node", ["evil.js", "azure_finops"]),
        ("python", ["-m", "azure_finops_evil"]),
    ):
        s["azure-finops"] = {"command": cmd, "args": args}
        write_profile(root, frontmatter(mcp_servers=s))
        assert check_agent_profile(root) == [f"{PROFILE}: server 'azure-finops' must run azure-finops-mcp"]
    s["azure-finops"] = {"command": "/opt/venv/bin/azure-finops-mcp", "args": []}
    write_profile(root, frontmatter(mcp_servers=s))
    assert check_agent_profile(root) == []


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        (
            {"args": [a for a in AZURE_MCP_ARGS if a != "--read-only"]},
            ["MCP server 'azure-mcp' must be started with --read-only"],
        ),
        (
            {"args": AZURE_MCP_ARGS[:5]},
            ["MCP server 'azure-mcp' must restrict tools with --tool flags"],
        ),
        (
            {"args": [*AZURE_MCP_ARGS, "--tool", "advisor_recommendation_update"]},
            ["azure-mcp --tool advisor_recommendation_update is not allow-listed"],
        ),
        (
            {"args": [*AZURE_MCP_ARGS[:5], "--tool=pricing_get", "--tool=storage_list"]},
            ["azure-mcp --tool storage_list is not allow-listed"],
        ),
        ({"tools": ["*"]}, ["azure-mcp tools entry '*' is not allow-listed"]),
        (
            {"tools": ["pricing_get", "storage_list"]},
            ["azure-mcp tools entry 'storage_list' is not allow-listed"],
        ),
        (
            {"args": ["-y", "@azure/mcp@latest", *AZURE_MCP_ARGS[2:]]},
            ["azure-mcp must use the pinned package @azure/mcp@3.0.0"],
        ),
        (
            {"args": ["-y", "@azure/mcp", *AZURE_MCP_ARGS[2:]]},
            ["azure-mcp must use the pinned package @azure/mcp@3.0.0"],
        ),
    ],
)
def test_allowlisted_server_checks(root, over, expected):
    write_profile(root, frontmatter(mcp_servers=servers(**over)))
    assert check_agent_profile(root) == [f"{PROFILE}: {e}" for e in expected]


def test_unknown_server(root):
    s = frontmatter()["mcp-servers"]
    s["github"] = {"command": "npx", "args": ["--read-only", "--tool", "x"]}
    write_profile(root, frontmatter(mcp_servers=s))
    assert check_agent_profile(root) == [f"{PROFILE}: MCP server 'github' is not in finops/allowlist.yaml"]


@pytest.mark.parametrize("rel", [".mcp.json", ".github/mcp.json"])
def test_workspace_mcp_config_is_rejected(root, rel):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"mcpServers": {}}')
    [err] = check_agent_profile(root)
    assert err.startswith(f"{rel}: Copilot CLI loads workspace MCP servers from this file")


def vscode(root: Path, data) -> None:
    p = root / ".vscode" / "mcp.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data))


def test_vscode_mcp_must_match_profile(root):
    good = {
        "servers": {
            "azure-finops": {"type": "stdio", "command": "azure-finops-mcp", "args": []},
            "azure-mcp": {"type": "stdio", "command": "npx", "args": AZURE_MCP_ARGS},
        }
    }
    vscode(root, good)
    assert check_agent_profile(root) == []

    vscode(root, {"servers": {"azure-finops": good["servers"]["azure-finops"]}})
    assert check_agent_profile(root) == [
        ".vscode/mcp.json: servers ['azure-finops'] must match the profile's ['azure-finops', 'azure-mcp']"
    ]

    bad = json.loads(json.dumps(good))
    bad["servers"]["azure-mcp"]["args"] = AZURE_MCP_ARGS[:4] + AZURE_MCP_ARGS[5:]
    vscode(root, bad)
    assert check_agent_profile(root) == [
        ".vscode/mcp.json: MCP server 'azure-mcp' must be started with --read-only"
    ]


def test_vscode_mcp_invalid_json(root):
    vscode(root, "{not json")
    errors = check_agent_profile(root)
    assert errors[0].startswith(".vscode/mcp.json: ")
    assert errors[1] == (
        ".vscode/mcp.json: servers [] must match the profile's ['azure-finops', 'azure-mcp']"
    )
