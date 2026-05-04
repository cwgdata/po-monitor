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
#
# Prereqs:
#   - databricks CLI v0.260+ (bundle support)
#   - node 18+, npm
#   - python 3.11+
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

if [[ -z "$WORKSPACE_HOST" || -z "$WAREHOUSE_ID" ]]; then
  echo "Usage: $0 <workspace_host> <warehouse_id> [target]" >&2
  echo "Example: $0 https://my-ws.cloud.databricks.com <warehouse-id> dev" >&2
  echo "Or set WORKSPACE_HOST + WAREHOUSE_ID in scripts/.deploy.env (gitignored)" >&2
  exit 1
fi

# Strip trailing slash for consistency
WORKSPACE_HOST="${WORKSPACE_HOST%/}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> 1/4  Build frontend"
(cd frontend && npm install --no-audit --no-fund && npm run build)

# Bundle reads workspace host from DATABRICKS_HOST (interpolation not allowed
# for workspace.host). Export it for all subsequent bundle commands.
export DATABRICKS_HOST="$WORKSPACE_HOST"

echo "==> 2/4  Validate bundle"
databricks bundle validate \
  --target "$TARGET" \
  --var "warehouse_id=$WAREHOUSE_ID"

echo "==> 3/4  Deploy bundle (sync source + create/update app)"
databricks bundle deploy \
  --target "$TARGET" \
  --var "warehouse_id=$WAREHOUSE_ID"

echo "==> 4/4  Run app (start the deployed app)"
databricks bundle run po_monitor \
  --target "$TARGET" \
  --var "warehouse_id=$WAREHOUSE_ID" || true

echo
echo "✓ deployed. Open the app from the Compute → Apps page in your workspace."
echo "  After it boots, hit /api/health and confirm warehouse_preflight.ok = true."
