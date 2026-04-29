# EasyFlex Web

Webová aplikace pro zpracování faktur v prohlížeči. Uživatel nahraje PDF nebo tabulku, zkontroluje vyčtená data a zařadí import do ABRA Flexi.

## Funkce

- přihlášení, admin účet a správa uživatelů,
- nahrávání PDF a CSV/XLSX/XML souborů,
- extrakce fakturačních údajů z PDF přes OpenAI,
- ruční kontrola a úprava řádků před importem,
- fronta pro PDF extrakci a ABRA import,
- historie dávek se stránkováním, filtrováním a opakovaným importem,
- per-user nastavení pro OpenAI, ABRA a kontext firmy,
- SQLite pro lokální vývoj, PostgreSQL pro produkci.

## Struktura

```text
EasyFlex/          Sdílené doménové jádro: extrakce, CSV/XML/XLSX parsing, ABRA mapping.
webapp/            Flask web, databázové modely, autentizace, templates, worker.
scripts/           Provozní a self-hosting pomocné skripty.
tests/             Pytest testy pro doménovou logiku a webovou vrstvu.
assets/            Statické assety používané aplikací.
```

## Lokální vývoj

Požadavky: Python 3.11 nebo 3.12, Poppler (`pdftoppm`) pro PDF extrakci.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r webapp/requirements.txt
```

Vytvořte `.env` nebo nastavte proměnné prostředí:

```bash
EASYFLEX_SECRET_KEY=change-me
EASYFLEX_ADMIN_PASSWORD=change-me
# DATABASE_URL=postgresql://user:password@host:5432/dbname
```

Inicializace admin účtu:

```bash
python -m webapp.create_admin
```

Spuštění webu:

```bash
python -m webapp.app
```

Spuštění workeru v druhém terminálu:

```bash
python -m webapp.worker
```

Aplikace běží na `http://127.0.0.1:5000/`. Po přihlášení nastavte OpenAI a ABRA přístupy na `/settings`.

## Konfigurace

Základní proměnné:

- `EASYFLEX_ENV=production` v produkci vynutí bezpečnější start,
- `EASYFLEX_SECRET_KEY` je povinný v produkci,
- `EASYFLEX_ADMIN_PASSWORD` vytvoří nebo aktualizuje admin účet,
- `DATABASE_URL` přepne aplikaci ze SQLite na PostgreSQL,
- `MAX_CONTENT_LENGTH`, `MAX_PDF_FILES_PER_BATCH`, `MAX_PDF_FILE_BYTES`, `MAX_BATCH_TOTAL_BYTES`, `MAX_BATCH_TOTAL_PAGES` a `MAX_TABLE_FILE_BYTES` řídí upload limity.

Citlivé OpenAI a ABRA přístupy se běžně nastavují v aplikaci na `/settings` a ukládají se do databáze k danému uživateli.

## Testy a kontrola

```bash
python -m pytest -q
pip install ruff
python -m ruff check . --exclude .venv
```

## Produkce

Produkční běh vyžaduje web proces i worker proces. `Procfile`, `Dockerfile` a `docker-compose.yml` už obsahují obě části.

```bash
gunicorn "webapp.app:app" --timeout 180 --graceful-timeout 30 --workers 2
python -m webapp.worker
```

Pro produkci používejte PostgreSQL přes `DATABASE_URL`. SQLite ponechte pro lokální vývoj nebo velmi malý provoz.

Detailní self-hosting postup je v `SELFHOSTING.md`.
