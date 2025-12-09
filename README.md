## EasyFlex – rychlé zpracování faktur (PDF/CSV) a import do ABRA Flexi

Jednoduchá desktopová aplikace pro Windows, která:

- **extrahuje údaje z PDF faktur** pomocí AI (OpenAI Vision),
- **načte faktury z CSV**,
- umožní **ruční úpravu polí** v přehledné tabulce,
- a **naimportuje vybrané faktury do ABRA Flexi**.

### Co aplikace dělá a nedělá
- **Dělá**: pracuje se souhrnnými částkami DPH (0 %, 12 %, 21 %) a základními údaji faktury (dodavatel, odběratel, datumy, čísla, částky). U CSV podporuje i základní mapování sloupců.
- **Nedělá**: neřeší položky faktury po řádcích ani žádné dopočty či dorovnání – hodnoty pouze bezpečně „opsává“ tak, jak jsou uvedené na faktuře.

---

## Požadavky

- Windows 10/11 (64‑bit)
- Připojení k internetu pro volání OpenAI a ABRA Flexi
- Pokud spouštíte ze zdrojového kódu: **Python 3.10+**

Poznámka: Aplikace umí automaticky najít přibalený Poppler (pro převod PDF→obrázky). Pokud Poppler přibalený není, lze jej doinstalovat a cestu nastavit proměnnou `POPPLER_PATH` nebo v `settings.ini` (viz níže).

---

## Instalace a spuštění

### Varianta A) Předpřipravené balíčky
- Windows: rozbalte `EasyFlex-Windows.zip` a spusťte `EasyFlex.exe`.
- macOS: rozbalte `EasyFlex-macOS.tar.gz`, přetáhněte `EasyFlex.app` do `/Applications` a při prvním spuštění potvrďte, že aplikaci důvěřujete.
- Nastavení a chyby se ukládají do uživatelského profilu (`%APPDATA%\EasyFlex` na Windows, `~/Library/Application Support/EasyFlex` na macOS).

### Varianta B) Ze zdrojového kódu
1) Nainstalujte Python 3.10+ a otevřete PowerShell ve složce projektu.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r EasyFlex/requirements.txt
python -m EasyFlex.main
```

Volitelně lze vytvořit EXE:

```powershell
pip install pyinstaller
pyinstaller EasyFlex.spec
```

---

## Distribuce

- Hotové balíčky pro Windows (`EasyFlex-Windows.zip`) i macOS (`EasyFlex-macOS.tar.gz`)
  lze vyrobit skripty v adresáři `build/`. Přesný postup viz
  [`DISTRIBUTION.md`](./DISTRIBUTION.md).
- Každý balíček už obsahuje všechny pythoní závislosti i Poppler – koncoví
  uživatelé pouze rozbalí archiv a spustí `EasyFlex.exe`, resp. `EasyFlex.app`.

---

## První nastavení

1) Otevřete aplikaci a klikněte na tlačítko `Nastavení`.
2) Zkontrolujte tři záložky:
   - `Abra`
     - Server (např. `abra.example.com`), Port (je‑li potřeba), Uživatel, Heslo
     - Volba „Ověřovat TLS certifikát“ je doporučená.
   - `Extractor`
     - Zadejte **OpenAI API Key**. Bez něj je extrakce z PDF vypnuta.
     - Ostatní hodnoty (model, concurrency, DPI…) jsou předvyplněné a můžete je ponechat.
   - `Extrakce`
     - Volitelné chování (např. „Použít číslo dokladu jako variabilní symbol“).
     - Pokud chcete, můžete povolit „Auto‑import do ABRA“ – import se po extrakci spustí sám.
     - U CSV lze povolit „LLM mapování sloupců“ (odešle ukázková data do OpenAI).

Klikněte na `Uložit`.

---

## Běžné použití

1) Vyberte zdroj dat
   - `Vybrat PDF`: jedno PDF s fakturou
   - `Vybrat složku`: hromadné zpracování všech PDF ve složce
  - `Vybrat tabulku`: import faktur z tabulkového souboru (CSV, XLSX, XML)

2) Práce v tabulce
   - Dvojklik na buňku → ruční úprava hodnoty.
   - První sloupec „Import“: kliknutím přepínáte, zda se řádek bude importovat (✓/✗).
   - Pravé tlačítko myši na řádku umožní označit/odznačit všechny.

3) Kontext pro ABRA (horní lišta)
   - `Firma`: vyberte firmu (lze přidat novou volbou „Nová firma…“).
   - `Směr`: `faktura-vydana` nebo `faktura-prijata`.
   - `Typ dokladu`: vyberte nebo přidejte nový typ (uloží se pro danou firmu a směr).

4) Import do ABRA Flexi
   - Manuálně: tlačítko `Importovat do ABRA`.
   - Automaticky: povolte v `Nastavení` → `Extrakce` → „Auto‑import do ABRA“.

5) Export do CSV
   - Tlačítko `Exportovat CSV` uloží aktuální tabulku do souboru.

---

## Kde jsou uložena nastavení a chyby

- Nastavení (`settings.ini`):
  - EXE (uživatel): `C:\Users\<uživatel>\AppData\Roaming\EasyFlex\settings.ini`
  - Vývoj (ze zdrojového kódu): `EasyFlex\settings.ini`
- Chybové JSONy z importu do ABRA:
  - EXE (uživatel): `C:\Users\<uživatel>\AppData\Roaming\EasyFlex\errors\`
  - Vývoj: `errors\` (pokud složka existuje), jinak `EasyFlex\errors\`

---

## Ochrana dat a soukromí

- Při extrakci PDF se snímky stránek posílají do služby OpenAI k vyčtení dat.
- CSV „LLM mapování sloupců“ je ve výchozím stavu vypnuto. Pokud jej zapnete, odešlou se do OpenAI hlavičky a ukázkové řádky – používejte jen, pokud je to v pořádku.
- API klíč OpenAI a přihlašovací údaje k ABRA se ukládají do `settings.ini` v prostém textu. Chraňte zařízení a přístup k souboru.

---

## Řešení problémů (FAQ)

- „Tlačítka pro PDF jsou šedá / extrakce nefunguje“
  - V `Nastavení` → `Extractor` doplňte **OpenAI API Key** a uložte.

- „Chyba převodu PDF“
  - Aplikace vyžaduje Poppler. V přibalené verzi je obvykle součástí. Pokud není, doinstalujte Poppler a nastavte `POPPLER_PATH` (nebo klíč `poppler_path` v `settings.ini`).

- „ABRA: company code is not configured“
  - V horní liště vyberte `Firma` (případně přidejte „Nová firma…“) a zkuste import znovu. V `Nastavení` → `Abra` vyplňte server/uživatele/heslo.

- „ABRA server not reachable / DNS failed / 401/403/404“
  - Zkontrolujte `Server`, `Port`, přihlašovací údaje a nastavení TLS v `Nastavení` → `Abra`. Ověřte, zda máte přístup k API ABRA Flexi.

- „Kde najdu detail chyby importu?“
  - Vytváří se JSON soubory v adresáři `errors` (viz výše). Obsahují požadavek i odpověď serveru.

---

## Tipy

- Dvojklikem v tabulce můžete opravit nesprávná políčka ještě před importem.
- Pokud používáte `Auto‑import`, doporučujeme nejdřív otestovat na několika souborech ručně.

---

## Webová vrstva (Flask)

Projekt nyní obsahuje jednoduchou webovou aplikaci (Flask) s přihlášením a základním workflow nahrání PDF/tabulky → náhled faktur → import do ABRA. Každý webový uživatel má vlastní OpenAI/ABRA přihlašovací údaje uložené v databázi (nastavují se na stránce `/settings` po přihlášení).

### Rychlý start (dev)
1. Vytvořte a aktivujte virtuální prostředí: `python -m venv .venv` a `.\.venv\Scripts\activate` (Windows) nebo `source .venv/bin/activate` (macOS/Linux).
2. Nainstalujte závislosti: `pip install -r EasyFlex/requirements.txt` (nebo jen web část `pip install -r webapp/requirements-web.txt`).
3. Do `.env` vložte pouze webové tajemství (`EASYFLEX_SECRET_KEY`, `EASYFLEX_ADMIN_PASSWORD`). OpenAI/ABRA klíče se již nečtou z `.env`.
4. Vytvořte administrátora: `EASYFLEX_ADMIN_PASSWORD=<heslo> python -m webapp.create_admin`. SQLite databáze se uloží do `webapp/easyflex_web.db`. Pokud potřebujete novou schéma (přidali jsme tabulku user settings), můžete starý soubor DB smazat a spustit skript znovu.
5. Spusťte server: `python -m webapp.app` (nebo `flask --app webapp.app run`). Otevřete `http://127.0.0.1:5000/`, přihlaste se jako `admin` a na stránce `/settings` vyplňte svůj OpenAI API Key a přístup k ABRA.
6. Pro produkční nasazení lze použít `gunicorn "webapp.app:app"`.

Desktopová aplikace zůstává zachována: `python -m EasyFlex.main`.

---

Vytvořil: Viktor Vítovec


