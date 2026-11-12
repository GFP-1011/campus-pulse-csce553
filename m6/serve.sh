#!/usr/bin/env bash
# Start the sharded API locally: m6/serve.sh <active_shards> [workers] [port]
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export ACTIVE_SHARDS="${1:?shards}" M6_POOL_MODE=transaction M6_POOL_MAX="${M6_POOL_MAX:-32}"
exec .venv/bin/uvicorn app_sharded.main:app --host 127.0.0.1 --port "${3:-8600}" \
  --workers "${2:-6}" --log-level warning --no-access-log
