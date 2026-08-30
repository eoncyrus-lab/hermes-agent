#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

project_python="$repo_dir/.venv/bin/python"
if [[ ! -x "$project_python" ]]; then
  echo "ERROR: project Python not found at $project_python; create .venv first." >&2
  exit 1
fi

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
export SONDER_TEST_DATABASE_URL="postgresql+asyncpg://sonder_app:sonder_app@$host"
export SONDER_MAINTENANCE_DATABASE_URL="postgresql+asyncpg://sonder_maint:sonder_maint@$host"
export SONDER_ERASURE_DATABASE_URL="postgresql+asyncpg://sonder_erase:sonder_erase@$host"
export SONDER_WORKER_DATABASE_URL="postgresql+asyncpg://sonder_worker:sonder_worker@$host"
export SONDER_IDENTITY_DATABASE_URL="postgresql+asyncpg://sonder_identity:sonder_identity@$host"
# The migration identity, for the tests that replay a migration to prove it
# is rerunnable in any order without handing a privilege back.
export SONDER_SUPERUSER_DATABASE_URL="postgresql+asyncpg://sonder:sonder@$host"
"$project_python" -m pytest \
  tests/test_integration.py \
  tests/test_erasure.py \
  tests/test_identity_boundary.py \
  tests/test_pgvector_integration.py
