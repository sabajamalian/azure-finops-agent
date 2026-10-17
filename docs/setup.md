# Setup

This page gets the agent running on your machine with your own `az login`. Running it from GitHub issues, on a schedule, or on anomalies is a second step covered in [headless runs](headless.md).

- [Checklist](#checklist)
- [Prerequisites](#prerequisites)
- [1. Create your repository](#1-create-your-repository)
- [2. Install](#2-install)
- [3. Configure](#3-configure)
- [4. Check access](#4-check-access)
- [5. Use it interactively](#5-use-it-interactively)
- [EA and MCA cost visibility](#ea-and-mca-cost-visibility)
- [Add, limit, or remove MCP servers](#add-limit-or-remove-mcp-servers)
- [Troubleshooting](#troubleshooting)

## Checklist

Copy this into an issue or your notes and tick it off.

**Local use**

- [ ] Private repository created from the template
- [ ] `.github/CODEOWNERS` points at a real team, and the default branch requires Code Owner review
- [ ] Virtual environment created and `pip install ./mcp-server` done (or the dev container is running)
- [ ] Copilot CLI installed and signed in
- [ ] `az login --tenant <tenant-id>` done
- [ ] `finops init` wrote `finops/config.yaml`, and it's committed
- [ ] `finops doctor --skip-github` shows no failures
- [ ] First question answered in `copilot --agent azure-finops`

**Headless runs** (see [headless runs](headless.md))

- [ ] `scripts/setup/setup.sh --dry-run` reviewed, then `scripts/setup/setup.sh` run
- [ ] Copilot authentication decided: workflow token or a fine-grained PAT
- [ ] On EA or MCA billing, subscription readers can see charges
- [ ] A manual `finops-run` posted an answer issue
- [ ] Automations you want turned on in a reviewed pull request

## Prerequisites

| Need | Why |
| --- | --- |
| A GitHub Copilot plan that includes Copilot CLI. On Copilot Business or Enterprise, the CLI policy must be enabled for you. | The agent runs in Copilot CLI or VS Code. |
| Python 3.11 or later (the workflows use 3.12) | The FinOps MCP server and the `finops` CLI. |
| Azure CLI | Your sign-in, and `finops init`. |
| Reader and Cost Management Reader on each subscription you want to analyze | Cost queries, Resource Graph, Activity Log, and Advisor. Reader alone also works. |
| Node.js 22 or later (optional) | Installing Copilot CLI with `npm`, and `npx` for the Azure MCP Server. See [Without Node.js](#without-nodejs). |
| GitHub CLI | Creating the repository from the command line, and later `setup.sh`. |

On EA or MCA billing, subscription-level roles aren't enough by themselves. See [EA and MCA cost visibility](#ea-and-mca-cost-visibility).

## 1. Create your repository

Create a **private** repository from this template (**Use this template > Create a new repository**), or with the GitHub CLI:

```bash
gh repo create my-org/finops --private --template sabajamalian/azure-finops-agent --clone
cd finops
```

Keep it private or internal. Issues, workflow logs, and run artifacts contain spend data, and the post job refuses to publish to a public repository unless you override it. One copy of the template serves one tenant; see [data handling](data-handling.md#isolation).

Then protect what the agent is allowed to do. Replace `@your-org/finops-owners` in `.github/CODEOWNERS` with a real team or user. On the default branch, add a branch protection rule or ruleset that requires a pull request with **Require review from Code Owners**. That puts the config, allowlist, automations, agent profile, and workflows behind an owner's review.

## 2. Install

Pick one of the options below. All of them end with `finops` and `azure-finops-mcp` on your `PATH`.

### macOS and Linux

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install ./mcp-server
npm install --global @github/copilot
```

Use the `.venv` name: `.vscode/mcp.json` starts the server from `.venv/bin/azure-finops-mcp`, and `finops doctor` warns if it isn't there.

Installing the latest Copilot CLI is fine for interactive use. The workflows pin a tested version (currently `1.0.98`, in `.github/workflows/finops-run.yml`); if the agent behaves differently locally than in Actions, try `npm install --global @github/copilot@1.0.98`.

### Windows

In PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install ./mcp-server
npm install --global @github/copilot
```

Then change the `azure-finops` command in `.vscode/mcp.json` to `${workspaceFolder}/.venv/Scripts/azure-finops-mcp.exe`. Copilot CLI doesn't use that file, so only VS Code needs the change.

The CLI, the MCP server, and interactive use work natively on Windows. `scripts/setup/setup.sh` for [headless runs](headless.md) is a Bash script: run it from WSL, Git Bash, or a Codespace.

### Option: dev container or Codespace

The repository includes `.devcontainer/devcontainer.json` with Python 3.12, Node.js, the Azure CLI, the GitHub CLI, and Copilot CLI. Open the folder in VS Code and pick **Reopen in Container**, or create a Codespace from your repository. Setup creates `.venv`, installs the server, and puts it on the `PATH`, so skip straight to [Configure](#3-configure). Sign in with `az login --use-device-code` inside the container.

### Without Node.js

Node.js is only used for two things:

- Installing Copilot CLI with `npm`. You can install it another way instead; see [Installing GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli).
- Starting the optional Azure MCP Server with `npx`. Without `npx`, the agent still works: the FinOps server covers costs, changes, anomalies, and Advisor on its own. You lose retail price lookups. `finops doctor` reports a missing `npx` as a warning, not a failure.

The first time the agent starts, `npx` downloads the Azure MCP Server package, which can take a minute. To do that ahead of time:

```bash
npx -y @azure/mcp@3.0.0 --help > /dev/null
```

## 3. Configure

Sign in to the tenant you want to analyze, then let `finops init` write the config from the subscriptions you can see:

```bash
az login --tenant <tenant-id>
finops init
```

It lists the enabled subscriptions in your current tenant and asks which ones to include. Non-interactive forms:

```bash
finops init --all                                    # every enabled subscription in the tenant
finops init --subscription contoso-prod --subscription 2222...   # by name or ID, repeatable
finops init --tenant <tenant-id> --timezone Europe/London --force
```

It starts from `finops/config.example.yaml`, keeps its comments, and fills in `azure.tenant_id`, `azure.subscriptions`, and `timezone` (your machine's time zone unless you pass `--timezone`). It won't overwrite an existing `finops/config.yaml` without `--force`. To write the file by hand instead, copy `finops/config.example.yaml` to `finops/config.yaml` and replace the placeholder IDs.

The other settings have working defaults:

| Setting | Default | Notes |
| --- | --- | --- |
| `azure.cost_basis` | `ActualCost` | `AmortizedCost` spreads reservation and savings plan purchases across the term. |
| `timezone` | Your machine's time zone with `finops init` | Used for report dates and as the default for schedules. Cost data itself is UTC. |
| `agent.model` | CLI default | Pin a model for headless runs so scheduled reports stay comparable. |
| `anomaly.*` | see file | Detector thresholds and the daily investigation cap. See [architecture](architecture.md#anomaly-detector). |
| `destinations` | `finops-reports`, `finops-actions` | Where headless runs may post. Each is a set of issue labels. |
| `retention_days` | 30 | How long Actions keeps run records. |
| `max_issues_per_run` | 5 | Cap on new action issues per run. |
| `allow_public_repo_output` | `false` | Leave it `false`. |

Your editor validates the file against `finops/schemas/config.schema.json`. Commit `config.yaml`, because headless runs read it from the default branch. Tenant and subscription IDs aren't secrets.

Optionally, add team context such as owners and cost centers in `finops/context/`. Read [finops/context/README.md](../finops/context/README.md) first: anything you put there is sent to the model on every run.

## 4. Check access

```bash
finops doctor --skip-github
```

`finops doctor` checks the config, the automation files, the agent profile against `finops/allowlist.yaml`, the VS Code server path, and the tools on your `PATH`. It then checks that `az` is signed in to the configured tenant, and that each subscription can reach Cost Management, Resource Graph, Advisor, and the Activity Log. Each failure comes with a fix. If a subscription is unreachable altogether, it shows one line for it instead of four errors.

Once you've set up [headless runs](headless.md), drop `--skip-github` to also check repository visibility and the `finops` environment. It needs a token and the repository name:

```bash
GITHUB_TOKEN=$(gh auth token) GITHUB_REPOSITORY=my-org/finops finops doctor
```

## 5. Use it interactively

**Copilot CLI**, from the repository root:

```bash
copilot --agent azure-finops --allow-tool azure-finops
```

Copilot asks you to trust the folder on first use. The agent profile (`.github/agents/azure-finops.agent.md`) starts the FinOps MCP server and the read-only Azure MCP Server for you. `--allow-tool azure-finops` pre-approves the FinOps server's tools, which are all read-only. Leave it off if you'd rather approve each call.

**VS Code**: open the folder. `.vscode/mcp.json` defines the same two servers and starts the FinOps server from `.venv`. Pick **azure-finops** from the agent picker in Chat. If you installed somewhere other than `.venv`, put the full path to `azure-finops-mcp` in `.vscode/mcp.json`.

Your sign-in decides what the agent can see. The MCP servers use your `az login` and never change Azure resources. See [usage](usage.md) for example questions.

## EA and MCA cost visibility

Cost Management Reader on a subscription only returns costs if the billing account lets subscription users see charges:

- **Enterprise Agreement**: an enterprise administrator enables **AO view charges** (Account owners can view charges) in the enrollment's policies.
- **Microsoft Customer Agreement**: a billing profile owner sets the profile's **Azure charges** policy so that users with access to subscriptions can view charges.

Without this, `finops doctor` and the agent report a permission error from Cost Management, and the error includes the fix. See Microsoft's [Assign access to Cost Management data](https://learn.microsoft.com/azure/cost-management-billing/costs/assign-access-acm-data).

## Add, limit, or remove MCP servers

The agent can use the FinOps MCP server and the third-party servers listed in `finops/allowlist.yaml`. Out of the box that's only the Azure MCP Server, limited to three read-only tools.

To add a server:

1. Add it to `finops/allowlist.yaml`. Pin an exact version, start it in read-only mode if it has one, and list the exact tools to allow and any to deny.
2. Add the same server and tools to `mcp-servers:` and `tools:` in `.github/agents/azure-finops.agent.md`, and to `.vscode/mcp.json`.
3. Run `finops check-agent-profile`. CI runs it too, and fails if the profile or `.vscode/mcp.json` enables a server or tool that isn't in the allowlist.

Set `headless: false` on a server to keep it out of Actions runs while still using it interactively. To remove the Azure MCP Server, delete it from all three files; the FinOps server covers Advisor on its own.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `finops doctor`: `config ids` fails | `finops/config.yaml` still has the example's placeholder IDs. Run `finops init --force`. |
| `finops doctor`: `az tenant` fails | `az` is signed in to a different tenant than the config. Run the `az login --tenant` command it prints. |
| `finops doctor`: a subscription shows `all data sources` failing | Your sign-in can't see that subscription. Check `az account list -o table`, or remove it from the config. |
| VS Code can't start `azure-finops` | `.vscode/mcp.json` points at `.venv/bin/azure-finops-mcp`. Create the virtual environment there, or edit the path (Windows: `.venv/Scripts/azure-finops-mcp.exe`). |
| The first answer takes a minute to start | `npx` is downloading the Azure MCP Server. Prefetch it as shown in [Without Node.js](#without-nodejs). |
| Copilot asks before every tool call | Start it with `--allow-tool azure-finops`. |
| Cost queries return 401 or 403 | Missing Cost Management Reader, or EA/MCA charge visibility is off. Run `finops doctor`. |
| The last day or two look low | Cost Management data for the most recent 1 to 2 days is usually incomplete. Reports state the date the data runs through, and the anomaly detector skips those days. |
| `429` or "rate limited" errors | Cost Management limits queries per scope. The server waits and retries, and caches results for 15 minutes. Ask for fewer, broader queries. |

Problems with Actions runs are covered in [headless runs](headless.md#troubleshooting).
