# Headless runs

Headless runs answer questions from GitHub issues, run scheduled automations, and investigate anomalies in GitHub Actions. They need [local setup](setup.md) first, at least through a committed `finops/config.yaml`.

- [Before you start](#before-you-start)
- [1. Run setup.sh](#1-run-setupsh)
- [2. Check a headless run](#2-check-a-headless-run)
- [3. Turn on automations](#3-turn-on-automations)
- [Teardown](#teardown)
- [Troubleshooting](#troubleshooting)

## Before you start

| Need | Why |
| --- | --- |
| `finops/config.yaml` committed to the default branch | Every run reads it from there. |
| Azure CLI, signed in to the configured tenant with Owner, User Access Administrator, or Role Based Access Control Administrator on each subscription | `setup.sh` creates the identity and its role assignments. If another team owns Azure, see the `--skip-azure` option below. |
| GitHub CLI, signed in with admin rights on the repository | `setup.sh` creates the `finops` environment, variables, and label. |
| Bash | `setup.sh` is a Bash script. On Windows use WSL, Git Bash, or a Codespace. |
| On EA or MCA billing, charge visibility for subscription readers | See [EA and MCA cost visibility](setup.md#ea-and-mca-cost-visibility). |

## 1. Run setup.sh

Preview the setup first, then run it:

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
5. Reports how Copilot will authenticate (see [Copilot authentication](#copilot-authentication)).

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

## 2. Check a headless run

```bash
gh workflow run finops-run.yml -f kind=manual -f question="What did each subscription cost last month?"
```

Or use **Actions > finops-run > Run workflow**. The answer is posted as a new issue labeled `finops-report`. If something fails, the run comments on a single open issue labeled `finops-failure` and links the run. The task record artifact holds the transcript and audit log; see [architecture](architecture.md#task-record).

## 3. Turn on automations

Automations are YAML files in `finops/automations/`. The two shipped ones are disabled:

- `weekly-spend-summary.yaml` posts a weekly report.
- `investigate-anomalies.yaml` lets the daily anomaly scan start investigations.

Set `enabled: true` in a pull request. With the CODEOWNERS rule from [setup](setup.md#1-create-your-repository), an owner has to approve it, and each run records which pull request approved the file. Check your changes with `finops automations validate`. The file format, schedule rules, and limits are in [finops/automations/README.md](../finops/automations/README.md).

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
| An issue labeled `finops-ask` gets no answer | Only owners, members, and collaborators can ask. Check that the `finops-ask` label exists and that `FINOPS_ENABLED` is `true`. |
| `The agent ... without submitting a result` | The agent timed out, exited early, or never started. Read `transcript.md` and `copilot-logs/` in the task record artifact. Authentication, timeout, and model errors show up there. |
