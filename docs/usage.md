# Usage

Ask the agent in Copilot CLI or VS Code for interactive analysis. Use GitHub issues to ask questions without a local setup. Use automations for recurring reports and anomaly investigations. Every path is read-only against Azure, and every number in an answer cites an evidence id you can trace back to the query that produced it.

- [Interactive](#interactive)
- [Ask from a GitHub issue](#ask-from-a-github-issue)
- [Automations](#automations)
- [Anomaly detection](#anomaly-detection)
- [Manual runs](#manual-runs)
- [Reading an answer](#reading-an-answer)
- [CLI reference](#cli-reference)
- [Evals](#evals)
- [Cost of running it](#cost-of-running-it)

## Interactive

```bash
copilot --agent azure-finops
```

Or pick **azure-finops** in VS Code Chat. Setup is in [setup](setup.md#5-use-it-interactively). Example questions:

| Task | Ask |
| --- | --- |
| Spend overview | "What did each subscription cost last month, by service?" |
| Period comparison | "Compare this month to date with the same days last month for contoso-prod." |
| Cost drivers | "Why did spend in contoso-prod go up last week? Break it down by resource group and resource." |
| Anomalies | "Any unusual daily spikes in the last 30 days?" |
| Changes | "What changed in rg-data around March 3, and could it explain the cost jump?" |
| Savings | "What are our top Advisor cost recommendations, and how much do they save?" |
| Prices | "What's the retail price of a D4s v5 in East US?" |
| Amortized view | "Show reservation costs amortized for the last quarter." |
| Context | "Who owns rg-data?" (reads `finops/context/`) |

Answers lead with the result, then tables, then an **Evidence** section listing each evidence id with its scope, period, cost basis, and currency. Ask "show evidence ev-004" to see the exact query behind a number.

The agent stays in scope. It declines to change resources, buy reservations or savings plans, or edit budgets, and explains who should act instead. It also won't create GitHub issues itself. If you ask, it shows the proposed issue, saves an approved draft under `.finops/drafts/`, and gives you the `gh issue create` command to run.

### Drafting an automation

Describe what you want in plain words: "Every Monday at 9, post last week's spend by subscription to finops-reports." The agent calls `draft_automation`, which validates the definition and returns the YAML and the next three run times without writing anything. It restates the schedule for you to confirm. After you confirm, it writes `finops/automations/<name>.yaml`. The automation turns on only when you open a pull request and an owner merges it.

## Ask from a GitHub issue

Requires [headless setup](headless.md).

1. Open an issue with the **Ask the FinOps agent** form, or add the `finops-ask` label to an issue you wrote.
2. The agent answers in a comment, usually within a few minutes. The workflow run is linked in the comment footer.
3. Follow up by commenting `/finops <question>`. The agent sees its earlier answers plus the original question and earlier comments written by collaborators, trimmed to the most recent when the thread is long. Text from anyone else is left out.
4. If the answer proposes new issues (for example, one per cleanup item), it lists them as numbered proposals. Comment `/finops approve` to create all of them, or `/finops approve 1,3` for some. Approving more than `max_issues_per_run` at once fails. If a matching issue from the last year already exists, open or closed, the approval comments on it instead of creating a duplicate.

Only owners, members, and collaborators can trigger the agent. Issues and comments from anyone else are ignored, and labeling someone else's issue doesn't trigger a run either. The question text is passed to the agent as untrusted data, so instructions inside it can't change the agent's tools, scope, or destinations.

## Automations

Automations are reviewed YAML files in `finops/automations/`. There are two trigger types:

- **schedule**: a cron in a time zone, or the first or last business day of the month. `finops-dispatch` runs hourly, looks back over a window in case Actions delayed a run, and starts one `finops-run` per due slot. Each slot runs once.
- **anomaly**: a filter (minimum impact, minimum percent, subscriptions, services, resource groups) that decides which detected anomalies get an investigation.

Each run posts its report as an issue to the automation's destinations. The shipped examples are disabled; see [headless runs](headless.md#3-turn-on-automations) to turn them on. The file format, examples, and limits are in [finops/automations/README.md](../finops/automations/README.md).

```bash
finops automations list        # name, trigger, next runs, last run (needs GITHUB_TOKEN for "last run")
finops automations validate    # what CI runs on every pull request
```

To preview what the dispatcher would start, run **finops-dispatch** manually. Manual runs default to `dry_run: true`, which lists due slots without starting anything.

## Anomaly detection

`finops-anomaly` runs daily at 14:23 UTC in two steps:

1. **Detect.** A deterministic detector, with no model involved, queries daily cost for each subscription, grouped by the dimensions in `anomaly.group_by` (resource group and service by default). It compares each day against a robust baseline, skips the most recent incomplete days, and flags increases that pass every threshold in `config.yaml` (`min_impact`, `min_percent`, `z_threshold`). This step uses no premium requests.
2. **Investigate.** Each new anomaly that matches an enabled anomaly automation starts one `finops-run`, up to `max_investigations_per_day`. Anomalies that already have an issue or a run are skipped. The agent confirms the spike, finds the drivers, correlates resource changes from the Activity Log, checks Advisor, and posts an issue labeled `finops-anomaly`.

Run it manually with `dry_run: true` (the default) to see what it would flag and investigate. The algorithm is described in [architecture](architecture.md#anomaly-detector).

## Manual runs

From **Actions > finops-run > Run workflow**, or:

```bash
gh workflow run finops-run.yml -f kind=manual -f question="Top 10 resources by cost last month?"
gh workflow run finops-run.yml -f kind=manual -f automation=weekly-spend-summary
```

A question posts its answer as a new issue labeled `finops-report`. An automation name runs that schedule automation now, even if it's disabled, which is how you test one before enabling it.

## Reading an answer

Every issue or comment the agent posts ends with a footer naming the agent, the run, and the trigger. To check a number:

1. Find its evidence id, like `(ev-004)`, in the Evidence section.
2. Open the linked run and download the `finops-run-*` artifact.
3. `evidence.jsonl` has the exact query, scope, period, cost basis, currency, and data-through date for each id. `audit.jsonl` lists every tool call and Azure request.

Treat the figures as you would a Cost Management export: accurate for the query, subject to the usual data delay, and worth checking before you act on them.

## CLI reference

`finops` is installed with the MCP server (`pip install ./mcp-server`).

| Command | Purpose |
| --- | --- |
| `finops init [--all \| --subscription NAME_OR_ID ...] [--tenant ID] [--timezone TZ] [--force]` | Write `finops/config.yaml` from the subscriptions your `az login` can see. Prompts for a choice when run in a terminal. |
| `finops doctor [--skip-azure] [--skip-github]` | Check config, automations, agent profile, schemas, required commands, the Copilot token, Azure access, and GitHub settings. |
| `finops automations list\|validate [--json]` | List automations with next run times, or validate them. |
| `finops detect --output anomalies.json` | Run the anomaly detector locally with your `az login`. |
| `finops config [all\|tenant\|subscriptions]` | Print the validated config. |
| `finops check-agent-profile` | Check the agent profile and `.vscode/mcp.json` against `finops/allowlist.yaml`. |
| `finops schemas export [--check]` | Regenerate the JSON schemas in `finops/schemas/`, or check they're current. |
| `finops evals list\|run` | Run the agent against synthetic data. See [Evals](#evals). |
| `finops task`, `post`, `approve`, `dispatch`, `anomaly-dispatch` | Workflow steps. You don't normally run these yourself. |

The MCP server itself is `azure-finops-mcp`, a stdio server that Copilot starts for you.

## Evals

`evals/cases.yaml` holds scripted questions with expected tool calls, evidence, and answer checks. They run against a synthetic Azure tenant served from fixtures, so no Azure access is needed and no real spend leaves your machine:

```bash
finops evals list
finops evals run --case prod-spend-increase
```

Or run the **finops-evals** workflow. Each case uses premium requests. Details are in [evals/README.md](../evals/README.md).

## Cost of running it

The Azure side is free: Cost Management, Resource Graph, Advisor, and Activity Log reads have no charge, and the identity costs nothing. Copilot is the cost. Each interactive prompt, issue question, scheduled report, and anomaly investigation uses premium requests from the seat or organization that authenticates the run (see [Copilot authentication](headless.md#copilot-authentication)). Use `max_investigations_per_day` and the automation schedules to keep that predictable.
