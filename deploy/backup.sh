#!/usr/bin/env bash
# Back up the Academic AI database.
#
#   ./deploy/backup.sh [destination-directory]
#
# Three decisions worth stating, because each is the reason a backup scheme
# fails rather than a preference:
#
# **It never writes to `/`.** Docker's data-root lives there, and a backup that
# fills the root filesystem takes down every project on this shared host —
# including the one it was protecting. The default destination is on
# `/mnt/sda1`, which is a different disk.
#
# **It reads the dump back before calling it a backup.** `pg_dump` exiting 0
# having written a truncated file is the ordinary way this goes wrong, and it
# is invisible until the day somebody needs it. `pg_restore --list` parses the
# archive's table of contents, so a corrupt or empty dump fails here rather
# than during an incident.
#
# **It does not delete anything unless asked.** Retention is opt-in via
# BACKUP_KEEP. A cron job that prunes by default deletes the good backups too
# when the new ones have silently been failing.
set -euo pipefail

cd "$(dirname "$0")/.."

DEST="${1:-${BACKUP_DIR:-/mnt/sda1/academic-ai-backups}}"
KEEP="${BACKUP_KEEP:-0}"          # 0 = keep everything
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# Read the credentials from .env rather than taking them on the command line:
# an argument is visible in `ps` to every user on a shared host.
[ -f .env ] || { echo "backup: .env not found; run from the repository root" >&2; exit 1; }
PGUSER_VAL="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)"
PGDB_VAL="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)"
PGPASSWORD_VAL="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)"

mkdir -p "$DEST"
OUT="$DEST/academic_ai_${STAMP}.dump"

# The database publishes no port (it is on an `internal: true` network), so the
# dump is produced inside the container and streamed to stdout. Nothing is
# written inside the container, which runs with a read-only root filesystem.
docker compose exec -T -e PGPASSWORD="$PGPASSWORD_VAL" db \
    pg_dump -U "$PGUSER_VAL" -d "$PGDB_VAL" --format=custom --compress=6 \
    > "$OUT"

chmod 600 "$OUT"

# A dump nobody has read is a file, not a backup.
#
# The archive is read back through a throwaway container mounting the backup
# directory read-only. Two earlier attempts were wrong in ways worth recording:
# this host has no `pg_restore` (the database only exists in a container), and
# piping the file to `pg_restore --list /dev/stdin` fails with "did not find
# magic string in file header" because a custom-format archive has to be
# seekable and a pipe is not. Both failures look identical to a corrupt dump,
# which is precisely the wrong thing for a verification step to be unable to
# distinguish.
if ! docker run --rm -v "$(cd "$(dirname "$OUT")" && pwd)":/backups:ro \
        postgres:16-alpine pg_restore --list "/backups/$(basename "$OUT")" \
        > /dev/null 2>&1; then
    echo "backup: $OUT did not parse as a pg_dump archive; NOT usable" >&2
    exit 1
fi

SIZE="$(du -h "$OUT" | cut -f1)"
echo "backup: $OUT ($SIZE), verified readable"

if [ "$KEEP" -gt 0 ]; then
    # Deliberately narrow: only files this script's own naming produces.
    mapfile -t OLD < <(ls -1t "$DEST"/academic_ai_*.dump 2>/dev/null | tail -n +$((KEEP + 1)))
    for f in "${OLD[@]:-}"; do
        [ -n "$f" ] || continue
        echo "backup: pruning $f"
        rm -f -- "$f"
    done
fi
