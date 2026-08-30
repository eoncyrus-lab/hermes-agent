#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

# Prefer the project venv, because that is what a developer has. CI installs
# the package into the runner's own interpreter and has no .venv at all --
# insisting on one meant this script could never pass in CI, which is where it
# matters most.
if [[ -x "$repo_dir/.venv/bin/python" ]]; then
  project_python="$repo_dir/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  project_python="$(command -v python3)"
else
  echo "ERROR: no Python found; create .venv or put python3 on PATH." >&2
  exit 1
fi

if ! "$project_python" -c 'import sonder' >/dev/null 2>&1; then
  echo "ERROR: $project_python cannot import sonder; run: pip install -e '.[dev]'" >&2
  exit 1
fi
echo "using $($project_python -c 'import sys; print(sys.executable)')"

"$project_python" -m ruff check .
"$project_python" -m ruff format --check .
"$project_python" -m pytest tests \
  --ignore=tests/test_integration.py \
  --ignore=tests/test_erasure.py \
  --ignore=tests/test_identity_boundary.py \
  --ignore=tests/test_pgvector_integration.py
"$project_python" evals/run.py

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is required for real PostgreSQL/pgvector tests." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 is required for real PostgreSQL/pgvector tests." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is installed but the daemon is unavailable." >&2
  exit 1
fi

# Generate per-run credentials. UUID hex is URL-safe, so the same values can
# be used in both PostgreSQL and SQLAlchemy DSNs without lossy quoting.
new_password() {
  "$project_python" -c 'import secrets; print(secrets.token_hex(24))'
}
export SONDER_TEST_POSTGRES_PASSWORD="$(new_password)"
export SONDER_TEST_APP_DB_PASSWORD="$(new_password)"
export SONDER_TEST_MAINT_DB_PASSWORD="$(new_password)"
export SONDER_TEST_IDENTITY_DB_PASSWORD="$(new_password)"
export SONDER_TEST_ERASURE_DB_PASSWORD="$(new_password)"
export SONDER_TEST_WORKER_DB_PASSWORD="$(new_password)"
export SONDER_TEST_BOT_DB_PASSWORD="$(new_password)"

compose=(docker compose -f docker-compose.test.yml -p sonder-check)
cleanup() {
  "${compose[@]}" down --volumes --remove-orphans
}
trap cleanup EXIT
"${compose[@]}" up --detach --wait

# One DSN per service role, because the point of the boundary tests is that
# these are separate database identities with separate reaches.
# The passwords are the throwaway container's (tests/sql/roles.sql); the
# migrations themselves create every role without one.
host="127.0.0.1:55432/sonder_test"
export SONDER_TEST_DATABASE_URL="postgresql+asyncpg://sonder_app:$SONDER_TEST_APP_DB_PASSWORD@$host"
export SONDER_MAINTENANCE_DATABASE_URL="postgresql+asyncpg://sonder_maint:$SONDER_TEST_MAINT_DB_PASSWORD@$host"
export SONDER_ERASURE_DATABASE_URL="postgresql+asyncpg://sonder_erase:$SONDER_TEST_ERASURE_DB_PASSWORD@$host"
export SONDER_WORKER_DATABASE_URL="postgresql+asyncpg://sonder_worker:$SONDER_TEST_WORKER_DB_PASSWORD@$host"
export SONDER_IDENTITY_DATABASE_URL="postgresql+asyncpg://sonder_identity:$SONDER_TEST_IDENTITY_DB_PASSWORD@$host"
export SONDER_BOT_DATABASE_URL="postgresql+asyncpg://sonder_bot:$SONDER_TEST_BOT_DB_PASSWORD@$host"
# The migration identity, for the tests that replay a migration to prove it
# is rerunnable in any order without handing a privilege back.
export SONDER_SUPERUSER_DATABASE_URL="postgresql+asyncpg://sonder:$SONDER_TEST_POSTGRES_PASSWORD@$host"
"$project_python" -m pytest \
  tests/test_integration.py \
  tests/test_erasure.py \
  tests/test_identity_boundary.py \
  tests/test_pgvector_integration.py
