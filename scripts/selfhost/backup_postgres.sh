#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
	echo "Chybí $ROOT_DIR/.env"
	exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
	echo "Chybí docker v PATH."
	exit 1
fi

set -a
source "$ROOT_DIR/.env"
set +a

if [[ -z "${POSTGRES_USER:-}" || -z "${POSTGRES_DB:-}" ]]; then
	echo "V .env chybí POSTGRES_USER nebo POSTGRES_DB."
	exit 1
fi

mkdir -p "$ROOT_DIR/backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date +%F_%H-%M-%S)"
BACKUP_FILE="$ROOT_DIR/backups/easyflex_${TIMESTAMP}.sql.gz"
TMP_FILE="$(mktemp "$ROOT_DIR/backups/easyflex_${TIMESTAMP}.XXXXXX.sql")"
cleanup_tmp() {
	rm -f "$TMP_FILE" "${TMP_FILE}.gz"
}
trap cleanup_tmp EXIT

cd "$ROOT_DIR"
if [[ -z "$(docker compose ps -q db)" ]]; then
	echo "Služba db neběží. Spusťte nejdřív docker compose up -d."
	exit 1
fi

docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "$TMP_FILE"
gzip -f "$TMP_FILE"
gzip -t "${TMP_FILE}.gz"
mv "${TMP_FILE}.gz" "$BACKUP_FILE"

if [[ ! -s "$BACKUP_FILE" ]]; then
	echo "Záloha se nevytvořila správně: $BACKUP_FILE"
	exit 1
fi

find "$ROOT_DIR/backups" -type f -name 'easyflex_*.sql.gz' -mtime +"$RETENTION_DAYS" -delete

echo "Záloha hotová: $BACKUP_FILE"
