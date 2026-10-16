# Setup

Interactive use needs four things: a config file, the MCP server installed, `az login`, and Copilot started with the `azure-finops` agent. Headless runs in GitHub Actions add one script, `scripts/setup/setup.sh`. It creates a read-only managed identity and configures the repository.

- [Prerequisites](#prerequisites)
- [1. Create your repository](#1-create-your-repository)
- [2. Configure](#2-configure)
- [3. Install and check access](#3-install-and-check-access)
- [4. Use it interactively](#4-use-it-interactively)
- [5. Set up headless runs](#5-set-up-headless-runs)
- [6. Turn on automations](#6-turn-on-automations)
- [Add, limit, or remove MCP servers](#add-limit-or-remove-mcp-servers)
- [Teardown](#teardown)
- [Troubleshooting](#troubleshooting)

## Prerequisites

| Need | Why |
| --- | --- |
| A GitHub Copilot plan that includes Copilot CLI. On Copilot Business or Enterprise, the CLI policy must be enabled for you. | The agent runs in Copilot CLI or VS Code. |
| Python 3.11 or later (the workflows use 3.12) | The FinOps MCP server and the `finops` CLI. |
| Node.js 22 or later | Copilot CLI, and `npx` for the Azure MCP Server. |
| Azure CLI | Local sign-in, and identity setup for headless runs. |
| Reader and Cost Management Reader on each subscription you want to analyze | Cost queries, Resource Graph, Activity Log, and Advisor. Reader alone also works. |
| GitHub CLI, signed in with admin rights on the repository | Only for `setup.sh`. |

On EA or MCA billing, subscription-level roles aren't enough by themselves. See [EA and MCA cost visibility](#ea-and-mca-cost-visibility).

## 1. Create your repository

Create a **private** repository from this template (**Use this template > Create a new repository**), or with the GitHub CLI:

```bash
gh repo create my-org/finops --private --template sabajamalian/azure-finops-agent --clone
```

Keep it private or internal. Issues, workflow logs, and run artifacts contain spend data, and the post job refuses to publish to a public repository unless you override it. One copy of the template serves one tenant; see [data handling](data-handling.md#isolation).

Then protect what the agent is allowed to do. Replace `@your-org/finops-owners` in `.github/CODEOWNERS` with a real team or user. On the default branch, add a branch protection rule or ruleset that requires a pull request with **Require review from Code Owners**. That puts the config, allowlist, automations, agent profile, and workflows behind an owner's review.

## 2. Configure

```bash
cp finops/config.example.yaml finops/config.yaml
```

Fill in `azure.tenant_id` and `azure.subscriptions`. The other settings have working defaults:

| Setting | Default | Notes |
| --- | --- | --- |
| `azure.cost_basis` | `ActualCost` | `AmortizedCost` spreads reservation and savings plan purchases across the term. |
| `timezone` | `America/Los_Angeles` | Used for report dates and as the default for schedules. Cost data itself is UTC. |
| `agent.model` | CLI default | Pin a model for headless runs so scheduled reports stay comparable. |
| `anomaly.*` | see file | Detector thresholds and the daily investigation cap. See [architecture](architecture.md#anomaly-detector). |
| `destinations` | `finops-reports`, `finops-actions` | Where headless runs may post. Each is a set of issue labels. |
| `retention_days` | 30 | How long Actions keeps run records. |
| `max_issues_per_run` | 5 | Cap on new action issues per run. |
| `allow_public_repo_output` | `false` | Leave it `false`. |

Your editor validates the file against `finops/schemas/config.schema.json`. Commit `config.yaml`, because headless runs read it from the default branch. Tenant and subscription IDs aren't secrets.

Optionally, add team context such as owners and cost centers in `finops/context/`. Read [finops/context/README.md](../finops/context/README.md) first: anything you put there is sent to the model on every run.

## 3. Install and check access

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install ./mcp-server
npm install --global @github/copilot@1.0.98   # the version the workflows pin
az login --tenant <tenant-id>
finops doctor --skip-github
```

`finops doctor` checks the config, the automation files, the agent profile against `finops/allowlist.yaml`, and the tools on your `PATH`. It also checks Azure access to every configured subscription: Cost Management, Resource Graph, Advisor, and the Activity Log. Each failure comes with a fix. Run it again with `GITHUB_TOKEN` and `GITHUB_REPOSITORY` set (or drop `--skip-github`) to also check repository visibility and the `finops` environment.

## 4. Use it interactively

**Copilot CLI**, from the repository root:

```bash
copilot --agent azure-finops
```

Copilot asks you to trust the folder on first use. The agent profile (`.github/agents/azure-finops.agent.md`) starts the FinOps MCP server and the read-only Azure MCP Server for you. Copilot asks before each tool call. Every FinOps tool is read-only, so you can pre-approve the whole server:

```bash
copilot --agent azure-finops --allow-tool azure-finops
```

**VS Code**: open the folder. `.vscode/mcp.json` defines the same two servers. Pick **azure-finops** from the agent picker in Chat. VS Code has to find `azure-finops-mcp` on its `PATH`, so start it from the activated virtual environment (`code .`) or put the full path in `.vscode/mcp.json`.

Your sign-in decides what the agent can see. The MCP servers use your `az login` and never change Azure resources. See [usage](usage.md) for example questions.

## 5. Set up headless runs

Headless runs answer questions from issues, run scheduled automations, and investigate anomalies. Preview the setup first, then run it:

```bash
scripts/setup/setup.sh --dry-run
scripts/setup/setup.sh
```

The dry run prints every command that would change something and only runs read-only queries. The script is safe to re-run. It does the following:

**In Azure**

1. Creates resource group `rg-finops-agent` in the first configured subscription.
2. Deploys `scripts/setup/identity.bicep`: a user-assigned managed identity, `id-finops-agent`, with one federated credential. Its subject is `repo:<owner>/<repo>:environment:finops`, so only jobs that use the `finops` environment in this repository can sign in as it.
3. Deploys `scripts/setup/roles.bicep` to every configured subscription, assigning Reader and Cost Management Reader. These are the only roles the identity gets.

You need rights to create role assignments on each subscription: Owner, User Access Administrator, or Role Based Access Control Administrator. If another team owns Azure, give them the two Bicep files. Then run the script with `--skip-azure --client-id <identity client id>`.

**In GitHub**

1. Creates the `finops` environment and restricts it to the default branch.
2. Sets the environment variables `AZURE_CLIENT_ID` and `AZURE_TENANT_ID`. They aren't secrets, and the workflows use OIDC, so no Azure secret is stored.
3. Creates the `finops-ask` label. The question issue form needs it to exist.
4. Sets the repository variable `FINOPS_ENABLED=true`, which every workflow checks before doing anything. Skip this with `--no-enable`. The shipped automations are disabled, so turning this on doesn't use any premium requests by itself. It enables questions from issues and the daily anomaly scan, which doesn't use Copilot.
5. Reports how Copilot will authenticate (next section).

| Option | Default |
| --- | --- |
| `--repo OWNER/NAME` | The current repository |
| `--resource-group`, `--location` | `rg-finops-agent`, `eastus` |
| `--identity-subscription`, `--identity-name` | First configured subscription, `id-finops-agent` |
| `--skip-azure --client-id ID` | Use an existing identity |
| `--skip-github` | Azure only |
| `--no-enable` | Leave `FINOPS_ENABLED` alone |

### Copilot authentication

The run job passes the `COPILOT_GITHUB_TOKEN` environment secret to Copilot CLI if it exists. Otherwise it passes the job's own `GITHUB_TOKEN`, which has the `copilot-requests: write` permission. Most repositories need no token at all.

| | Workflow `GITHUB_TOKEN` (default) | Fine-grained PAT |
| --- | --- | --- |
| Setup | None | Create a fine-grained PAT with the **Copilot Requests** permission, then `gh secret set COPILOT_GITHUB_TOKEN --env finops` |
| Who pays | Personal repository: the owner's Copilot seat. Organization repository: metered to the organization. | The PAT owner's seat, and their license decides the models available. |
| Requirement | In an organization, an owner must allow **Copilot CLI > Allow use of Copilot CLI billed to the organization** in Copilot policies. It's on by default where Copilot CLI is enabled. | The PAT owner has a Copilot license. Classic PATs (`ghp_`) don't work. |
| Credential | Short-lived, issued per job | Long-lived. Set an expiry and rotate it. |

Use the PAT if your organization won't enable the billing policy, or if usage must count against a specific user. User-level Copilot budgets don't apply to organization-billed usage, so use [cost centers](https://docs.github.com/en/billing/concepts/cost-centers) to cap it. See GitHub's [Copilot CLI in GitHub Actions](https://docs.github.com/en/copilot/concepts/agent-capabilities/agent-powered-workflows-and-operations/copilot-cli-in-github-actions) for details.

The workflow log of each run says which credential it used.

### EA and MCA cost visibility

Cost Management Reader on a subscription only returns costs if the billing account lets subscription users see charges:

- **Enterprise Agreement**: an enterprise administrator enables **AO view charges** (Account owners can view charges) in the enrollment's policies.
- **Microsoft Customer Agreement**: a billing profile owner sets the profile's **Azure charges** policy so that users with access to subscriptions can view charges.

Without this, `finops doctor` and the agent report a permission error from Cost Management, and the error includes the fix. See Microsoft's [Assign access to Cost Management data](https://learn.microsoft.com/azure/cost-management-billing/costs/assign-access-acm-data).

### Check a headless run

```bash
gh workflow run finops-run.yml -f kind=manual -f question="What did each subscription cost last month?"
```

Or use **Actions > finops-run > Run workflow**. The answer is posted as a new issue labeled `finops-report`. If something fails, the run comments on a single open issue labeled `finops-failure` and links the run. The task record artifact holds the transcript and audit log; see [architecture](architecture.md#task-record).

## 6. Turn on automations

Automations are YAML files in `finops/automations/`. The two shipped ones are disabled:

- `weekly-spend-summary.yaml` posts a weekly report.
- `investigate-anomalies.yaml` lets the daily anomaly scan start investigations.

Set `enabled: true` in a pull request. With the CODEOWNERS rule from step 1, an owner has to approve it, and each run records which pull request approved the file. Check your changes with `finops automations validate`. The file format, schedule rules, and limits are in [finops/automations/README.md](../finops/automations/README.md).

## Add, limit, or remove MCP servers

The agent can use the FinOps MCP server and the third-party servers listed in `finops/allowlist.yaml`. Out of the box that's only the Azure MCP Server, limited to three read-only tools.

To add a server:

1. Add it to `finops/allowlist.yaml`. Pin an exact version, start it in read-only mode if it has one, and list the exact tools to allow and any to deny.
2. Add the same server and tools to `mcp-servers:` and `tools:` in `.github/agents/azure-finops.agent.md`, and to `.vscode/mcp.json`.
3. Run `finops check-agent-profile`. CI runs it too, and fails if the profile or `.vscode/mcp.json` enables a server or tool that isn't in the allowlist.

Set `headless: false` on a server to keep it out of Actions runs while still using it interactively. To remove the Azure MCP Server, delete it from all three files; the FinOps server covers Advisor on its own.

## Teardown

**Azure**. Remove the role assignments before you delete the identity; otherwise they become orphaned "Identity not found" entries:

```bash
PRINCIPAL_ID=$(az identity show -g rg-finops-agent -n id-finops-agent --subscription <identity-sub> --query principalId -o tsv)
for sub in $(finops config subscriptions); do
  az role assignment delete --assignee "$PRINCIPAL_ID" --scope "/subscriptions/$sub"
  az deployment sub delete --subscription "$sub" --name finops-agent-roles
done
az group delete --name rg-finops-agent --subscription <identity-sub>
```

**GitHub**. Deleting the environment also deletes its variables and secrets:

```bash
gh variable delete FINOPS_ENABLED
gh api --method DELETE "repos/<owner>/<repo>/environments/finops"
for l in finops finops-ask finops-report finops-anomaly finops-action finops-failure; do gh label delete "$l" --yes; done
```

If you used a PAT, revoke it. Issues, artifacts, and logs that hold spend data are covered in [data handling](data-handling.md#what-is-stored-where).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Workflow runs show as skipped | `FINOPS_ENABLED` isn't `true`. Run `gh variable set FINOPS_ENABLED --body true`. |
| `Branch "x" is not allowed to deploy to finops` | The `finops` environment only accepts the default branch. Run the workflow from the default branch. |
| Azure login: `No matching federated identity record found` | The federated subject doesn't match. It must be `repo:<owner>/<repo>:environment:finops`. If you renamed or transferred the repository, run `setup.sh` again. |
| Copilot: `Authentication failed` | Organization repository: ask an owner to enable the Copilot CLI billing policy, or add a PAT. With a PAT: check that it has Copilot Requests, hasn't expired, and belongs to a licensed user. |
| Copilot: `Classic Personal Access Tokens (ghp_) are not supported` | Create a fine-grained PAT instead. |
| Cost queries return 401 or 403 | Missing Cost Management Reader, or EA/MCA charge visibility is off. Run `finops doctor`. |
| An issue labeled `finops-ask` gets no answer | Only owners, members, and collaborators can ask. Check that the `finops-ask` label exists and that `FINOPS_ENABLED` is `true`. |
| The last day or two look low | Cost Management data for the most recent 1 to 2 days is usually incomplete. Reports state the date the data runs through, and the anomaly detector skips those days. |
| `429` or "rate limited" errors | Cost Management limits queries per scope. The server waits and retries, and caches results for 15 minutes. Ask for fewer, broader queries. |
| `The agent ... without submitting a result` | The agent timed out, exited early, or never started. Read `transcript.md` and `copilot-logs/` in the task record artifact. Authentication, timeout, and model errors show up there. |
