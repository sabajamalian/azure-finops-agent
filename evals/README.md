# Evals

Evals run the agent end to end (Copilot CLI, the agent profile, the FinOps MCP server) against a synthetic Azure tenant and grade what it submits. They don't touch real Azure: the server runs with `FINOPS_FIXTURE_MODE=synthetic`, and any allow-listed MCP servers (like Azure MCP) are left out and denied for the run.

Each case and repeat is one Copilot session, so evals use premium requests. Run them when you change the agent profile, the server's tools, or the model, not on every commit. Unit tests in `mcp-server/tests/` cover the graders and the pipeline (with a scripted stand-in for the CLI) at no cost.

## Running

Locally, with the Copilot CLI installed and signed in:

```bash
pip install -e ./mcp-server
finops evals list
finops evals run --case prod-spend-increase          # one case
finops evals run --repeat 3 --model <model-id>       # every case, three times each
```

Results go to `.finops/evals/` (gitignored): `results.json`, `summary.md`, and one directory per case and run with the same artifacts a real run produces (`prompt.md`, `result.json`, `evidence.jsonl`, `audit.jsonl`, `transcript.md`, `tool-results.jsonl`).

In GitHub Actions, run the **finops-evals** workflow. Copilot CLI authenticates with the job's `GITHUB_TOKEN` (or the optional `COPILOT_GITHUB_TOKEN` secret in the `finops` environment; see [docs/setup.md](../docs/setup.md#copilot-authentication)), and there's no Azure login. Results are uploaded as an artifact; the data is synthetic, so the artifact holds no real spend.

## The synthetic tenant

`mcp-server/src/azure_finops/simulator/synthetic.py` answers the Cost Management, Resource Graph, Activity Log, and Advisor calls the server makes. Today is fixed at 2026-03-18. The scenario:

- **contoso-prod**: a steady web, data, and shared platform. Six days ago alice created `vm-gpu-train-01` (NC6s v3) in `rg-ml`, adding about 1,180 USD a day. Five days ago bob added a diagnostic setting, and Log Analytics ingestion in `rg-shared` went from about 38 to about 160 USD a day.
- **contoso-dev**: small and stable, with weekend shutdown.
- **Advisor**: four cost recommendations worth 1,635 USD a month.

`FINOPS_SYNTHETIC_DENY` simulates missing permissions, for example `cost:<subscription-id>` returns 403 from Cost Management for that subscription (`arg:` and `activity:` work the same way for Resource Graph and the Activity Log).

## Cases and graders

Cases live in `cases.yaml`. Each has a question, optional extra `env`, and `expect`:

| Check | Passes when |
|---|---|
| `status` | `result.json` status is in the list (default `[ok]`) |
| `cites_evidence` | the result cites evidence ids, all of them exist in `evidence.jsonl`, and the summary references them (default on) |
| `numbers_match` | every currency amount in the summary (`$1,180`, `1,180 USD`, `$35.4k`) matches a number in a tool result, within its displayed rounding or 0.5% (default on) |
| `mentions_all` | for each group, the summary contains at least one phrase (case-insensitive) |
| `mentions_none` | the summary contains none of the phrases |
| `tools_any` / `tools_none` | at least one / none of the tools was called (from `audit.jsonl`) |
| `max_data_tool_calls` | calls to data tools (everything except capabilities, scopes, context, automations, and submit) stay under the cap |
| `max_actions` | the result proposes at most this many issues |

`numbers_match` is deliberately strict: the agent's rules say every number must come from a tool result, so a projection like "about 35k a month" that the agent computed itself fails the check unless a tool returned it. If that's too strict for a case, set `numbers_match: false` there.

## Adding a case

1. Write the question as a user would ask it.
2. Check what the synthetic data says (run the case once and read `result.json` and `tool-results.jsonl`).
3. Add expectations about facts that should always be in a correct answer, not about wording. Prefer groups of alternatives (`[log analytics, loganalytics, rg-shared]`).
4. Run it with `--repeat 3`. A case that passes two out of three times is telling you something about the agent or the case.
