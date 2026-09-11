#!/usr/bin/env bash
#
# Run one Sonder process against the hybrid stack:
#
#   postgres  - the `db` container from docker-compose.yml, published on
#               127.0.0.1:5432 by docker-compose.override.yml
#   redis     - the Homebrew service on 127.0.0.1:6379, which has no password
#   api/worker/bot - this script, on the host, from .venv
#
# The point of the split is that the three application images never have to be
# built. Compose still owns PostgreSQL, because the schema needs pgvector 0.8.6
# and six RLS roles that only migrations/001..006 create.
#
#   scripts/run-local.sh api       uvicorn on 127.0.0.1:8000
#   scripts/run-local.sh worker    arq worker (ingest, analysis, cron jobs)
#   scripts/run-local.sh bot       Telegram long polling
#   scripts/run-local.sh cli ...   the operator CLI, same environment
#
# Every DSN is assembled here rather than exported by hand, because the six
# roles are not interchangeable: the API must never hold the maintenance DSN,
# and the bot must hold `sonder_bot` rather than `sonder_identity`.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

# Read one value without sourcing .env: SONDER_LLM_ROUTES contains JSON, and
# bash would brace-expand it on the way in.
read_secret() {
  local name="$1" value
  value="$(grep -m1 "^${name}=" .env | cut -d= -f2- || true)"
  if [[ -z "$value" ]]; then
    echo "ERROR: ${name} is not set in .env" >&2
    exit 1
  fi
  printf '%s' "$value"
}

# Hex passwords are URL-safe, which is why the deployment generates them that
# way; a password containing `:/@?#[]` would have to be percent-encoded here.
pg="127.0.0.1:5432/sonder"
export SONDER_DATABASE_URL="postgresql+asyncpg://sonder_app:$(read_secret SONDER_APP_DB_PASSWORD)@${pg}"
export SONDER_IDENTITY_DATABASE_URL="postgresql+asyncpg://sonder_identity:$(read_secret SONDER_IDENTITY_DB_PASSWORD)@${pg}"
export SONDER_ERASURE_DATABASE_URL="postgresql+asyncpg://sonder_erase:$(read_secret SONDER_ERASURE_DB_PASSWORD)@${pg}"
export SONDER_WORKER_DATABASE_URL="postgresql+asyncpg://sonder_worker:$(read_secret SONDER_WORKER_DB_PASSWORD)@${pg}"
export SONDER_MAINTENANCE_DATABASE_URL="postgresql+asyncpg://sonder_maint:$(read_secret SONDER_MAINT_DB_PASSWORD)@${pg}"

# No password: this is the developer redis, not the compose one. Setting
# SONDER_REDIS_URL explicitly is what stops .env's SONDER_REDIS_PASSWORD from
# being used to build an authenticated URL that this server would reject.
export SONDER_REDIS_URL="redis://127.0.0.1:6379/0"

if [[ ! -x "$repo_dir/.venv/bin/python" ]]; then
  echo "ERROR: no .venv; run: python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

case "${1:-}" in
  api)
    exec "$repo_dir/.venv/bin/uvicorn" sonder.api.main:app --host 127.0.0.1 --port 8000
    ;;
  worker)
    exec "$repo_dir/.venv/bin/arq" sonder.worker.tasks.WorkerSettings
    ;;
  bot)
    exec "$repo_dir/.venv/bin/python" -m sonder.bot.telegram
    ;;
  wechat)
    # The channel authenticates its chats with `auth_wechat`, which only
    # `sonder_wechat` may execute -- so it takes that DSN rather than the
    # identity one the API and the Telegram bot use. Same variable, different
    # role: the process gets the narrowest credential that does its job.
    export SONDER_IDENTITY_DATABASE_URL="postgresql+asyncpg://sonder_wechat:$(read_secret SONDER_WECHAT_DB_PASSWORD)@${pg}"
    exec "$repo_dir/.venv/bin/python" -m sonder.wechat
    ;;
  cli)
    shift
    exec "$repo_dir/.venv/bin/python" -m sonder.cli "$@"
    ;;
  *)
    echo "usage: $0 {api|worker|bot|wechat|cli <args>}" >&2
    exit 2
    ;;
esac
