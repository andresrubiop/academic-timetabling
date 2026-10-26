#!/usr/bin/env bash
# Restore an Academic AI database dump.
#
#   ./deploy/restore.sh <dump-file> [target-database]
#
# **Restores into a NEW database by default**, never over the live one. A
# restore script whose default action overwrites production is a data-loss
# incident waiting for a tired operator: the common case is checking that a
# backup is good, not replacing everything.
#
# To replace the live database you must pass its name explicitly *and* set
# I_UNDERSTAND_THIS_DESTROYS_DATA=yes. There is no shorter path on purpose.
set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="${1:?usage: restore.sh <dump-file> [target-database]}"
[ -f "$DUMP" ] || { echo "restore: $DUMP does not exist" >&2; exit 1; }

[ -f .env ] || { echo "restore: .env not found; run from the repository root" >&2; exit 1; }
PGUSER_VAL="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)"
PGDB_VAL="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)"
PGPASSWORD_VAL="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)"

TARGET="${2:-${PGDB_VAL}_restore_$(date -u +%Y%m%d%H%M%S)}"

if [ "$TARGET" = "$PGDB_VAL" ] && [ "${I_UNDERSTAND_THIS_DESTROYS_DATA:-no}" != "yes" ]; then
    cat >&2 <<MSG
restore: refusing to restore over the live database "$PGDB_VAL".

Restore somewhere else and compare:
    ./deploy/restore.sh "$DUMP"

If you really mean to replace it, stop the application first so nothing writes
during the restore, then:
    I_UNDERSTAND_THIS_DESTROYS_DATA=yes ./deploy/restore.sh "$DUMP" "$PGDB_VAL"
MSG
    exit 1
fi

psql_run() {
    docker compose exec -T -e PGPASSWORD="$PGPASSWORD_VAL" db \
        psql -U "$PGUSER_VAL" -d postgres -v ON_ERROR_STOP=1 "$@"
}

echo "restore: creating database $TARGET"
psql_run -q -c "DROP DATABASE IF EXISTS \"$TARGET\";"
psql_run -q -c "CREATE DATABASE \"$TARGET\";"

echo "restore: loading $DUMP"
docker compose exec -T -e PGPASSWORD="$PGPASSWORD_VAL" db \
    pg_restore -U "$PGUSER_VAL" -d "$TARGET" --no-owner --exit-on-error < "$DUMP"

# Report what arrived. "pg_restore exited 0" is not the same claim as "the
# data is there", and the difference is the whole reason to test a restore.
TABLES="$(docker compose exec -T -e PGPASSWORD="$PGPASSWORD_VAL" db \
    psql -U "$PGUSER_VAL" -d "$TARGET" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"
REVISION="$(docker compose exec -T -e PGPASSWORD="$PGPASSWORD_VAL" db \
    psql -U "$PGUSER_VAL" -d "$TARGET" -tAc "SELECT version_num FROM alembic_version;" 2>/dev/null || echo "none")"

echo "restore: $TARGET has $TABLES tables, alembic revision ${REVISION:-none}"
echo "restore: inspect it with"
echo "  docker compose exec -e PGPASSWORD=... db psql -U $PGUSER_VAL -d $TARGET"
echo "restore: drop it when done with"
echo "  docker compose exec -e PGPASSWORD=... db psql -U $PGUSER_VAL -d postgres -c 'DROP DATABASE \"$TARGET\";'"
