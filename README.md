# EasyFlex Web – zpracování faktur v prohlížeči

EasyFlex Web je webová aplikace postavená na Flasku, která umožňuje nahrávat PDF faktury nebo tabulkové soubory, automaticky z nich vyčíst údaje pomocí OpenAI, data zkontrolovat/upravit a následně importovat do ABRA Flexi. Flask zde zajišťuje webový server, routování stránek, práci se sessions a napojení na uživatelskou autentizaci i databázi. Aplikace je navržena pro více uživatelů, každý má vlastní přihlašovací údaje, nastavení a historii zpracovaných dávek.

> V repozitáři je také původní desktopová (Tkinter) aplikace, podle které web vznikl. README se však soustředí pouze na webovou část.

---

## Hlavní schopnosti webové aplikace

- **Přihlášení a správa uživatelů** (admin účet + běžní uživatelé).
- **Nahrávání PDF nebo tabulek** (CSV/XLSX/XML) a tvorba dávek (batch).
- **Extrakce údajů z PDF** pomocí OpenAI Vision a následná kontrola v tabulce.
- **Ruční úpravy hodnot** přímo v UI před importem.
- **Import do ABRA Flexi** s podporou kontextu firmy, směru a typu dokladu.
- **Uložení a historie dávek** – možnost k dávkám vracet, filtrovat je a znovu importovat.
- **Oddělené přihlašovací údaje OpenAI/ABRA pro každého uživatele**.
- **Podpora SQLite i PostgreSQL** (přepíná se přes `DATABASE_URL`).

---

## Rychlý start (lokální vývoj)

### 1) Virtuální prostředí
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2) Instalace závislostí
```bash
pip install -r webapp/requirements.txt
```

### 3) Nastavení prostředí
Vytvořte soubor `.env` v kořeni projektu (nebo nastavte proměnné prostředí):

- `EASYFLEX_SECRET_KEY` – tajný klíč pro Flask session
- `EASYFLEX_ADMIN_PASSWORD` – heslo pro vytvoření admin účtu
- `DATABASE_URL` (volitelné) – PostgreSQL URL místo SQLite

### 4) Vytvoření admin účtu
```bash
EASYFLEX_ADMIN_PASSWORD=<heslo> python -m webapp.create_admin
```

### 5) Spuštění serveru
```bash
python -m webapp.app
```

Aplikace poběží na `http://127.0.0.1:5000/`. Přihlaste se jako `admin` a přejděte na **/settings**, kde nastavíte OpenAI a ABRA přístupové údaje.

---

## Uživatelské role a přihlášení

- **Admin** je vytvořen při inicializaci (skript `webapp.create_admin`).
- Admin může spravovat další uživatele a má kontrolu nad výchozími extrakčními parametry.
- Každý uživatel má vlastní přístupové údaje k OpenAI/ABRA a vlastní historii dávek.

---

## Workflow: od nahrání po import

1. **Nahrání souborů**
   - PDF soubory (jednotlivě nebo víc najednou) nebo tabulky (CSV/XLSX/XML).

2. **Extrakce / Import do dávky**
   - U PDF proběhne extrakce přes OpenAI.
   - U tabulek se data načtou do dávky a mapují se na interní strukturu.

3. **Kontrola v tabulce**
   - Každý řádek lze ručně editovat.
   - Vyberete, které řádky se mají importovat.

4. **Nastavení ABRA kontextu**
   - Firma, směr (vydaná/přijatá), typ dokladu.

5. **Import do ABRA**
   - Vybrané řádky se odešlou přes API.
   - Výsledek importu se zobrazí v UI a případné chyby se uloží.

---

## Konfigurace webové aplikace

### Databáze
- **Výchozí**: SQLite soubor `webapp/easyflex_web.db`.
- **PostgreSQL**: nastavte `DATABASE_URL` (např. z Renderu). Aplikace automaticky upraví prefix `postgres://` → `postgresql://`.

### Konfigurační klíče a přístupy
Většina citlivých údajů se nastavuje **po přihlášení** na stránce `/settings`:

- `OPENAI_API_KEY` – API klíč (per uživatel)
- `ABRA_SERVER`, `ABRA_PORT`, `ABRA_USERNAME`, `ABRA_PASSWORD`, `ABRA_COMPANY`
- Volby jako `ABRA_VERIFY_TLS`, kontext firmy, typ dokladu a další

Admin může nastavit výchozí extrakční parametry pro ostatní uživatele (model, DPI, limit stránek apod.).

---

## Práce s dávkami (batch)

- Každé nahrání nebo import z tabulky vytvoří **dávku**.
- Dávka obsahuje všechny řádky a metadata (název, typ zdroje, datum vytvoření).
- Dávky lze znovu otevřít, upravit a importovat opakovaně.

---

## Uložení dat, chyb a logů

- **Web DB**: `webapp/easyflex_web.db` (pokud nepoužíváte PostgreSQL).
- **Chybové JSONy z ABRA importu**: ukládají se do `errors/` nebo do uživatelského profilu (dle prostředí).
- **Cache extrakce**: v uživatelském profilu `.../EasyFlex/cache`.

---

## Ochrana dat a soukromí

- Při extrakci PDF se obrázky stránek odesílají do OpenAI.
- LLM mapování sloupců u tabulek je volitelné – pokud je zapnuto, odešlou se ukázková data.
- Přístupové údaje OpenAI/ABRA jsou ukládány v databázi (`user_settings`).

---

## Časté problémy a jejich řešení

**„Extrakce nefunguje / PDF tlačítka jsou šedá“**
- Zkontrolujte, zda má uživatel vyplněný `OPENAI_API_KEY` na stránce `/settings`.

**„Chyba převodu PDF“**
- Ujistěte se, že je dostupný Poppler (`pdftoppm`) a že je správně nastaven `POPPLER_PATH`.

**„ABRA server not reachable / 401/403/404“**
- Zkontrolujte server, port, přihlašovací údaje a TLS v `/settings`.

**„ABRA: company code is not configured“**
- Zkontrolujte kontext firmy a typu dokladu v horní liště před importem.

---

## Produkční nasazení (stručně)

- Produkční server lze spustit přes `gunicorn`:
```bash
gunicorn "webapp.app:app"
```
- Pro PostgreSQL nastavte `DATABASE_URL`.
- Doporučeno nastavit silný `EASYFLEX_SECRET_KEY` a zabezpečit přístup k `/settings`.

---

Vytvořil: Viktor Vítovec
