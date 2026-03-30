#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
	echo "Chybí $ROOT_DIR/.env"
	exit 1
fi

set -a
source "$ROOT_DIR/.env"
set +a

mkdir -p "$ROOT_DIR/backups"
BACKUP_FILE="$ROOT_DIR/backups/easyflex_$(date +%F_%H-%M-%S).sql"

cd "$ROOT_DIR"
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "$BACKUP_FILE"

echo "Záloha hotová: $BACKUP_FILE"
