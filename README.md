# azure-finops-agent

A read-only Azure FinOps agent built on GitHub Copilot. Ask it about Azure spend in Copilot CLI, VS Code, or a GitHub issue, and it answers with an evidence id behind every number. Scheduled reports and anomaly investigations run in GitHub Actions and post to issues in your repository.

It's a template: create your own private copy, point it at your subscriptions, and run it with your own Copilot plan and Azure access.

> Figures come from Azure Cost Management and Advisor through read-only queries. The agent can still misread a question or a result, so verify before acting. Advisor savings are Advisor's estimates.

## What it does

- **Answers cost questions**: spend by service, resource group, tag, or resource; period comparisons with like-for-like day counts; what drove a change.
- **Investigates anomalies**: a deterministic detector flags unusual daily spend, and the agent correlates it with Activity Log and resource changes, with a confidence level.
- **Summarizes savings**: Advisor cost recommendations ranked by savings and routed to owning teams.
- **Runs on a schedule**: describe a report in plain language ("first business day of the month"), review the generated YAML in a pull request, and it runs from then on.
- **Never changes Azure**: the MCP server only allows a fixed list of read requests, and the identity it uses in Actions has Reader and Cost Management Reader only.

```mermaid
flowchart LR
  You[You: Copilot CLI or VS Code] --> Agent
  Issue[GitHub issue question] --> Run
  Schedule[Schedules and anomaly detector] --> Run
  Run[GitHub Actions run] --> Agent
  Agent[azure-finops agent] --> MCP[FinOps MCP server<br/>read-only, does the math]
  MCP --> Azure[(Cost Management, Advisor,<br/>Resource Graph, Activity Log)]
  Run --> Post[Post job] --> Issues[(Issues in your repo)]
```

## Quickstart

Interactive use, with your own `az login`. You need Python 3.11+, the Azure CLI, Copilot CLI with a Copilot plan, and Reader plus Cost Management Reader on the subscriptions. Node.js is only needed for `npm` and the optional Azure MCP Server.

```bash
gh repo create my-org/finops --private --template sabajamalian/azure-finops-agent --clone
cd finops
python3 -m venv .venv && source .venv/bin/activate
pip install ./mcp-server
npm install --global @github/copilot
az login --tenant <tenant-id>
finops init                # writes finops/config.yaml from the subscriptions you can see
finops doctor --skip-github
copilot --agent azure-finops --allow-tool azure-finops
```

`--allow-tool azure-finops` pre-approves the FinOps server's tools, which are all read-only; without it Copilot asks before every call. On Windows, or to skip the local install, open the repository in a [dev container or Codespace](docs/setup.md#option-dev-container-or-codespace). The full walkthrough, with a checklist, is in [setup](docs/setup.md).

Headless runs (issue questions, schedules, anomalies) need one more step: `scripts/setup/setup.sh` creates a read-only managed identity and configures the repository. See [headless runs](docs/headless.md).

### What an answer looks like

This is the agent's answer format, filled in with tool output from the synthetic test tenant (`FINOPS_FIXTURE_MODE=synthetic`), trimmed for length.

> **You:** Why did spend in contoso-prod go up last week?
>
> Spend in contoso-prod rose 6,295.69 USD (+173.2%) for Mar 10 to 16 compared with Mar 3 to 9 (ev-001). A new GPU VM, vm-gpu-train-01, accounts for 94.1% of the increase (ev-005), and Log Analytics ingestion in rg-shared most of the rest.
>
> | Service | Mar 3 to 9 | Mar 10 to 16 | Change | Share of change |
> | --- | ---: | ---: | ---: | ---: |
> | Virtual Machines | 1,368.28 | 7,287.90 | +5,919.61 | 94.0% |
> | Log Analytics | 264.94 | 632.08 | +367.14 | 5.8% |
> | SQL Database | 967.95 | 981.56 | +13.61 | 0.2% |
>
> **Likely cause (medium confidence):** alice@contoso.com created vm-gpu-train-01 (NC6s v3) in rg-ml through the Azure portal on Mar 12 at 09:36 UTC (ev-009). That lines up in time and resource group with the increase, but correlation isn't proof, so confirm with alice.
>
> Actual cost, USD, UTC days. Mar 15 and 16 may still be incomplete.
>
> **Evidence**
>
> - ev-001: Cost Management by service, subscription contoso-prod, Mar 3 to 16, ActualCost, USD
> - ev-005: Cost Management by resource, same frame
> - ev-009: Activity Log, rg-ml, Mar 9 to 13
>
> Figures come from Azure Cost Management and Advisor via read-only queries; verify before acting.

## Before you use it

- **Keep the repository private.** Issues, workflow logs, and run artifacts contain spend data. The post job refuses to publish to a public repository unless you override it.
- **Copilot is the cost.** Each prompt, issue question, scheduled report, and anomaly investigation uses premium requests. The Azure reads are free. See [Cost of running it](docs/usage.md#cost-of-running-it).
- **Check your Copilot plan's training setting.** On Copilot Free, Pro, and Pro+, interaction data is used for model training unless you opt out. Business and Enterprise data isn't. See [Model training](docs/data-handling.md#model-training).
- **Not yet run end to end.** The test suite runs against synthetic Azure data and mocked GitHub APIs. A live run against a real subscription and Copilot in Actions is the first thing to try after setup. See [Open questions](docs/security.md#open-questions).

## Documentation

| Doc | What's in it |
| --- | --- |
| [Setup](docs/setup.md) | Checklist, prerequisites, config, local install (macOS, Linux, Windows, dev container), interactive use |
| [Headless runs](docs/headless.md) | `setup.sh`, Copilot authentication in Actions, automations, teardown |
| [Usage](docs/usage.md) | Example questions, issue questions, automations, anomaly detection, CLI reference |
| [Architecture](docs/architecture.md) | MCP server, tools, headless run flow, task record, detector, design decisions, what's not in this version |
| [Security](docs/security.md) | Permissions, who can start a run, prompt injection, supply chain, known limitations |
| [Data handling](docs/data-handling.md) | What's stored where, retention and deletion, model hosting, training |

## Repository layout

| Path | Contents |
| --- | --- |
| [`.github/agents/azure-finops.agent.md`](.github/agents/azure-finops.agent.md) | Agent profile: scope, financial rules, answer format, allowed tools |
| [`.github/workflows/`](.github/workflows) | `finops-run` (headless runs), `finops-ask`, `finops-dispatch`, `finops-anomaly`, `finops-evals`, `ci` |
| [`mcp-server/`](mcp-server/README.md) | The FinOps MCP server and `finops` CLI (Python) |
| [`finops/`](finops) | Config example, tool allow-list, automations, context files, JSON schemas |
| [`scripts/setup/`](scripts/setup) | `setup.sh` and Bicep for the managed identity and role assignments |
| [`evals/`](evals/README.md) | Agent eval cases against synthetic data |
| [`.devcontainer/`](.devcontainer/devcontainer.json) | Dev container with Python, Node, Azure CLI, GitHub CLI, and Copilot CLI |

## Development

```bash
pip install -e "./mcp-server[dev]"
cd mcp-server && ruff format --check . && ruff check . && pytest
```

Changes are listed in [CHANGELOG.md](CHANGELOG.md). CI also checks that the JSON schemas are current (`finops schemas export --check`), that the agent profile matches the allow-list (`finops check-agent-profile`), that automations are valid, and runs actionlint on the workflows.
