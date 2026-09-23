#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"
mkdir -p backups
chmod 700 backups
exec 9>backups/.backup.lock
flock -n 9 || { echo "EasyFlex backup is already running."; exit 0; }

[[ -f .env ]] || { echo "Missing EasyFlex .env"; exit 1; }
set -a
source .env
set +a
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ && "$RETENTION_DAYS" -ge 1 ]] || { echo "Invalid retention"; exit 1; }

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGING="$(mktemp -d "$ROOT_DIR/backups/.pending.XXXXXX")"
trap 'rm -rf -- "$STAGING"' EXIT

docker compose exec -T db pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "$STAGING/database.dump"
docker compose exec -T db pg_restore --list < "$STAGING/database.dump" > /dev/null
# Fail instead of publishing a partial snapshot if an upload changes during tar.
docker compose exec -T web tar -czf - -C /app/instance . > "$STAGING/instance.tar.gz"
gzip -t "$STAGING/instance.tar.gz"
cp .env "$STAGING/environment.env"
git rev-parse HEAD > "$STAGING/revision.txt"
(
 cd "$STAGING"
 sha256sum database.dump instance.tar.gz environment.env revision.txt > SHA256SUMS
)
DESTINATION="$ROOT_DIR/backups/easyflex_snapshot_$TIMESTAMP"
[[ ! -e "$DESTINATION" ]] || { echo "Backup destination already exists"; exit 1; }
mv -- "$STAGING" "$DESTINATION"
trap - EXIT

# Only expire snapshots created by this script after publishing a valid new one.
find "$ROOT_DIR/backups" -mindepth 1 -maxdepth 1 -type d -name 'easyflex_snapshot_*' -mtime +"$RETENTION_DAYS" -exec rm -rf -- {} +
echo "EasyFlex backup complete: $DESTINATION"
