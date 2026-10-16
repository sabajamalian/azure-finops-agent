# Automations

One YAML file per automation. The file name must match `name`. Files starting with `_` are ignored.

- Schema: [`../schemas/automation.schema.json`](../schemas/automation.schema.json)
- Ask the agent to draft one: "Every Monday at 9 AM, summarize last week's spend by service and post it to finops-reports."
  It calls `draft_automation`, shows you the YAML and the next three run times, and writes the file only after you approve.
- Merging the pull request that adds or changes a file is what authorizes it. CODEOWNERS requires a review.
- `finops automations list` shows each automation's schedule, next runs, and last triggered time.
- Delete the file (or set `enabled: false`) to stop it. Past issues and run artifacts stay until you delete them.

Two trigger types:

- `schedule`: cron plus IANA timezone, with optional `business_day: first|last`. The dispatcher runs hourly,
  so expect up to about an hour of delay.
- `anomaly`: runs when the daily detector finds an anomaly matching `filter`. Each anomaly is investigated once,
  and at most `anomaly.max_investigations_per_day` investigations start per day.

The examples here ship with `enabled: false`. Enable them after setup.
