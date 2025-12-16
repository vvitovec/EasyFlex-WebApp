#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "[check] Hledám zakázané pravidlo if-not-found → create..."
if rg -g'!scripts/hardening_check.sh' -n "if-not-found\\s*=\\s*create" .; then
	echo "✖ Nalezen zakázaný if-not-found → create" >&2
	exit 1
fi

echo "[check] Hledám zápisy na /adresar (POST/PUT/PATCH)..."
if rg -g'!tests/*' -g'!scripts/hardening_check.sh' -n "POST.*/adresar|PUT.*/adresar|PATCH.*/adresar" .; then
	echo "✖ Detekován POST/PUT/PATCH na /adresar" >&2
	exit 1
fi

echo "[info] Výskyty '/adresar' pro kontrolu (měly by být jen GET):"
rg -n "/adresar" . || true

echo "[info] Mapování odběratele ve faktura-vydana (kontrolní výpis):"
rg -n "odberatel|nazFirmy|firma" EasyFlex/abra.py webapp/app.py || true

echo "✔ Hardening greps OK"
