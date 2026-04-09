#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
	echo "Chybí $ROOT_DIR/.env"
	exit 1
fi

cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
	echo "Chybí docker v PATH."
	exit 1
fi

if ! git diff --quiet --ignore-submodules -- .; then
	if [[ "${EASYFLEX_ALLOW_DIRTY_UPDATE:-0}" != "1" ]]; then
		echo "Repo obsahuje lokální změny. Update byl zastaven, aby nedošlo ke konfliktu."
		echo "Pokud to opravdu chcete obejít, spusťte EASYFLEX_ALLOW_DIRTY_UPDATE=1 bash scripts/selfhost/update_stack.sh"
		exit 1
	fi
fi

git pull --ff-only
docker compose config -q
docker compose up -d --build

echo "EasyFlex je aktualizovaný."
