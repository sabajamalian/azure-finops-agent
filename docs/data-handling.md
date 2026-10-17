# Data handling

Spend data ends up in four places: your GitHub repository (issues, Actions logs, and artifacts), the model provider while a request is processed, the machine of anyone who uses the agent interactively, and, by default, their synced Copilot session history. None of it is committed to Git. This page covers where it's stored, who can see it, how to delete it, and what happens to it at the model provider.

- [Isolation](#isolation)
- [Encryption at rest](#encryption-at-rest)
- [What is stored where](#what-is-stored-where)
- [Model hosting and data residency](#model-hosting-and-data-residency)
- [Model training](#model-training)
- [Sensitive data in context files](#sensitive-data-in-context-files)

## Isolation

One copy of this template is one agent for one tenant. Each copy has its own managed identity, config, automations, issues, and run records, and there's no shared service between copies.

- The identity's federated credential names one repository and environment (`repo:<owner>/<repo>:environment:finops`), so another repository, including a fork, can't sign in as it.
- Headless runs start Copilot with an empty, temporary `COPILOT_HOME` on a GitHub-hosted runner that's discarded after the job. No memory, instructions, or sessions carry over between runs.
- Interactive sessions run as the person using them, with their own Azure access, and their session history stays with them.

Inside one copy there's no per-user partition. Anyone with read access to the repository can read every issue, workflow log, and artifact. If two teams must not see each other's spend, give each its own copy and its own identity, scoped to their subscriptions.

## Encryption at rest

| Where | Encryption |
| --- | --- |
| GitHub: issues, comments, Actions logs, artifacts | Managed by GitHub. GitHub has stated that [Git data is encrypted at rest](https://github.blog/changelog/2019-05-22-git-data-encryption-at-rest/); for issues, logs, and artifacts, check the [GitHub Trust Center](https://github.com/trust-center) and your agreement. This template adds no customer-managed keys. |
| Your machine: `.finops/`, `~/.copilot/session-state/` | Plain files. Use full-disk encryption (FileVault or BitLocker). |
| Model providers | Covered by the provider terms GitHub lists under [Model hosting](#model-hosting-and-data-residency). |
| Azure | The agent stores nothing in Azure. The managed identity holds no data. |

## What is stored where

| Data | Where | Who can read it | Kept for | How to delete |
| --- | --- | --- | --- | --- |
| Answers, reports, investigations, proposed and created issues | Issues and comments in your repository | Anyone with read access | Until deleted | Delete comments, or delete the issue (needs admin). See [Deleting an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/administering-issues/deleting-an-issue). |
| Task record: prompt, transcript, Copilot logs, audit log, evidence, result | Actions artifact `finops-run-<run_id>-<attempt>` | Anyone with read access | `retention_days` in `finops/config.yaml` (default 30) | Delete the artifact from the run page. See [Removing workflow artifacts](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/remove-workflow-artifacts). |
| Post record: what was created, skipped, or refused | Actions artifact `finops-post-<run_id>-<attempt>` | Anyone with read access | Same as the task record | Same |
| Anomaly scan output | Actions artifact `finops-detect-<run_id>-<attempt>` | Anyone with read access | 30 days | Same |
| Eval runs (fixture data, no real spend) | Actions artifact `finops-evals-<run_id>-<attempt>` | Anyone with read access | 14 days | Same |
| Workflow logs and step summaries: the agent's final reply, which can include spend figures, and the titles of issues it created | Actions logs | Anyone with read access | The repository or organization retention setting: 90 days by default, 1 to 400 days for private repositories | Delete the workflow run, which also deletes its artifacts. See [Deleting a workflow run](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/delete-a-workflow-run). |
| Azure response cache | `.finops/cache/` on your machine, or in the runner's workspace | You, or nobody once the runner is discarded | Entries expire after 15 minutes but the files stay until deleted | `rm -rf .finops/cache` |
| Issue drafts and local eval results | `.finops/drafts/`, `.finops/evals/` | You | Until deleted | `rm -rf .finops` |
| Interactive Copilot CLI sessions | `~/.copilot/session-state/` and a local session database | You | Until deleted | Delete the session in Copilot CLI or the GitHub Copilot app |
| Synced copies of interactive sessions | Your GitHub account | Only you, unless you share a session | Until deleted | Delete it on GitHub.com. Deleting a local session in the GitHub Copilot app deletes the synced copy too; Copilot CLI asks. |
| VS Code chat history | VS Code's own storage | You | See VS Code's documentation | See VS Code's documentation |

**Session sync.** Copilot CLI and the GitHub Copilot app sync local sessions to your GitHub account by default, and those sessions include the spend figures the agent returned. To keep them local, turn off `remoteExport` in the [Copilot CLI config](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference). On Copilot Business and Enterprise, sessions only sync if the **Store local sessions in the Cloud** policy allows it. Headless runs pass `--no-remote-export`, so they never sync. See [About GitHub Copilot session data](https://docs.github.com/en/copilot/concepts/security-governance-and-network-settings/session-data).

**Deleting the whole agent.** Follow [Teardown](headless.md#teardown) for the Azure identity, role assignments, environment, and labels, then delete the repository, which removes its issues, logs, and artifacts. Ask everyone who used it interactively to delete `.finops/` and any sessions they want gone. GitHub's and Microsoft's own backup retention applies after deletion; this template can't shorten it, and their completion timelines are in their own policies.

## Model hosting and data residency

Everything in the prompt goes to the model: your question, the agent's instructions, and tool results with spend figures, resource names, and change history. Which company hosts the model, and on which cloud, depends on the model you pick. GitHub lists the hosting providers for each model family, their retention terms, and the exceptions (such as prompt caching and some preview models) in [Hosting of models for GitHub Copilot](https://docs.github.com/en/copilot/reference/ai-models/model-hosting). Check it before choosing a model.

To control the provider:

- **Headless runs.** Set `agent.model` in `finops/config.yaml`. Otherwise the run uses the Copilot CLI default.
- **Interactive sessions.** Each user picks their model. Organization admins can limit which models are available in Copilot policies.

This template has no region setting. Copilot requests aren't pinned to a region by anything here, and cost data is read through the global `management.azure.com` endpoint. If you need data residency, look at [GitHub Enterprise Cloud with data residency](https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/about-github-enterprise-cloud-with-data-residency) and check which Copilot features and models it supports.

## Model training

GitHub's [policy update](https://github.blog/news-insights/company-news/updates-to-github-copilot-interaction-data-usage-policy/) for interaction data (inputs, outputs, and their context), effective April 24, 2026:

| Copilot plan | Used to train models? |
| --- | --- |
| Business, Enterprise | No |
| Free, Pro, Pro+ | Yes, unless the user opts out under **Privacy** in their [Copilot settings](https://github.com/settings/copilot) |

The plan that applies depends on how the agent runs:

| How it runs | Whose plan applies |
| --- | --- |
| Interactive | The person running it |
| Headless, workflow `GITHUB_TOKEN`, personal repository | The repository owner's |
| Headless, workflow `GITHUB_TOKEN`, organization repository | The organization's. Confirm with an admin that it's Business or Enterprise. |
| Headless, `COPILOT_GITHUB_TOKEN` PAT | The PAT owner's |

If any of these is a Free, Pro, or Pro+ plan, opt out before sending spend data, or switch to an organization-billed setup.

The FinOps MCP server sends no telemetry, and the Azure MCP Server runs with `AZURE_MCP_COLLECT_TELEMETRY=false`.

## Sensitive data in context files

Files in `finops/context/` are sent to the model whenever the agent reads them, and anyone with repository access can read them. Don't put secrets, credentials, or personal data there. Use team names, not people, in `owners.yaml`.

The same applies to questions. Anything you type in an issue or a chat goes to the model and is stored in the places listed under [What is stored where](#what-is-stored-where).

The warning appears in three places: [`finops/context/README.md`](../finops/context/README.md), the agent profile (the agent repeats it when someone asks it to remember team facts), and this page. Context files are listed in CODEOWNERS, which takes effect once you require Code Owner review ([setup step 1](setup.md#1-create-your-repository)).
