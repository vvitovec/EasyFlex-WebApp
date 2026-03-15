# EasyFlex Audit (Web + Core)

## 1) Zadání a rozsah
Tento audit hodnotí, jak EasyFlex technicky funguje dnes, co umí, jak jsou poskládané makroprocesy i mikrologika komponent, a navrhuje realistické cesty zlepšení.

Rozsah:
- Web aplikace (Flask): autentizace, upload, dávky, editace, import do ABRA.
- Core engine (`EasyFlex/*`): extrakce z PDF přes OpenAI, zpracování tabulek, mapování do ABRA payloadu.
- Datový model, provozní spolehlivost, bezpečnost, testovatelnost, škálování.
- Samostatné posouzení migrace na Next.js.

Mimo rozsah:
- Implementace změn (neprováděno).

---

## 2) Jak EasyFlex funguje dnes (makromanagement procesu)

### 2.1 End-to-end flow
1. Uživatel se přihlásí do Flask webu.
2. Vybere vstup:
   - PDF dávka (`/upload-pdf`) nebo
   - tabulka CSV/XLSX/XML (`/upload-table`).
3. Zpracování:
   - PDF: vytvoří se dávka, běží background thread, extrahuje se přes OpenAI, ukládají se `InvoiceRow`.
   - Tabulka: synchronní parsing + mapování sloupců (heuristika + volitelně LLM), vytvoření dávky.
4. Uživatel vidí výsledky na `/results/<batch_id>`, může editovat řádky.
5. Uživatel vybere ABRA kontext (firma, směr, typ) a spustí import.
6. Import do ABRA probíhá po řádcích, ukládá status/chyby a řeší duplicity přes `kod` + `ext-id` strategii.

### 2.2 Produktové schopnosti
- Multi-user účty + role admin/běžný uživatel.
- Kreditový model (odečet podle typu zpracování).
- PDF extrakce (včetně volitelné segmentace více faktur v jednom PDF).
- Tabulkové importy CSV/XLSX/XML s robustní fallback logikou.
- Ruční editace dat před importem.
- ABRA import s guardraily a deduplikací.
- Historie dávek a průběžný polling stavu dávky.

---

## 3) Architektura a komponenty (mikromanagement)

## 3.1 Web vrstva (Flask)
Hlavní orchestrátor je [`webapp/app.py`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/webapp/app.py).

Klíčové body:
- `create_app()` skládá konfiguraci, DB, auth, routy.
- PDF dávky běží ve vlákně (`threading.Thread`) a stav se ukládá do DB (`InvoiceBatch`).
- Výsledková stránka periodicky polluje `/results/<id>/progress`.
- Import do ABRA jede synchronně v requestu po řádcích.

Silná stránka:
- Celý business flow je ucelený, čitelný a funkčně propojený.

Limit:
- `app.py` je velmi velký monolit (~1300 řádků), mixuje HTTP, orchestrace, billing, import, validace i UX rozhodnutí.

## 3.2 Datový model
[`webapp/models.py`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/webapp/models.py)
- `User`, `UserSettings`, `Company`, `DocType`, `InvoiceBatch`, `InvoiceRow`.
- Běží `db.create_all()` + lightweight migrace přes `ALTER TABLE` za běhu.
- Při startu se „visící“ dávky (`queued/running`) přepnou na `interrupted`.

Silná stránka:
- Dobrá praktičnost pro rychlé nasazení a kompatibilitu starších DB.

Limit:
- Schéma je už širší než „lightweight migration“ model zvládá dlouhodobě čistě.

## 3.3 Konfigurace per user
[`webapp/config_utils.py`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/webapp/config_utils.py)
- Skládá runtime `AppConfig` z base configu + user settings + admin overrides.
- Ne-admin dědí OpenAI klíč od admina.

Silná stránka:
- Pragmatické centralizované skládání konfigurace.

Limit:
- Tenancy je částečně „shared“ (OpenAI key přes admina), což komplikuje auditovatelnost nákladů a izolaci tenantů.

## 3.4 PDF extrakce
[`EasyFlex/extractor.py`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/EasyFlex/extractor.py)
- PDF -> obrázky (Poppler/pdf2image).
- OpenAI Structured Output přes JSON schema.
- Retry, rate limiting, backoff, timeout.
- Post-validace datumů/částek + druhý „strict“ pokus při nekonzistenci.
- Disk cache výsledků podle hash klíče.
- Volitelná segmentace stránek na více faktur.

Silná stránka:
- Nadstandardní robustnost kolem retriů, validity a fallbacků.

Limit:
- Nekompatibilní MIME detail: obrázek se často ukládá jako JPEG, ale posílá s prefixem `data:image/png`.

## 3.5 Tabulkové zpracování
[`EasyFlex/csv_processor.py`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/EasyFlex/csv_processor.py)
- Načítá CSV/XLSX/XML.
- Fallback parser pro XLSX bez openpyxl.
- Heuristické mapování hlaviček + volitelný LLM mapping.
- Normalizace čísel/datumů + infer missing dates.

Silná stránka:
- Vysoká odolnost na reálné, nečisté vstupy.

Limit:
- LLM mapping posílá sample data (včetně potenciálně citlivých údajů) bez jemné redakce.

## 3.6 ABRA integrace
[`EasyFlex/abra.py`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/EasyFlex/abra.py)
- Staví payload do winstrom envelope.
- Guard: blokuje zápis na `/adresar`.
- Lookup partnera v adresáři pouze čtením.
- Idempotence přes stabilní `ext:easyflex:*` ID.
- Dedupe strategie `safe_update`/`skip` při duplicitním `kod`.

Silná stránka:
- Dobře promyšlené guardraily proti nechtěným zásahům do adresáře.

Limit:
- Partner lookup tahá celý adresář (`limit=0`) pro každou fakturu -> výkonový problém při větším objemu.

## 3.7 Frontend (Jinja + vanilla JS)
[`webapp/templates/results.html`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/webapp/templates/results.html), [`webapp/templates/settings.html`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/webapp/templates/settings.html), [`webapp/static/style.css`](/Users/viktorvitovec/Documents/Projekty/EasyFlex%20-%20Web/EasyFlex-WebApp/webapp/static/style.css)
- Server-rendered HTML, polling progressu, client-side třídění tabulky.
- Funkčně dostačující admin/business UI.

Silná stránka:
- Nízká komplexita frontend stacku.

Limit:
- Frontend je těžší na dlouhodobou evoluci (state management, komponentizace, UX testování).

---

## 4) Co je technicky velmi dobré
- Robustní ABRA deduplikace a idempotence (`ext-id`, safe update flow).
- Fallbacky u parserů (CSV encoding detekce, XML fallback, XLSX fallback parser).
- Praktický batch model (`InvoiceBatch` + `InvoiceRow`) a UX s průběžným stavem.
- Jasně oddělený domain model `InvoiceData` a jednotný přenos do UI/importu.
- Ochranná logika proti mutaci adresáře v ABRA.

---

## 5) Hlavní rizika a slabiny

## 5.1 Bezpečnost
1. Citlivé údaje jsou ukládané plaintext (`openai_api_key`, `abra_password`) v DB.
2. Fallback secret key `dev-secret-key` je nebezpečný, pokud chybí env.
3. Chybí CSRF ochrana POST formulářů.
4. Login nemá rate limit / lockout ochranu.

## 5.2 Spolehlivost a provoz
1. Background processing je in-process thread model.
   - Při více gunicorn workers není centrální fronta.
   - Restart procesu přeruší práci (jen se označí `interrupted`).
2. Dlouhé operace importu běží synchronně v requestu.
3. Chybí explicitní limity upload velikosti a robustní backpressure strategie.

## 5.3 Škálování
1. `app.py` jako orchestrátor je příliš koncentrovaný (single-file bottleneck).
2. Partner lookup v ABRA (`adresar` full scan) neškáluje na velké adresáře.
3. Tabulkový import účtuje fixně 2 kredity bez ohledu na velikost dávky (obchodně i výkonově asymetrické).

## 5.4 Konzistence produktu
1. Nekonzistence textů kreditů:
   - `credits.html`: nový uživatel 10 kreditů,
   - jinde (help, users): 100 kreditů.
2. Potenciální nejasnost billingu PDF vs tabulka pro uživatele.

---

## 6) Doporučená roadmapa zlepšení

## 6.1 Fáze A (rychlé, vysoký dopad, nízké riziko)
1. Zabezpečení secrets:
   - Encrypt-at-rest pro `UserSettings` secrets.
   - Povinný `EASYFLEX_SECRET_KEY` bez insecure defaultu.
2. CSRF ochrana a login hardening (rate limit, lockout, audit log).
3. Sjednocení kreditových pravidel + textů v UI/help.
4. Oprava MIME detailu u extrakce (`image/jpeg` vs `image/png` prefix).
5. Nastavení upload limitů a validace (size/type/page-count guardrails).

## 6.2 Fáze B (stabilita a provoz)
1. Přesun background jobů do fronty (RQ/Celery + Redis).
2. Import do ABRA přes async job (neblokovat request/worker).
3. Zavést operation IDs + idempotentní zpracování batch jobů.
4. Caching partner lookupu v rámci dávky (firma + identifikátory).

## 6.3 Fáze C (architektura a scaling)
1. Rozdělit `app.py` na blueprinty/služby:
   - `routes/upload.py`, `routes/results.py`, `routes/settings.py`, `services/batch.py`, `services/import_abra.py`.
2. Zavést standardní migrace (Alembic) místo runtime ALTER bloků.
3. Přidat observability:
   - strukturované logy,
   - metriky (latence extrakce, ABRA success/error ratio, queue depth),
   - tracing jobů.

---

## 7) Varianty většího refaktoru

## Varianta 1: Evoluční modular monolith (Python) - doporučeno
- Zůstane Flask + Python core.
- Silné oddělení route/service/repository vrstev.
- Queue pro async processing.
- Nejnižší riziko, nejrychlejší návratnost.

## Varianta 2: Service split bez kompletního přepisu
- API gateway + 2 služby:
  - Extraction service (OpenAI + PDF/table parsing)
  - ABRA import service
- Vhodné pro vyšší objem a separátní škálování.
- Vyšší provozní složitost než varianta 1.

## Varianta 3: Kompletní rewrite stacku
- Nejvyšší riziko, nejdelší time-to-value.
- Dává smysl jen při zásadní změně produktu/multi-tenant enterprise požadavků.

---

## 8) Next.js - je to možné a praktické?

Krátká odpověď: Ano, možné to je. Prakticky dává smysl jen jako frontend/BFF evoluce, ne jako full rewrite celé domény.

## 8.1 Co je realistické
1. Next.js jako nová UI vrstva + Python backend API zůstává.
2. Postupná migrace obrazovek (dashboard/results/settings) bez "big-bang".
3. Zachování Python core pro:
   - PDF/Poppler pipeline,
   - ABRA business logiku,
   - stávající testované dedupe/import flows.

## 8.2 Co je nepraktické
1. Přepis ABRA + extraction logiky do Node jen kvůli frameworku UI.
2. Přepis pdf2image/poppler pipeline do JS bez silného business důvodu.

## 8.3 Doporučený postup, pokud chcete Next.js
1. Nejdřív udělat interní API kontrakty ve Flasku (REST/JSON).
2. Poté nasadit Next.js frontend nad těmito API.
3. Až pak rozhodovat, zda něco přesouvat z Pythonu.

---

## 9) Prioritizované doporučení (můj názor)

Nejlepší varianta pro výsledek, spolehlivost, údržbu i scaling:
1. Držet Python core (extraction + ABRA) a provést evoluční refaktor modular monolith + queue.
2. Okamžitě zavést bezpečnostní hardening (secrets, CSRF, auth rate limiting, secret key policy).
3. Paralelně sjednotit produktovou logiku kreditů + texty v UI.
4. Next.js řešit až jako druhý krok pro UX a frontend škálování, ne jako přepis celé business logiky.

Tohle je nejvyšší poměr přínos/riziko a nejkratší cesta k robustní produkční verzi.

---

## 10) Stav testování v tomto auditu
Pokus o spuštění testů v aktuálním prostředí neproběhl úspěšně kvůli chybějícímu `pytest` (`python3 -m pytest -q` -> `No module named pytest`).

