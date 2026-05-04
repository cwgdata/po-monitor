#!/usr/bin/env bash
# One-shot deploy of PO Monitor to a target Databricks workspace.
#
# Usage:
#   ./scripts/deploy.sh <workspace_host> <warehouse_id> [target]
# Example:
#   ./scripts/deploy.sh https://my-ws.cloud.databricks.com <warehouse-id> dev
#
# Prereqs:
#   - databricks CLI v0.260+ (bundle support)
#   - node 18+, npm
#   - python 3.11+
#   - You're authenticated against the target workspace (databricks auth login)

set -euo pipefail

WORKSPACE_HOST="${1:-}"
WAREHOUSE_ID="${2:-}"
TARGET="${3:-dev}"

if [[ -z "$WORKSPACE_HOST" || -z "$WAREHOUSE_ID" ]]; then
  echo "Usage: $0 <workspace_host> <warehouse_id> [target]" >&2
  echo "Example: $0 https://my-ws.cloud.databricks.com <warehouse-id> dev" >&2
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
