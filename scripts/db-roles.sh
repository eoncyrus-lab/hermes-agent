#!/usr/bin/env bash
#
# Set the login password for each service role from the environment.
#
# The migrations create these roles with no password at all, which under md5
# or scram means they cannot authenticate. That is deliberate: a password
# written into a migration is a published credential in every deployment that
# ever runs the file, and "we'll change it in production" is not a control.
#
# Rerunnable, so this is also the rotation path: set the new value in the
# environment and run it again.
set -euo pipefail

require() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: $name is not set." >&2
    exit 1
  fi
}

for var in \
  SONDER_APP_DB_PASSWORD \
  SONDER_IDENTITY_DB_PASSWORD \
  SONDER_ERASURE_DB_PASSWORD \
  SONDER_WORKER_DB_PASSWORD \
  SONDER_MAINT_DB_PASSWORD \
  SONDER_BOT_DB_PASSWORD
do
  require "$var"
done

set_password() {
  local role="$1" password="$2"
  local command_file
  command_file="$(mktemp "${TMPDIR:-/tmp}/sonder-db-role.XXXXXX")"
  chmod 600 "$command_file"
  trap 'rm -f "$command_file"' RETURN
  # \prompt reads the secret from stdin. The command file contains only the
  # fixed role and psql variable reference, so neither argv nor disk contains
  # the password. :'pw' still provides psql's SQL-literal escaping.
  #
  # The role name is a fixed literal from the list below, never input.
  printf '%s\n' '\prompt pw' "ALTER ROLE $role LOGIN PASSWORD :'pw';" >"$command_file"
  printf '%s\n' "$password" | \
    psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 --file="$command_file"
  rm -f "$command_file"
  trap - RETURN
  echo "set password for $role"
}

# Wait for the migrations mounted into docker-entrypoint-initdb.d to have
# created the roles. On a fresh volume the healthcheck can pass before the
# last init script has run.
for _ in $(seq 1 60); do
  if psql --no-psqlrc --quiet --tuples-only --set=ON_ERROR_STOP=1 \
      -c "SELECT 1 FROM pg_roles WHERE rolname = 'sonder_bot'" | grep -q 1; then
    break
  fi
  sleep 1
done

set_password sonder_app      "$SONDER_APP_DB_PASSWORD"
set_password sonder_identity "$SONDER_IDENTITY_DB_PASSWORD"
set_password sonder_erase    "$SONDER_ERASURE_DB_PASSWORD"
set_password sonder_worker   "$SONDER_WORKER_DB_PASSWORD"
set_password sonder_maint    "$SONDER_MAINT_DB_PASSWORD"
set_password sonder_bot      "$SONDER_BOT_DB_PASSWORD"
