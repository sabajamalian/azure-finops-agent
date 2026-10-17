# azure-finops-agent (MCP server and CLI)

The Python package behind the `azure-finops` Copilot agent. It installs two commands:

- `azure-finops-mcp`: a stdio MCP server with read-only Azure FinOps tools. Copilot CLI and VS Code start it for you from the agent profile or `.vscode/mcp.json`.
- `finops`: setup, checks, and the steps the GitHub Actions workflows run.

Setup for the whole template is in [docs/setup.md](../docs/setup.md). This page covers the package itself.

## Install

From the repository root:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install ./mcp-server            # or: pip install -e "./mcp-server[dev]" for development
```

Requires Python 3.11 or later. Azure calls use `DefaultAzureCredential`, so locally that's your `az login`, and in Actions the federated managed identity.

## Tools

All tools are read-only against Azure. Amounts, deltas, and percentages are computed by the server, and each result carries evidence ids.

| Tool | What it does |
| --- | --- |
| `get_capabilities` | Which data sources each configured subscription can reach, with fixes for what's missing |
| `list_scopes` | Configured subscriptions, their resource groups, the default cost basis, and time zone |
| `query_costs` | Cost Management query grouped by service, resource group, tag, resource, or meter |
| `compare_periods` | Two periods compared with like-for-like day counts, ranked deltas, and reconciliation |
| `explain_drivers` | The resources and meters behind a change |
| `get_advisor_cost_recommendations` | Advisor cost recommendations with Advisor's savings estimates |
| `get_change_history` | Who changed what: Activity Log writes and deletes (90 days) and Resource Graph resource changes (14 days) |
| `correlate_changes` | Changes near a cost increase, scored with a confidence level |
| `detect_anomalies` | The deterministic daily anomaly detector |
| `query_dataset` | Read-only SQL (SQLite dialect) over datasets returned earlier in the session |
| `get_evidence` | The exact query, scope, period, basis, and currency behind an evidence id |
| `list_context`, `read_context` | Team notes in `finops/context/` |
| `list_automations`, `draft_automation` | List automations, or validate and preview a new one; writes nothing |
| `submit_result` | Headless runs: validate the result and write `result.json` for the post job. Never posts anything itself. |

Design notes, the task record, and the detector are described in [docs/architecture.md](../docs/architecture.md).

## CLI

`finops --help` lists every command. The ones you'll use by hand are `finops init`, `finops doctor`, `finops automations`, `finops detect`, `finops check-agent-profile`, and `finops evals`. See the [CLI reference](../docs/usage.md#cli-reference).

## Running the server directly

```bash
azure-finops-mcp [--repo-root PATH] [--output-dir PATH]
```

It finds `finops/config.yaml` from the current directory (or `--repo-root`) and exits with an error if the config is invalid. `--output-dir` is for headless runs, where it writes `result.json`, `evidence.jsonl`, and `audit.jsonl`.

## Environment variables

| Variable | Effect |
| --- | --- |
| `FINOPS_REPO_ROOT` | Repository root containing `finops/`. Default: discovered from the current directory. |
| `FINOPS_CONFIG` | Path to a config file other than `finops/config.yaml`. |
| `FINOPS_CACHE_DIR`, `FINOPS_CACHE_TTL` | Query cache location (default `.finops/cache`) and lifetime in seconds (default 900). |
| `FINOPS_FIXTURE_MODE=synthetic` | Serve a built-in synthetic tenant instead of calling Azure. Used by the tests and evals. |
| `FINOPS_FIXED_TODAY`, `FINOPS_SYNTHETIC_DENY` | Fixture mode only: pin "today", and deny data sources per subscription (`cost:<sub>,arg:<sub>,...`). |

## Development

```bash
pip install -e "./mcp-server[dev]"
cd mcp-server
ruff format --check . && ruff check . && pytest
```

From the repository root, CI also runs `finops schemas export --check` (regenerate with `finops schemas export` after changing config or automation models) and `finops check-agent-profile`.
