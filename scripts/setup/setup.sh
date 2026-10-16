#!/usr/bin/env bash
# One-time setup for the Azure FinOps agent's headless runs (GitHub Actions).
#
# Azure: creates a user-assigned managed identity with a federated credential for this repo's `finops`
#        environment, and assigns Reader + Cost Management Reader on every subscription in finops/config.yaml.
# GitHub: creates the `finops` environment (default branch only) and sets AZURE_CLIENT_ID / AZURE_TENANT_ID.
#        Turns on FINOPS_ENABLED (unless --no-enable). Shipped automations start disabled, so that alone only
#        enables questions from issues and the free anomaly scan.
#
# Needs: az (signed in to the tenant in config, with rights to create role assignments on each subscription),
#        gh (signed in, admin on the repo), and `finops` (pip install ./mcp-server).
# Safe to re-run. Use --dry-run first to see every command that would change something.

set -euo pipefail

ENV_NAME="finops" # the workflows reference this name
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

REPO=""
RG="rg-finops-agent"
LOCATION="eastus"
ID_SUB=""
ID_NAME="id-finops-agent"
CLIENT_ID=""
SKIP_AZURE=0
SKIP_GITHUB=0
ENABLE=1
DRY_RUN=0

usage() {
  cat <<EOF
Usage: scripts/setup/setup.sh [options]

  --repo OWNER/NAME           GitHub repository (default: the current repo, from gh)
  --resource-group NAME       Resource group for the identity (default: $RG)
  --location LOCATION         Azure region for the identity and deployments (default: $LOCATION)
  --identity-subscription ID  Subscription that holds the identity (default: first one in config)
  --identity-name NAME        Managed identity name (default: $ID_NAME)
  --skip-azure                Don't create the identity or roles; needs --client-id
  --client-id ID              Client id of an existing identity (with --skip-azure)
  --skip-github               Don't touch GitHub settings
  --no-enable                 Leave the FINOPS_ENABLED repository variable alone
  --dry-run                   Print the commands that change things instead of running them
  -h, --help                  Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --resource-group) RG="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --identity-subscription) ID_SUB="$2"; shift 2 ;;
    --identity-name) ID_NAME="$2"; shift 2 ;;
    --client-id) CLIENT_ID="$2"; shift 2 ;;
    --skip-azure) SKIP_AZURE=1; shift ;;
    --skip-github) SKIP_GITHUB=1; shift ;;
    --no-enable) ENABLE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Runs a command that changes state, or prints it with --dry-run.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is not installed ($2)"; }

cd "$ROOT"
need finops "pip install ./mcp-server"
[ -f finops/config.yaml ] || die "finops/config.yaml not found; copy finops/config.example.yaml and fill it in"
TENANT="$(finops config tenant)"
SUBS="$(finops config subscriptions)"
[ -n "$SUBS" ] || die "no subscriptions in finops/config.yaml"
[ -n "$ID_SUB" ] || ID_SUB="$(printf '%s\n' "$SUBS" | head -n 1)"

if [ "$SKIP_GITHUB" = 0 ] || [ -z "$REPO" ]; then
  need gh "https://cli.github.com"
fi
if [ -z "$REPO" ]; then
  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)" || die "can't detect the repo; pass --repo OWNER/NAME"
fi

say "Repository $REPO, tenant $TENANT"
printf '%s\n' "$SUBS" | sed 's/^/    subscription /'
[ "$DRY_RUN" = 1 ] && echo "    (dry run: commands that change things are printed, not run)"

if [ "$SKIP_GITHUB" = 0 ]; then
  VISIBILITY="$(gh repo view "$REPO" --json visibility -q .visibility)"
  if [ "$VISIBILITY" = "PUBLIC" ]; then
    echo "warning: $REPO is public. The post job refuses to publish spend data to a public repo unless"
    echo "         allow_public_repo_output is true in finops/config.yaml. Use a private or internal repo."
  fi
fi

# -- Azure -----------------------------------------------------------------------------------------

if [ "$SKIP_AZURE" = 1 ]; then
  [ -n "$CLIENT_ID" ] || die "--skip-azure needs --client-id"
else
  need az "https://aka.ms/azcli"
  CURRENT_TENANT="$(az account show --query tenantId -o tsv 2>/dev/null)" || die "run: az login --tenant $TENANT"
  [ "$CURRENT_TENANT" = "$TENANT" ] || die "az is signed in to tenant $CURRENT_TENANT; run: az login --tenant $TENANT"

  say "Managed identity $ID_NAME in $RG ($ID_SUB)"
  run az group create --subscription "$ID_SUB" --name "$RG" --location "$LOCATION" \
    --tags purpose=finops-agent --output none
  run az deployment group create --subscription "$ID_SUB" --resource-group "$RG" --name finops-identity \
    --template-file "$SCRIPT_DIR/identity.bicep" \
    --parameters repository="$REPO" environmentName="$ENV_NAME" identityName="$ID_NAME" --output none

  if [ "$DRY_RUN" = 1 ]; then
    CLIENT_ID="<client-id>"
    PRINCIPAL_ID="<principal-id>"
  else
    IDS="$(az identity show --subscription "$ID_SUB" --resource-group "$RG" --name "$ID_NAME" \
      --query "[clientId, principalId]" -o tsv)"
    CLIENT_ID="$(printf '%s\n' "$IDS" | sed -n 1p)"
    PRINCIPAL_ID="$(printf '%s\n' "$IDS" | sed -n 2p)"
    [ -n "$CLIENT_ID" ] && [ -n "$PRINCIPAL_ID" ] || die "couldn't read the identity's ids"
    echo "    client id $CLIENT_ID, principal id $PRINCIPAL_ID"
  fi

  say "Read-only roles (Reader, Cost Management Reader)"
  printf '%s\n' "$SUBS" | while read -r sub; do
    [ -n "$sub" ] || continue
    run az deployment sub create --subscription "$sub" --location "$LOCATION" --name finops-agent-roles \
      --template-file "$SCRIPT_DIR/roles.bicep" --parameters principalId="$PRINCIPAL_ID" --output none
  done
fi

# -- GitHub ----------------------------------------------------------------------------------------

if [ "$SKIP_GITHUB" = 0 ]; then
  DEFAULT_BRANCH="$(gh repo view "$REPO" --json defaultBranchRef -q .defaultBranchRef.name)"

  say "GitHub environment '$ENV_NAME' (deployments from $DEFAULT_BRANCH only)"
  run gh api --method PUT "repos/$REPO/environments/$ENV_NAME" --silent \
    -F "deployment_branch_policy[protected_branches]=false" \
    -F "deployment_branch_policy[custom_branch_policies]=true"
  EXISTING="$(gh api "repos/$REPO/environments/$ENV_NAME/deployment-branch-policies" \
    -q '.branch_policies[] | select(.type == "branch") | .name' 2>/dev/null || true)"
  if printf '%s\n' "$EXISTING" | grep -qxF "$DEFAULT_BRANCH"; then
    echo "    branch policy for $DEFAULT_BRANCH already exists"
  else
    run gh api --method POST "repos/$REPO/environments/$ENV_NAME/deployment-branch-policies" --silent \
      -f name="$DEFAULT_BRANCH" -f type=branch
  fi

  say "Environment variables"
  run gh variable set AZURE_CLIENT_ID --repo "$REPO" --env "$ENV_NAME" --body "$CLIENT_ID"
  run gh variable set AZURE_TENANT_ID --repo "$REPO" --env "$ENV_NAME" --body "$TENANT"

  # The issue form only applies labels that already exist. The post job creates its own labels.
  say "Label finops-ask"
  run gh label create finops-ask --repo "$REPO" --force --color 1d76db \
    --description "Question for the Azure FinOps agent"

  if [ "$ENABLE" = 1 ]; then
    say "Turning on headless runs (FINOPS_ENABLED)"
    run gh variable set FINOPS_ENABLED --repo "$REPO" --body true
  fi

  say "Copilot authentication for headless runs"
  if gh secret list --repo "$REPO" --env "$ENV_NAME" --json name -q '.[].name' 2>/dev/null \
    | grep -qx COPILOT_GITHUB_TOKEN; then
    echo "    using the COPILOT_GITHUB_TOKEN secret (a PAT); delete it to use the workflow token instead"
  elif [ "$(gh repo view "$REPO" --json isInOrganization -q .isInOrganization)" = "true" ]; then
    cat <<EOF
    using the workflow's GITHUB_TOKEN, billed to the organization. An organization owner must allow it:
    Copilot policies > Copilot CLI > "Allow use of Copilot CLI billed to the organization" (on by default
    when Copilot CLI is enabled). If you can't use that policy, store a fine-grained PAT with the
    "Copilot Requests" permission instead (you'll be prompted for the value):
      gh secret set COPILOT_GITHUB_TOKEN --repo $REPO --env $ENV_NAME
EOF
  else
    echo "    using the workflow's GITHUB_TOKEN, billed to the repository owner's Copilot seat"
  fi
fi

say "Next steps"
STEP=1
if [ "$SKIP_GITHUB" = 0 ] && [ "$ENABLE" = 0 ]; then
  cat <<EOF
  $STEP. Turn on the workflows when you're ready:
       gh variable set FINOPS_ENABLED --repo $REPO --body true
EOF
  STEP=$((STEP + 1))
fi
cat <<EOF
  $STEP. On EA or MCA billing, make sure subscription readers can see charges (docs/setup.md).
  $((STEP + 1)). Commit finops/config.yaml, then check access: run the finops-run workflow with kind=manual and a
     question like "What did each subscription cost last month?"
EOF
