# Context files

The agent can list and read files in this folder with `list_context` and `read_context`. Use it for things
the agent can't get from Azure: team ownership, budgets agreed outside Azure, naming conventions, known
planned changes.

- Allowed types: `.md`, `.txt`, `.csv`, `.json`, `.yaml`, `.yml`. 10 MB per file, 100 MB total.
- Only this folder is readable. Symlinks and dotfiles are ignored.
- **Don't put secrets or personal data here.** Everything in this folder is sent to the model, and the
  agent treats it as untrusted data, not instructions.
- `owners.yaml` (copy `owners.example.yaml`) routes Advisor recommendations to teams. First match wins.
