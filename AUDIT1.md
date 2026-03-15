# AUDIT: párování dat, rozřazování a mapování sloupců (PDF + Excel/CSV)

## 1) Jak funguje současné řešení

### 1.1 Extrakce z PDF: párování dat a rozřazování

Současná pipeline v `EasyFlex/extractor.py` funguje ve dvou režimech:

1. Jednoduchý režim (`extract_from_pdf`):
- PDF -> obrázky (`pdf2image`) -> base64 -> OpenAI Vision.
- Model vrací striktní JSON podle schématu `InvoiceData`.
- Výstup jde přes postprocessing (normalizace dat, kontroly, warningy).

2. Segmentovaný režim (`extract_from_pdf_multi_segmented`) pro PDF s více fakturami:
- Každá stránka se nejdřív klasifikuje (`_call_openai_segment_page`) na:
  - `is_invoice_page`
  - `role` (`start`, `middle`, `end`, `single`, `unknown`)
  - `doc_key`
  - `confidence`
- Potom `_build_groups_from_segments` rozřadí stránky do skupin faktur.
- Nad každou skupinou se spustí samostatná extrakce.

Jak zde probíhá „párování“:
- Párování stránek do jedné faktury je řízeno kombinací `role + doc_key + confidence`.
- Pokud je v průběhu skupiny výrazná změna `doc_key` a `confidence >= 0.6`, skupina se ukončí a začne nová.
- `non-invoice` stránky skupinu zavírají.

Důležité post-kroky po extrakci:
- `use_doc_number_as_variable_symbol`: volitelně přepíše VS číslem dokladu.
- `infer_missing_dates`: domyslí chybějící data (`domysleni_chybejicich_datumu`) a přidá warningy.
- `_normalize_dates`: normalizace/validace datumu, detekce podezřelých hodnot.
- `_check_amount_consistency`: kontrola součtů částek (základy/DPH/celkem).
- Při problému se udělá retry s přísnějším promptem (`strict_dates` / `strict_amounts`).

Poznámka:
- Není tu párování „mezi dokumenty“ (např. deduplikace mezi různými PDF), ale párování je uvnitř jednoho PDF na úrovni stránek -> faktura.

### 1.2 Extrakce z Excel/CSV/XML: mapování sloupců a rozřazení do interních polí

Hlavní logika je v `EasyFlex/csv_processor.py`.

Načtení souboru:
- CSV: `pandas.read_csv` + autodetekce encodingu (`charset_normalizer`).
- Excel: primárně `pandas.read_excel`; při problému fallback parserem nad XML uvnitř `.xlsx`.
- XML: přes `pandas.read_xml` nebo vlastní flattening XML stromu.

Mapování sloupců (`_infer_column_map`) má 4 kroky:

1. Přímá shoda
- Pokud hlavička přesně odpovídá očekávanému názvu (`FIELD_SPECS -> label`), přiřadí se rovnou.

2. Heuristika
- Normalizace názvů (diakritika, case, whitespace).
- Skórování podle klíčových slov pro každé cílové pole.
- Přihlédnutí k typu hodnot (`number`/`date`/`string`) přes ukázku dat ve sloupci.
- Řešení konfliktu více targetů na jeden sloupec přes `_pick_best_target_for_header`.

3. LLM mapování (volitelné)
- Spouští se jen pokud je API klíč a `csv_enable_llm_mapping=true`.
- Model dostane hlavičky + vzorky řádků + shrnutí sloupců.
- Vrací strict JSON schéma (target -> existující hlavička nebo `null`).
- Výsledek se sanitizuje (`_sanitize_llm_mapping`):
  - odstranění neznámých hlaviček
  - odstranění duplicit
  - fuzzy oprava názvu hlavičky

4. Fallback synonyma
- Poslední doplnění mapy přes ručně definované synonymní fráze.

Mapování řádku na fakturu (`_map_row_to_invoice_data`):
- `safe_get`: nejdřív přímý název sloupce, pak přes inferred mapu.
- Naplní `InvoiceData` pole (dodavatel/odběratel, data, částky, DPH sazby).
- Volitelně doplní data splatnosti/DUZP a přidá warning.

Jak zde probíhá „rozřazování“:
- Každý zdrojový sloupec je přiřazen max. jednomu internímu targetu.
- Každý řádek tabulky je převeden na jednu `InvoiceData` položku.

## 2) Silné stránky současného řešení

- Kombinace deterministických pravidel + LLM fallbacku.
- Striktní JSON schémata na PDF i mapování sloupců (nižší riziko „halucinací“).
- Obrana proti chybám: retry, backoff, fallback modelu, parsing fallbacky.
- Segmentace vícefakturálních PDF řeší reálný problém bez nutnosti ručního střihu.
- Validace dat a částek po extrakci + warningy uživateli.
- Dobrá praktická robustnost pro heterogenní tabulky (CZ/EN synonyma, typové skóre).

## 3) Slabší místa / limity

- PDF segmentace je call-per-page: nákladnější a citlivější na kvalitu klasifikace jednotlivých stránek.
- `doc_key` porovnání je spíš přes exact match; nepoužívá podobnost klíčů (OCR chyby mohou rozdělit jednu fakturu).
- U částek se některá pole nulují na `0.0`, což může maskovat rozdíl mezi „chybí“ a „je opravdu nula“.
- Excel fallback parser je záměrně jednoduchý (omezená práce s formulí, typy buněk, datum serialy).
- Chybí explicitní confidence score per-field (uživatel nevidí, co je nejisté kromě warning textu).
- Mapování sloupců je per-file; neukládá se aktivně jako „profil dodavatele“ pro příští importy.

## 4) Jak by to šlo dělat jinak (a proč)

### Varianta A: Profilové mapování podle „šablony dodavatele“

Nápad:
- Po prvním úspěšném importu uložit profil zdroje (fingerprint hlaviček + finální mapu).
- Při dalším importu stejného typu souboru použít mapu přímo, bez LLM.

Výhody:
- Výrazně nižší cena a latence.
- Stabilnější výsledky u opakovaných datových zdrojů.
- Lepší auditovatelnost (mapa je explicitní a verzovaná).

Co vylepšuje:
- Konzistenci mapování a předvídatelnost.

### Varianta B: Dvouúrovňové mapování sloupců (embedding + pravidla)

Nápad:
- Místo/vedle keyword match použít embedding podobnost mezi hlavičkou a definicí targetu.
- Druhá vrstva: typové a business validace (date/number constraints, blacklist sloupců typu „poznámka“).

Výhody:
- Lepší odolnost na jazykové varianty, překlepy, neobvyklé názvy sloupců.
- Menší závislost na přesně ručně zadaných synonymních klíčových slovech.

Co vylepšuje:
- Recall mapování u neznámých formátů.

### Varianta C: Human-in-the-loop s aktivním učením

Nápad:
- V UI zobrazit auto-mapování + confidence + důvod, uživatel případně opraví.
- Oprava se ukládá a automaticky posiluje další mapování.

Výhody:
- Postupné zlepšování na datech konkrétního zákazníka.
- Méně opakovaných ručních oprav.

Co vylepšuje:
- Dlouhodobou kvalitu mapování bez větších modelových zásahů.

### Varianta D: Lepší párování stránek v PDF přes grafový clustering

Nápad:
- Každé stránce vyrobit fingerprint (číslo dokladu, IČ dodavatele, datum, měna).
- Stránky spojovat do faktur přes skórovací funkci podobnosti (nejen role + exact doc_key).

Výhody:
- Lepší odolnost na OCR chyby a neúplné `doc_key`.
- Menší riziko špatného rozdělení dlouhých faktur.

Co vylepšuje:
- Přesnost segmentace vícefakturálních PDF.

### Varianta E: Canonical intermediate model + provenance

Nápad:
- Každé pole nést i metadata: `source_column/source_page`, `confidence`, `normalization_steps`.

Výhody:
- Silná auditovatelnost (co odkud přišlo).
- Snadnější debug chybných importů.
- Lepší UX pro ruční kontrolu (zvýraznit riziková pole).

Co vylepšuje:
- Trasovatelnost a bezpečnost importu.

## 5) Praktická doporučení (priorita)

1. Krátkodobě:
- Přidat confidence score na úroveň pole (PDF i tabulka).
- Rozlišit „missing“ vs. „0.0“ u finančních polí v interním modelu.
- U PDF segmentace zavést fuzzy porovnání `doc_key` (normalizace + podobnost).

2. Střednědobě:
- Zavést per-supplier/per-template profil mapování sloupců.
- Přidat „explain mode“ pro mapování (proč byl sloupec přiřazen).

3. Dlouhodobě:
- Přesunout segmentaci na robustnější stránkový matching (graf/cluster).
- Zavednout průběžné vyhodnocování kvality na referenční sadě (precision/recall mapování, segmentační přesnost, míra ručních oprav).

## 6) Stručný závěr

Aktuální řešení je už teď prakticky použitelné a relativně robustní: má kombinaci pravidel, LLM, fallbacků a validací. Největší prostor ke zlepšení je v trvalém učení mapování pro opakované zdroje, ve zpřesnění párování stránek u multi-PDF a ve zpřehlednění jistoty jednotlivých vyextrahovaných polí.
