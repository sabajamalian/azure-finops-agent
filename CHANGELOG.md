# Changelog

Notable changes to this template. Copies made from it don't receive updates automatically; compare against this file to see what's worth pulling in.

## Unreleased

### Added

- `finops init` writes `finops/config.yaml` from the subscriptions your `az login` can see, interactively or with `--all` / `--subscription`.
- `finops doctor` flags placeholder IDs left from the example config, an `az login` in the wrong tenant, and a missing `.venv` server for VS Code. A subscription you can't see at all now shows one line with a fix instead of four errors. A missing `npx` is a warning, since the Azure MCP Server is optional.
- Dev container (`.devcontainer/`) with Python, Node.js, the Azure CLI, the GitHub CLI, and Copilot CLI.
- `docs/headless.md`, split out of the setup guide, and a setup checklist. Windows and no-Node.js instructions.
- `mcp-server/README.md` and this changelog.
- A sample answer in the README.

### Changed

- `.vscode/mcp.json` starts the FinOps server from `${workspaceFolder}/.venv`, so VS Code finds it without an activated shell.
- The quickstart installs the latest Copilot CLI and starts it with `--allow-tool azure-finops`.

## 0.1.0

- First version: the `azure-finops` agent profile, the read-only FinOps MCP server and `finops` CLI, headless runs in GitHub Actions (issue questions, schedules, anomaly detection), `setup.sh` with Bicep for the managed identity, and evals against synthetic data.
