# Security

The agent reads cost and inventory data and writes nothing to Azure. In GitHub it can only create issues and comments, and only through a separate job that never holds Azure credentials or talks to the model. This page covers what each part can reach, who can start a run, how untrusted text is contained, and what's still open.

- [What each part can reach](#what-each-part-can-reach)
- [Azure access](#azure-access)
- [GitHub permissions](#github-permissions)
- [Running Copilot headless](#running-copilot-headless)
- [Who can start a run](#who-can-start-a-run)
- [Prompt injection](#prompt-injection)
- [Supply chain and change control](#supply-chain-and-change-control)
- [Known limitations](#known-limitations)
- [Open questions](#open-questions)

## What each part can reach

| Part | Azure | GitHub | Model |
| --- | --- | --- | --- |
| Interactive session | Your `az login`, through the read-only client | Nothing. Proposed issues are saved as drafts for you to create with `gh`. | Your Copilot seat |
| `finops-run` run job | Managed identity: Reader and Cost Management Reader | Read only | Copilot CLI, with tools limited to read-only MCP tools |
| `finops-run` post job | None | `issues: write` | None |
| `finops-ask` approve job | None | `issues: write` | None |
| `finops-anomaly` detect job | Managed identity | Read only | None |
| Dispatchers | None | `actions: write` to start `finops-run` | None |

The split is the main control. The job that runs the model can't write to GitHub, and the jobs that write can't reach Azure or the model. A post job only publishes a `result.json` that passes validation (see [prompt injection](#prompt-injection)).

## Azure access

**Identity.** `setup.sh` creates a user-assigned managed identity with one federated credential, subject `repo:<owner>/<repo>:environment:finops`. The `finops` environment only accepts deployments from the default branch, so a workflow on another branch, a pull request, or a fork can't get an Azure token. There's no client secret to leak or rotate.

**Roles.** [`roles.bicep`](../scripts/setup/roles.bicep) assigns Reader and Cost Management Reader on each subscription in `finops/config.yaml`, and nothing else. To narrow access, remove subscriptions from the config or remove role assignments; `get_capabilities` reports what's missing and the agent carries on with the rest. You can narrow the identity's access but nothing in the repo widens it.

**Read-only client.** Every Azure call from the FinOps MCP server goes through an explicit allow-list in [`arm.py`](../mcp-server/src/azure_finops/arm.py):

| Method | Paths |
| --- | --- |
| `GET` | `/subscriptions`, `/subscriptions/{id}` |
| `POST` | Cost Management `query` and `forecast`, at subscription or resource group scope |
| `POST` | `/providers/Microsoft.ResourceGraph/resources` |
| `GET` | Activity Log `eventtypes/management/values` |
| `GET` | Advisor `recommendations` |
| `GET` | `Microsoft.Authorization/permissions` |

Any other method or path raises `ReadOnlyViolation` before a request is sent. So do hosts other than Azure Resource Manager and subscriptions that aren't in the config. Tests confirm that every allow-list entry is exercised and that a list of blocked methods, paths, and lookalike hosts never reaches the network. This applies in interactive sessions too, so a user with Contributor still can't write through the agent.

**Azure MCP Server.** It runs with `--read-only` at a pinned version (`@azure/mcp@3.0.0`), with telemetry off (`AZURE_MCP_COLLECT_TELEMETRY=false`). Only `advisor_recommendation_list`, `advisor_recommendation_summary`, and `pricing_get` are allowed, and `advisor_recommendation_update` is denied by name in case a future version exposes it.

## GitHub permissions

Every workflow starts with `permissions: {}` and grants each job only what it needs. Checkouts use `persist-credentials: false`.

| Workflow | Job | Permissions |
| --- | --- | --- |
| `finops-run` | run | `contents: read`, `issues: read`, `pull-requests: read`, `id-token: write`, `copilot-requests: write` |
| `finops-run` | post | `contents: read`, `issues: write` |
| `finops-ask` | ask | `actions: write` |
| `finops-ask` | approve | `contents: read`, `issues: write` |
| `finops-dispatch` | dispatch | `contents: read`, `actions: write` |
| `finops-anomaly` | detect | `contents: read`, `id-token: write` |
| `finops-anomaly` | dispatch | `contents: read`, `issues: read`, `actions: write` |
| `finops-evals` | evals | `contents: read`, `copilot-requests: write` (no Azure login; fixtures only) |
| `ci` | all | `contents: read` |

The post job also refuses to:

- Publish to a public repository, unless `allow_public_repo_output: true`.
- Publish schedule or anomaly runs whose task didn't come from the default branch.

## Running Copilot headless

`finops task run-agent` starts Copilot CLI with these controls:

| Control | Flags or mechanism |
| --- | --- |
| Agent | `--agent azure-finops`, `-p <prompt>`, `--silent`, `--no-ask-user` |
| Tools | `--allow-tool azure-finops` plus each allow-listed third-party tool. `--deny-tool` for `shell`, `write`, and `url`, for each denied tool, and for every server marked `headless: false`. In fixture mode, third-party servers are denied entirely. |
| MCP servers | `--disable-builtin-mcps`, and `--additional-mcp-config` built from the allowlist |
| Files | `--disallow-temp-dir`; a fresh `COPILOT_HOME` in a temp directory, so no user config, memory, or other sessions are visible |
| Updates | `--no-auto-update`, with the CLI version pinned in `COPILOT_CLI_VERSION` |
| Session sync | `--no-remote-export`, so headless sessions don't land in the token owner's synced history |
| Secrets | `--secret-env-vars COPILOT_GITHUB_TOKEN,GITHUB_TOKEN,GH_TOKEN,ACTIONS_ID_TOKEN_REQUEST_TOKEN`, so the CLI redacts them from its output |
| Environment | Copilot gets an allow-list of variables, and the only token in it is `COPILOT_GITHUB_TOKEN`. The MCP servers get the `AZURE_*` and `FINOPS_*` variables they need. |
| Azure CLI state | `AZURE_CONFIG_DIR` points to a temp directory that's deleted at the end of the job |
| Records | `--share transcript.md` and `--log-dir copilot-logs`, uploaded with the task record |

The audit log masks any field named `token`, `access_token`, `refresh_token`, `id_token`, `client_secret`, or `secret`.

**Copilot credentials.** With no `COPILOT_GITHUB_TOKEN` secret, Copilot uses the job's own `GITHUB_TOKEN` through `copilot-requests: write`. That token is short-lived, but it means anyone who can start `finops-run` spends Copilot requests billed to the repository owner or organization. With a PAT, the cost lands on the PAT owner, and the PAT is a long-lived credential to rotate. See [Copilot authentication](setup.md#copilot-authentication).

## Who can start a run

| Trigger | Who | Notes |
| --- | --- | --- |
| Interactive | Anyone with the repo and their own Azure access | Runs as them, with their roles |
| Issue labeled `finops-ask` | Issue author must be `OWNER`, `MEMBER`, or `COLLABORATOR` | Anyone can open an issue with the form, but no run starts unless the author is trusted |
| `/finops <question>` comment | Comment author must be trusted, on an issue labeled `finops-ask` | Works on any `finops-ask` issue, including one opened by an untrusted user. In that case the original issue body is left out of the prompt. |
| `/finops approve` | Comment author must be trusted | Proposals are read only from comments by `github-actions[bot]`, and every selected proposal is revalidated before any issue is created |
| Manual `finops-run` | Anyone with write access (needed to run workflows) | Can run any automation, even a disabled one |
| Schedule and anomaly | Merged automation files on the default branch | Gated by Code Owner review; each run records the file's last commit, the PR that merged it, and who approved that PR |

Runs are capped by job timeouts (45 minutes for the agent), one concurrent run per dedupe key, and `max_investigations_per_day` for anomalies. There's no cap on how many questions collaborators can ask, so anyone you add as a collaborator can spend Copilot requests.

## Prompt injection

Untrusted text reaches the model from issue bodies and comments, anomaly dimension values (resource group and service names), resource names and tags, Activity Log caller fields, Advisor text, and context files. Assume the model can be talked into anything; the controls limit what that achieves.

**Limit who and what gets in.**

- Only trusted authors start runs. In follow-ups, only comments by trusted authors and the agent's own earlier answers are included, and an untrusted original issue body is replaced with a note.
- Issue text and anomaly titles are wrapped in `<<<UNTRUSTED-xxxxxxxxxxxx>>>` markers with a random nonce per run, so the text can't close the block early. The agent profile says to treat that text, resource names, tags, and context files as data.
- Hidden `<!-- finops:... -->` markers are stripped from input, so a question can't forge dedupe or proposal markers.
- Context files are behind Code Owner review, and `read_context` prefixes each file with an "untrusted reference material" header.

**Limit what the agent can do.**

- The only tools are the read-only FinOps tools and three read-only Azure MCP tools. No shell, file writes, or URL fetches.
- The agent's only output is `submit_result`. It can't post anything itself.

**Validate the output in a separate job.** The post job (and the approve job for proposals):

- Checks `result.json` against the schema.
- Rejects evidence ids that no tool returned in that run.
- Only allows destinations listed in `finops/config.yaml` and allowed for that task.
- Caps proposed issues at `max_issues_per_run`.
- Rejects the workflow labels (`finops-ask`, `finops-failure`, `finops-report`, `finops-anomaly`), so a proposed issue can't trigger an ask run or impersonate a report.
- Prefixes fingerprints of proposed issues with `act-`, so they can't overwrite or comment on report and anomaly issues.
- Sanitizes Markdown: removes images and HTML media (which could beacon data to an outside URL), neutralizes `@mentions`, strips spoofed markers, and caps length.

**What a hijacked agent could still do.**

- Write a misleading answer or report, or propose misleading issues, in an allowed destination. Ask runs only propose; a person has to `/finops approve`. Automation and manual runs create up to `max_issues_per_run` issues directly.
- Read anything the identity can read and put it in its answer, which lands in your private repository.
- Put data in the filter it sends to `pricing_get`, which queries Microsoft's public Azure Retail Prices API. If that matters, set `headless: false` on `azure-mcp` in `finops/allowlist.yaml`.

## Supply chain and change control

- Third-party actions are pinned to full commit SHAs, with the version in a comment.
- Copilot CLI is pinned with `COPILOT_CLI_VERSION` and runs with `--no-auto-update`.
- `@azure/mcp` is pinned to an exact version, but `npx` resolves it from npm at run time without an integrity hash.
- Python dependencies in `mcp-server/pyproject.toml` use version ranges, not a lock file. Pin them, or install from a hashed requirements file, if your policy needs reproducible builds.
- [`CODEOWNERS`](../.github/CODEOWNERS) covers the config, allowlist, automations, context files, agent profile, workflows, `.vscode/mcp.json`, and the MCP server. It only takes effect with **Require review from Code Owners** on the default branch (see [setup](setup.md#1-create-your-repository)).
- CI runs ruff, pytest, `finops schemas export --check`, `finops check-agent-profile` (fails if the profile or `.vscode/mcp.json` enables a server or tool that isn't in the allowlist), `finops automations validate`, and actionlint.

## Known limitations

- Dedupe and the failure-issue lookup trust any issue labeled `finops` that carries the right marker, without checking who created it. Adding labels needs triage access, so this is limited to people you've already trusted.
- Dispatch dedupe matches on the last word of a run's title, case-sensitively.
- `finops doctor` loads the config from the current directory rather than its `root` argument, and reads fixture mode from the process environment.
- The Cost Management rate limiter busy-waits in tests.
- `query_dataset` rejects `;` inside string literals and recursive CTEs.
- When the preferred Cost Management API version isn't available, the fallback request spends extra QPU.
- When `get_change_history` merges the same change seen in the Activity Log and Resource Graph, a third change to the same resource within 10 minutes can be folded into that pair.
- The "Resource Graph unavailable" note has no `Fix:` line.
- `correlate_changes` gives a change at 23:59 UTC the day before an increase more weight than one at 00:01 UTC on the day itself.
- `next_fire_times` looks ahead at most 400 days.

## Open questions

These haven't been tested against live services:

- Whether `--agent azure-finops` finds the profile when Copilot CLI runs in Actions with an isolated `COPILOT_HOME`.
- Whether Copilot CLI authenticates end to end with the workflow `GITHUB_TOKEN` and `copilot-requests: write`.
- Whether the `labeled` event fires when the issue form applies `finops-ask` at creation.
- Whether Advisor's `savingsAmount` is always monthly. The tool ranks by `annualSavingsAmount` and passes `savingsAmount` through as reported.
