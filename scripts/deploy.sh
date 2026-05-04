#!/usr/bin/env bash
# One-shot deploy of PO Monitor to a target Databricks workspace.
#
# Usage:
#   ./scripts/deploy.sh <workspace_host> <warehouse_id> [target]
# Example:
#   ./scripts/deploy.sh https://my-ws.cloud.databricks.com <warehouse-id> dev
#
# Or, for repeated deploys to the same workspace, drop the args and put values
# in scripts/.deploy.env (gitignored — see scripts/deploy.env.example):
#   WORKSPACE_HOST=https://my-ws.cloud.databricks.com
#   WAREHOUSE_ID=abcd1234ef
#   TARGET=dev   # optional
#   PO_MONITOR_CATALOG=...   # injected into deployed app's env at deploy time
#
# Flow:
#   1. Build frontend (vite + tsc)
#   2. Sync source to /Workspace/Users/<me>/po-monitor (excluding gitignored stuff)
#   3. Render a deploy-time app.yaml with extra env vars from .deploy.env and
#      overwrite the workspace copy. Local app.yaml is never modified, so your
#      workspace-specific values stay out of git.
#   4. Trigger `databricks apps deploy` against the synced source path
#
# Prereqs:
#   - databricks CLI v0.260+
#   - node 18+, npm
#   - python 3.11+ (used to render app.yaml; PyYAML pulled from .venv if present)
#   - You're authenticated against the target workspace (databricks auth login)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ENV="$REPO_ROOT/scripts/.deploy.env"

# If positional args are missing, try sourcing the gitignored local config.
if [[ -z "${1:-}" || -z "${2:-}" ]]; then
  if [[ -f "$DEPLOY_ENV" ]]; then
    # shellcheck disable=SC1090
    set -a; . "$DEPLOY_ENV"; set +a
  fi
fi

WORKSPACE_HOST="${1:-${WORKSPACE_HOST:-}}"
WAREHOUSE_ID="${2:-${WAREHOUSE_ID:-}}"
TARGET="${3:-${TARGET:-dev}}"
APP_NAME="${APP_NAME:-po-monitor}"

if [[ -z "$WORKSPACE_HOST" || -z "$WAREHOUSE_ID" ]]; then
  echo "Usage: $0 <workspace_host> <warehouse_id> [target]" >&2
  echo "Example: $0 https://my-ws.cloud.databricks.com <warehouse-id> dev" >&2
  echo "Or set WORKSPACE_HOST + WAREHOUSE_ID in scripts/.deploy.env (gitignored)" >&2
  exit 1
fi

# Strip trailing slash for consistency.
WORKSPACE_HOST="${WORKSPACE_HOST%/}"
export DATABRICKS_HOST="$WORKSPACE_HOST"

cd "$REPO_ROOT"

PY="${PYTHON:-python3}"

echo "==> 1/4  Build frontend"
(cd frontend && npm install --no-audit --no-fund && npm run build)

# Resolve workspace user once for the source-code-path. Use stdlib json only.
ME_EMAIL="$(databricks current-user me 2>/dev/null | "$PY" -c 'import sys,json; print(json.load(sys.stdin).get("userName",""))')"
if [[ -z "$ME_EMAIL" ]]; then
  echo "Failed to resolve current user. Is the CLI authenticated?" >&2
  exit 1
fi
WORKSPACE_PATH="/Workspace/Users/$ME_EMAIL/$APP_NAME"

echo "==> 2/4  Sync source to $WORKSPACE_PATH"
databricks sync . "$WORKSPACE_PATH" --full

# Inject any PO_MONITOR_* env vars that .deploy.env defined into the deployed
# app.yaml. We render to a temp file and overwrite the workspace copy — the
# local app.yaml is untouched so workspace-specific config never enters git.
echo "==> 3/4  Inject env overrides into workspace app.yaml"
INJECT_VARS=()
for v in PO_MONITOR_CATALOG PO_MONITOR_SCHEMA PO_MONITOR_MAINTAINER_EMAIL; do
  if [[ -n "${!v:-}" ]]; then
    INJECT_VARS+=("$v=${!v}")
  fi
done

if (( ${#INJECT_VARS[@]} > 0 )); then
  if ! grep -q '__DEPLOY_INJECT__' app.yaml; then
    echo "app.yaml is missing the __DEPLOY_INJECT__ marker; cannot inject env." >&2
    exit 1
  fi
  RENDERED="$(mktemp -t app.yaml.XXXXXX)"
  trap 'rm -f "$RENDERED"' EXIT

  # Pass kv pairs to awk via "kv=K=V" args; awk emits the env block lines
  # before the __DEPLOY_INJECT__ marker. Single-quoting matches the committed
  # app.yaml's style.
  awk -v kvs="$(IFS='|'; echo "${INJECT_VARS[*]}")" '
    BEGIN { n = split(kvs, arr, "|") }
    /__DEPLOY_INJECT__/ {
      for (i = 1; i <= n; i++) {
        eq = index(arr[i], "=")
        if (eq == 0) continue
        k = substr(arr[i], 1, eq - 1)
        v = substr(arr[i], eq + 1)
        printf "  - name: %c%s%c\n", 39, k, 39
        printf "    value: %c%s%c\n", 39, v, 39
      }
      print
      next
    }
    { print }
  ' app.yaml > "$RENDERED"

  databricks workspace import "$WORKSPACE_PATH/app.yaml" \
    --file "$RENDERED" --format AUTO --overwrite
  echo "    injected: ${INJECT_VARS[*]%%=*}"
else
  echo "    no PO_MONITOR_* overrides set; using committed app.yaml as-is"
fi

echo "==> 4/4  Deploy app from $WORKSPACE_PATH"
databricks apps deploy "$APP_NAME" --source-code-path "$WORKSPACE_PATH"

echo
echo "✓ deployed. Confirm with:"
echo "  databricks apps get $APP_NAME"
echo "  open the URL it prints; /api/health should return warehouse_preflight.ok = true"
