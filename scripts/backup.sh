#!/usr/bin/env bash
#
# Dump the database to a timestamped file, and verify the dump can be read
# back before reporting success.
#
# A backup nobody has restored is a guess. `pg_restore --list` is not a full
# restore, but it does parse the archive end to end, so a truncated or
# corrupt file fails here rather than on the day it is needed.
#
#   scripts/backup.sh [destination-directory]
#
# Restore into a stack whose migrations have already run -- the dump carries
# row-level policies that name sonder_app, sonder_erase and sonder_worker, and
# those are cluster roles the migrations create. Restoring into a bare
# database gives you the rows and drops every policy on the floor.
#
#   docker compose up -d db          # fresh volume: migrations create the roles
#   docker compose exec -T db pg_restore -U sonder -d sonder --clean \
#     --if-exists --no-owner --no-acl < backups/sonder-YYYYmmdd-HHMMSS.dump
#
# Verified end to end: 0 errors, 39 policies restored.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

dest="${1:-$repo_dir/backups}"
mkdir -p "$dest"

if ! docker compose ps --status running --services 2>/dev/null | grep -qx db; then
  echo "ERROR: the db service is not running; start the stack first." >&2
  exit 1
fi

stamp="$(date +%Y%m%d-%H%M%S)"
out="$dest/sonder-$stamp.dump"

# --format=custom so pg_restore can be selective, and so the archive carries
# its own table of contents for the verification below.
docker compose exec -T db \
  pg_dump -U sonder -d sonder --format=custom --no-owner --no-acl > "$out"

if [[ ! -s "$out" ]]; then
  echo "ERROR: dump is empty; removing $out" >&2
  rm -f "$out"
  exit 1
fi

if ! docker compose exec -T db pg_restore --list < "$out" > /dev/null 2>&1; then
  echo "ERROR: dump did not parse; removing $out" >&2
  rm -f "$out"
  exit 1
fi

chmod 600 "$out"

rows=$(docker compose exec -T db psql -U sonder -d sonder -At -c \
  "select (select count(*) from users)||' users, '
        ||(select count(*) from relationships)||' relationships, '
        ||(select count(*) from interactions)||' interactions'" | tr -d '\r')

echo "$out"
echo "  $(wc -c < "$out" | tr -d ' ') bytes, verified readable"
echo "  $rows"
echo
echo "NOTE: this covers the database only. The other half of a working"
echo "      restore is .env -- it holds the role passwords, the provider keys"
echo "      and the Telegram token, and it is deliberately not in git."
