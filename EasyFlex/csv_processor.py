"""
Modul pro zpracování tabulkových souborů (CSV, XLSX, XML) s daty faktur.
Vytváří položky podle sloupců FAKTURY.csv a předává je dál jako InvoiceData.
Podporuje částky v různých sazbách DPH (12 % a 21 %) i jednotnou sazbu.

LLM-asistované mapování sloupců:
- nejdříve přímé/heuristické shody,
- poté dotaz na OpenAI (gpt-5) se strict JSON schématem,
- při chybě OpenAI robustní fallback (strip BOM, case-insensitive, česká synonyma).

Omezení: pokud tabulka neobsahuje členění DPH podle sazeb, pole zaklad_dane_12/
vyse_dph_12 zůstávají null; DPH se mapuje na 21 % sloupec „DPH“/„Celkem bez DPH“.
"""
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zipfile import ZipFile, BadZipFile

from xml.etree import ElementTree as ET

import pandas as pd
from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError

from .models import InvoiceData
from .invoice_warnings import append_warnings
from .config import DEFAULT_OPENAI_MODEL, load_config, AppConfig, model_supports_sampling_params
from .date_helpers import parse_invoice_date, domysleni_chybejicich_datumu
from charset_normalizer import from_path as cn_from_path


logger = logging.getLogger(__name__)


XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


FIELD_SPECS: Dict[str, Dict[str, Any]] = {
	"cislo_dokladu": {
		"label": "Číslo faktury",
		"description": "Jedinečné číslo dokladu nebo faktury.",
		"keywords": [["cislo", "fakt"], ["invoice", "number"], ["cislo", "doklad"], ["document", "number"]],
		"type": "string",
	},
	"variabilni_symbol": {
		"label": "Číslo Objednávky",
		"description": "Variabilní symbol nebo číslo objednávky.",
		"keywords": [["variabil"], ["vs"], ["order", "number"], ["po", "number"], ["purchase", "order"], ["reference", "number"], ["po", "reference"], ["order", "reference"], ["customer", "reference"]],
		"type": "string",
	},
	"dodavatel_jmeno": {
		"label": "Dodavatel – název",
		"description": "Název nebo jméno dodavatele (supplier/vendor).",
		"keywords": [["dodavatel", "nazev"], ["dodavatel", "jmeno"], ["supplier", "name"], ["vendor", "name"], ["seller", "name"], ["supplier", "company"]],
		"type": "string",
	},
	"dodavatel_adresa": {
		"label": "Dodavatel – adresa",
		"description": "Adresa dodavatele.",
		"keywords": [["dodavatel", "adresa"], ["supplier", "address"], ["vendor", "address"], ["seller", "address"], ["supplier", "street"], ["vendor", "street"]],
		"type": "string",
	},
	"dodavatel_stat": {
		"label": "Dodavatel – stát",
		"description": "Stát nebo země dodavatele.",
		"keywords": [["dodavatel", "stat"], ["supplier", "country"], ["vendor", "country"], ["seller", "country"]],
		"type": "string",
	},
	"dodavatel_ic": {
		"label": "Dodavatel – IČ",
		"description": "Identifikační číslo dodavatele (IČ, Company ID).",
		"keywords": [["dodavatel", "ic"], ["dodavatel", "ico"], ["supplier", "company", "id"], ["supplier", "registration"], ["business", "id"], ["supplier", "id"]],
		"type": "string",
	},
	"dodavatel_dic": {
		"label": "Dodavatel – DIČ",
		"description": "DIČ nebo VAT číslo dodavatele.",
		"keywords": [["dodavatel", "dic"], ["dodavatel", "vat"], ["supplier", "vat"], ["supplier", "tax", "id"], ["vat", "number", "supplier"], ["seller", "vat"]],
		"type": "string",
	},
	"odberatel_jmeno": {
		"label": "Jméno",
		"description": "Název nebo jméno odběratele (customer/client).",
		"keywords": [["odberatel", "nazev"], ["odberatel", "jmeno"], ["customer", "name"], ["client", "name"], ["buyer", "name"], ["customer", "company"]],
		"type": "string",
	},
	"odberatel_adresa": {
		"label": "Adresa",
		"description": "Adresa odběratele.",
		"keywords": [["odberatel", "adresa"], ["customer", "address"], ["client", "address"], ["buyer", "address"], ["customer", "street"], ["client", "street"], ["buyer", "street"]],
		"type": "string",
	},
	"odberatel_stat": {
		"label": "Odběratel – stát",
		"description": "Stát nebo země odběratele.",
		"keywords": [["odberatel", "stat"], ["customer", "country"], ["client", "country"], ["buyer", "country"]],
		"type": "string",
	},
	"odberatel_ic": {
		"label": "Odběratel – IČ",
		"description": "Identifikační číslo odběratele (IČ).",
		"keywords": [["odberatel", "ic"], ["odberatel", "ico"], ["customer", "company", "id"], ["client", "id"], ["buyer", "id"]],
		"type": "string",
	},
	"odberatel_dic": {
		"label": "Odběratel – DIČ",
		"description": "DIČ nebo VAT číslo odběratele.",
		"keywords": [["odberatel", "dic"], ["customer", "vat"], ["client", "vat"], ["buyer", "vat"], ["customer", "tax", "id"]],
		"type": "string",
	},
	"datum_vystaveni": {
		"label": "Datum vystavení",
		"description": "Datum vystavení faktury (issue date).",
		"keywords": [["datum", "vystav"], ["issue", "date"], ["invoice", "date"], ["date", "issued"]],
		"type": "date",
	},
	"datum_duzp": {
		"label": "Datum objednávky",
		"description": "Datum DUZP / zdanitelného plnění / tax point.",
		"keywords": [["datum", "duzp"], ["duzp"], ["zdanitel", "plnen"], ["tax", "date"], ["tax", "point"], ["delivery", "date"]],
		"type": "date",
	},
	"datum_splatnosti": {
		"label": "Datum splatnosti",
		"description": "Datum splatnosti (due date).",
		"keywords": [["splat"], ["due", "date"], ["payment", "due"], ["pay", "by"], ["deadline"]],
		"type": "date",
	},
	"zaklad_dane_0": {
		"label": "Základ daně 0 %",
		"description": "Základ daně bez DPH v sazbě 0 %.",
		"keywords": [["zaklad", "0"], ["net", "0"], ["base", "0"], ["without", "vat", "0"], ["0", "bez", "dph"], ["rate", "0"]],
		"type": "number",
	},
	"zaklad_dane_12": {
		"label": "Částka celkem bez DPH v sazbě 12%",
		"description": "Základ daně bez DPH v sazbě 12 %.",
		"keywords": [["zaklad", "12"], ["net", "12"], ["base", "12"], ["12", "bez", "dph"], ["12", "without", "vat"], ["rate", "12"]],
		"type": "number",
	},
	"zaklad_dane_21": {
		"label": "Částka celkem bez DPH v sazbě 21%",
		"description": "Základ daně bez DPH v sazbě 21 %.",
		"keywords": [["zaklad", "21"], ["net", "21"], ["base", "21"], ["21", "bez", "dph"], ["21", "without", "vat"], ["rate", "21"]],
		"type": "number",
	},
	"vyse_dph_12": {
		"label": "DPH 12 %",
		"description": "Výše DPH v sazbě 12 %.",
		"keywords": [["dph", "12"], ["vat", "12"], ["tax", "12"], ["12", "dph"]],
		"type": "number",
	},
	"vyse_dph_21": {
		"label": "DPH 21 %",
		"description": "Výše DPH v sazbě 21 %.",
		"keywords": [["dph", "21"], ["vat", "21"], ["tax", "21"], ["21", "dph"]],
		"type": "number",
	},
	"celkova_cena": {
		"label": "Celkem k úhradě",
		"description": "Celková částka k úhradě (gross amount).",
		"keywords": [["celkem", "uhrad"], ["celkova", "castka"], ["total", "amount"], ["gross", "total"], ["amount", "due"], ["grand", "total"]],
		"type": "number",
	},
	"psc": {
		"label": "PSČ",
		"description": "PSČ odběratele (použije se pro doplnění adresy).",
		"keywords": [["psc"], ["postal", "code"], ["zip"], ["postcode"]],
		"type": "string",
	},
	"mesto": {
		"label": "Město",
		"description": "Město odběratele (doplnění adresy).",
		"keywords": [["mesto"], ["city"], ["town"], ["municipality"]],
		"type": "string",
	},
}

FALLBACK_SYNONYMS: Dict[str, List[str]] = {
	"cislo_dokladu": ["cislo dokladu"],
	"variabilni_symbol": ["variabilni symbol"],
	"odberatel_jmeno": ["nazev/jmeno", "nazev jmeno", "odberatel"],
	"odberatel_ic": ["ic"],
	"odberatel_dic": ["dic / ic dph", "dic", "ic dph"],
	"datum_vystaveni": ["vystaveno", "datum vystaveni", "vystaveni"],
	"datum_splatnosti": ["splatnost"],
	"datum_duzp": ["duzp"],
	"celkova_cena": ["celkem s dph", "celkem s dani"],
	"zaklad_dane_21": ["celkem bez dph", "zaklad dane"],
	"vyse_dph_21": ["dph"],
}


class CSVProcessor:
	"""Procesor pro tabulkové soubory s daty faktur."""

	def __init__(self, config: Optional[AppConfig] = None) -> None:
		self.logger = logger
		self._config: Optional[AppConfig] = config
		self._field_specs: Dict[str, Dict[str, Any]] = FIELD_SPECS
		# Mapování cílových interních polí na defaultní (očekávané) názvy sloupců
		self._expected_headers_by_target: Dict[str, str] = {
			name: spec["label"]
			for name, spec in self._field_specs.items()
		}
		# Obrácená mapa: očekávaný (český) název → interní pole
		self._target_by_label: Dict[str, str] = {
			spec["label"]: name
			for name, spec in self._field_specs.items()
		}
		# Bude naplněno detekcí/LLM: interní pole → skutečná hlavička v CSV
		self._column_map: Dict[str, str] = {}

	def _get_config(self) -> AppConfig:
		return self._config or load_config()

	def _day_first(self) -> bool:
		"""Aktuální preference pořadí dne/měsíce podle konfigurace."""
		cfg = self._get_config()
		return getattr(cfg, "date_day_first", True)

	def process_csv_file(self, csv_path: str) -> List[InvoiceData]:
		"""Zachovaná alias metoda pro kompatibilitu se starým API."""
		return self.process_table_file(csv_path)

	def process_table_file(self, table_path: str) -> List[InvoiceData]:
		"""Načte libovolnou tabulku (CSV/XLSX/XML) a vrátí InvoiceData objekty."""
		try:
			path = Path(table_path)
			df = self._load_table(path)
			return self._process_dataframe(df, str(path))
		except Exception:
			self.logger.exception("Chyba při zpracování souboru %s", table_path)
			raise

	def _process_dataframe(self, df: pd.DataFrame, source_label: str) -> List[InvoiceData]:
		self.logger.info("Načtena tabulka: %s, řádků: %s", source_label, len(df))
		df = df.copy()
		if isinstance(df.columns, pd.MultiIndex):
			df.columns = [
				" ".join(str(part) for part in col if part not in (None, "")) or "sloupec"
				for col in df.columns
			]
		else:
			df.columns = [str(col) for col in df.columns]

		# Infer column mapping before iterating rows (LLM-assisted with fallback)
		self._column_map = self._infer_column_map(df)
		if self._column_map:
			self.logger.info(
				"Mapování sloupců: %s",
				{k: self._column_map.get(k) for k in sorted(self._expected_headers_by_target.keys())},
			)

		invoices: List[InvoiceData] = []
		for index, row in df.iterrows():
			try:
				invoice_data = self._map_row_to_invoice_data(row, index + 1)
				if invoice_data:
					invoices.append(invoice_data)
			except Exception as exc:  # noqa: BLE001
				self.logger.error("Chyba při zpracování řádku %s: %s", index + 1, exc)
				continue

		self.logger.info("Úspěšně zpracováno %s faktur z tabulky", len(invoices))
		return invoices

	def _load_table(self, path: Path) -> pd.DataFrame:
		suffix = path.suffix.lower()
		if suffix in {".csv", ".txt"}:
			return self._load_csv(path)
		if suffix in {".xlsx", ".xls"}:
			return self._load_excel(path)
		if suffix == ".xml":
			return self._load_xml(path)
		raise ValueError(f"Nepodporovaný formát tabulky: {path.suffix}")

	def _load_csv(self, path: Path) -> pd.DataFrame:
		encoding = "utf-8"
		try:
			best = cn_from_path(str(path)).best()
			if best is not None and best.encoding:
				encoding = best.encoding
		except Exception:
			pass
		return pd.read_csv(path, sep=None, engine="python", encoding=encoding)

	def _load_excel(self, path: Path) -> pd.DataFrame:
		try:
			return pd.read_excel(path, sheet_name=0)
		except Exception as exc:  # noqa: BLE001
			if self._is_missing_openpyxl(exc):
				self.logger.info("openpyxl není k dispozici – používám fallback parser", exc_info=exc)
				return self._load_excel_fallback(path)
			if isinstance(exc, ValueError):
				self.logger.info("Pandas neumí soubor přečíst, zkouším fallback parser", exc_info=exc)
				return self._load_excel_fallback(path)
			raise

	def _is_missing_openpyxl(self, exc: Exception) -> bool:
		if isinstance(exc, ImportError):
			return True
		msg = str(exc).lower()
		return "openpyxl" in msg and ("missing" in msg or "no module named" in msg)

	def _load_xml(self, path: Path) -> pd.DataFrame:
		# Rychlý pokus využít interní parser Pandas
		try:
			df = pd.read_xml(path)
			if df is not None and not df.empty:
				return df
		except (ValueError, ET.ParseError, ImportError):
			pass

		try:
			tree = ET.parse(path)
		except ET.ParseError as exc:
			raise ValueError(f"XML soubor {path} obsahuje chybu: {exc}") from exc

		root = tree.getroot()
		rows = self._extract_xml_rows(root)
		if not rows:
			raise ValueError(f"V XML souboru {path} nebyla nalezena tabulka dat.")
		return pd.DataFrame(rows)

	def _load_excel_fallback(self, path: Path) -> pd.DataFrame:
		try:
			with ZipFile(path) as archive:
				shared_strings = self._read_shared_strings(archive)
				sheet_hint = self._first_sheet_path(archive)
				for candidate in (
					sheet_hint,
					f"xl/{sheet_hint}" if sheet_hint and not sheet_hint.startswith("xl/") else None,
					"xl/worksheets/sheet1.xml",
					"worksheets/sheet1.xml",
				):
					if candidate and candidate in archive.namelist():
						sheet_path = candidate
						break
				else:
					sheet_path = "xl/worksheets/sheet1.xml"
				raw_rows = self._read_sheet_rows(archive, sheet_path, shared_strings)
		except (KeyError, ET.ParseError, BadZipFile) as exc:
			raise ValueError(f"Excel soubor {path} je poškozený nebo neúplný: {exc}") from exc

		if not raw_rows:
			return pd.DataFrame()
		max_col = max((max(r.keys(), default=-1) for r in raw_rows), default=-1) + 1
		if max_col <= 0:
			return pd.DataFrame()
		table_rows: List[List[Optional[str]]] = []
		for row in raw_rows:
			line: List[Optional[str]] = [None] * max_col
			for idx, value in row.items():
				if 0 <= idx < max_col:
					line[idx] = value
			table_rows.append(line)

		headers = table_rows[0]
		columns = [
			(str(header).strip() if header not in (None, "") else f"Sloupec {i + 1}")
			for i, header in enumerate(headers)
		]
		data = [
			[(cell.strip() if isinstance(cell, str) else cell) for cell in row]
			for row in table_rows[1:]
		]
		return pd.DataFrame(data, columns=columns)

	def _read_shared_strings(self, archive: ZipFile) -> List[str]:
		if "xl/sharedStrings.xml" not in archive.namelist():
			return []
		root = ET.parse(archive.open("xl/sharedStrings.xml")).getroot()
		strings: List[str] = []
		for si in root.findall(f"{XL_NS}si"):
			parts = [node.text or "" for node in si.findall(f".//{XL_NS}t")]
			strings.append("".join(parts))
		return strings

	def _first_sheet_path(self, archive: ZipFile) -> str:
		rel_map: Dict[str, str] = {}
		if "xl/_rels/workbook.xml.rels" in archive.namelist():
			rels_root = ET.parse(archive.open("xl/_rels/workbook.xml.rels")).getroot()
			for rel in rels_root.findall(f"{PKG_REL_NS}Relationship"):
				rel_id = rel.attrib.get("Id")
				rel_type = rel.attrib.get("Type", "")
				if rel_type.endswith("/worksheet"):
					target = rel.attrib.get("Target", "")
					if target.startswith("/"):
						rel_map[rel_id] = target.lstrip("/")
					else:
						rel_map[rel_id] = f"xl/{target}" if not target.startswith("xl/") else target
		if "xl/workbook.xml" in archive.namelist():
			workbook_root = ET.parse(archive.open("xl/workbook.xml")).getroot()
			sheets = workbook_root.findall(f"{XL_NS}sheets/{XL_NS}sheet")
			for sheet in sheets:
				rel_id = sheet.attrib.get(f"{REL_NS}id")
				if rel_id and rel_id in rel_map:
					return rel_map[rel_id]
		return "xl/worksheets/sheet1.xml"

	def _read_sheet_rows(
		self,
		archive: ZipFile,
		sheet_path: str,
		shared_strings: List[str],
	) -> List[Dict[int, Optional[str]]]:
		with archive.open(sheet_path) as sheet_file:
			sheet_root = ET.parse(sheet_file).getroot()
		rows: List[Dict[int, Optional[str]]] = []
		sheet_data = sheet_root.find(f"{XL_NS}sheetData")
		if sheet_data is None:
			return rows
		for row in sheet_data.findall(f"{XL_NS}row"):
			row_data: Dict[int, Optional[str]] = {}
			for cell in row.findall(f"{XL_NS}c"):
				ref = cell.attrib.get("r")
				idx = self._excel_ref_to_index(ref)
				if idx is None:
					continue
				value = self._parse_excel_cell(cell, shared_strings)
				if value is not None:
					row_data[idx] = value
			if row_data:
				rows.append(row_data)
		return rows

	def _excel_ref_to_index(self, ref: Optional[str]) -> Optional[int]:
		if not ref:
			return None
		letters = ''.join(ch for ch in ref if ch.isalpha())
		if not letters:
			return None
		idx = 0
		for ch in letters.upper():
			if 'A' <= ch <= 'Z':
				idx = idx * 26 + (ord(ch) - ord('A') + 1)
			else:
				return None
		return idx - 1

	def _parse_excel_cell(self, cell: ET.Element, shared_strings: List[str]) -> Optional[str]:
		cell_type = cell.attrib.get("t")
		if cell_type == "inlineStr":
			texts = [node.text or "" for node in cell.findall(f".//{XL_NS}t")]
			text = "".join(texts).strip()
			return text or None
		value = cell.findtext(f"{XL_NS}v")
		if cell_type == "s":
			if value is None:
				return None
			try:
				index = int(value)
			except ValueError:
				return None
			if 0 <= index < len(shared_strings):
				return shared_strings[index]
			return None
		if cell_type == "b":
			return "1" if (value or "").strip() == "1" else "0"
		if value is None:
			return None
		return value.strip()

	def _extract_xml_rows(self, root: ET.Element) -> List[Dict[str, Any]]:
		# Nejprve zkusíme identifikovat prvky opakující se pod jedním rodičem
		for element in root.iter():
			children = list(element)
			if not children:
				continue
			tag_counts: Dict[str, int] = {}
			for child in children:
				tag_counts[child.tag] = tag_counts.get(child.tag, 0) + 1
			repeated_tags = [tag for tag, count in tag_counts.items() if count > 1]
			if repeated_tags:
				tag = repeated_tags[0]
				candidates = [self._xml_row_to_record(item) for item in element.findall(tag)]
				rows = [row for row in candidates if row]
				if rows:
					return rows
		# Fallback: použijeme přímé potomky kořene
		rows = [self._xml_row_to_record(child) for child in list(root)]
		return [row for row in rows if row]

	def _xml_row_to_record(self, element: ET.Element) -> Dict[str, Any]:
		record: Dict[str, Any] = {}
		node_key = self._normalize_xml_key(element.tag)
		for attr_key, attr_val in element.attrib.items():
			record[f"{node_key}@{self._normalize_xml_key(attr_key)}"] = attr_val
		children = list(element)
		text = (element.text or "").strip()
		if text and not children:
			record[node_key] = text
		for child in children:
			record.update(self._xml_child_to_record(child))
		return record

	def _xml_child_to_record(self, element: ET.Element, prefix: str = "") -> Dict[str, Any]:
		base = self._normalize_xml_key(element.tag)
		key_prefix = f"{prefix}{base}" if prefix else base
		record: Dict[str, Any] = {}
		for attr_key, attr_val in element.attrib.items():
			record[f"{key_prefix}@{self._normalize_xml_key(attr_key)}"] = attr_val
		children = list(element)
		text = (element.text or "").strip()
		if children:
			if text:
				record[key_prefix] = text
			for child in children:
				record.update(self._xml_child_to_record(child, prefix=f"{key_prefix}."))
		else:
			record[key_prefix] = text if text else None
		return record

	def _normalize_xml_key(self, key: str) -> str:
		key = str(key or "").strip()
		key = key.replace("-", " ")
		key = key.replace("_", " ")
		key = re.sub(r"\s+", " ", key)
		return key or "hodnota"

	def _map_row_to_invoice_data(self, row: pd.Series, row_number: int) -> Optional[InvoiceData]:
		"""
		Mapuje jeden řádek CSV na InvoiceData objekt podle struktury FAKTURY.csv.
		Informace o jménu a adrese jsou o odběrateli.
		"""
		try:
			cfg = self._get_config()
			day_first = getattr(cfg, "date_day_first", True)
			invoice_data = InvoiceData()

			# Základní informace
			invoice_data.cislo_dokladu = self._safe_get(row, "Číslo faktury")
			invoice_data.variabilni_symbol = self._safe_get(row, "Číslo Objednávky")
			if getattr(cfg, "use_doc_number_as_variable_symbol", False) and invoice_data.cislo_dokladu:
				invoice_data.variabilni_symbol = invoice_data.cislo_dokladu

			# Dodavatel
			dodavatel_jmeno = self._safe_get(row, "Dodavatel – název")
			if dodavatel_jmeno:
				invoice_data.dodavatel_jmeno = dodavatel_jmeno
			dodavatel_adresa = self._safe_get(row, "Dodavatel – adresa")
			if dodavatel_adresa:
				invoice_data.dodavatel_adresa = dodavatel_adresa
			dodavatel_stat = self._safe_get(row, "Dodavatel – stát")
			if dodavatel_stat:
				invoice_data.dodavatel_stat = dodavatel_stat
			dodavatel_ic = self._safe_get(row, "Dodavatel – IČ")
			if dodavatel_ic:
				invoice_data.dodavatel_ic = dodavatel_ic
			dodavatel_dic = self._safe_get(row, "Dodavatel – DIČ")
			if dodavatel_dic:
				invoice_data.dodavatel_dic = dodavatel_dic

			# Datum
			invoice_data.datum_vystaveni = parse_invoice_date(self._safe_get(row, "Datum vystavení"), day_first=day_first)
			invoice_data.datum_duzp = parse_invoice_date(self._safe_get(row, "Datum objednávky"), day_first=day_first)
			invoice_data.datum_splatnosti = parse_invoice_date(self._safe_get(row, "Datum splatnosti"), day_first=day_first)

			# Odběratel
			invoice_data.odberatel_jmeno = self._safe_get(row, "Jméno")
			invoice_data.odberatel_adresa = self._safe_get(row, "Adresa")
			odberatel_stat = self._safe_get(row, "Odběratel – stát")
			if odberatel_stat:
				invoice_data.odberatel_stat = odberatel_stat
			odberatel_ic = self._safe_get(row, "Odběratel – IČ")
			if odberatel_ic:
				invoice_data.odberatel_ic = odberatel_ic
			odberatel_dic = self._safe_get(row, "Odběratel – DIČ")
			if odberatel_dic:
				invoice_data.odberatel_dic = odberatel_dic
			if not invoice_data.odberatel_stat:
				invoice_data.odberatel_stat = "CZ"

			psc = self._safe_get(row, "PSČ")
			mesto = self._safe_get(row, "Město")
			if psc and mesto:
				addr = invoice_data.odberatel_adresa or ""
				invoice_data.odberatel_adresa = f"{addr}, {psc} {mesto}".strip().strip(', ')

			# Finanční údaje (bez DPH) dle sazeb – pouze opsané hodnoty, bez výpočtů
			zaklad_0 = self._safe_float(self._safe_get(row, "Základ daně 0 %"))
			zaklad_12 = self._safe_float(self._safe_get(row, "Částka celkem bez DPH v sazbě 12%"))
			zaklad_21 = self._safe_float(self._safe_get(row, "Částka celkem bez DPH v sazbě 21%"))

			vyse_dph_12 = self._safe_float(self._safe_get(row, "DPH 12 %"))
			vyse_dph_21 = self._safe_float(self._safe_get(row, "DPH 21 %"))
			celkem = self._safe_float(self._safe_get(row, "Celkem k úhradě"))

			invoice_data.zaklad_dane_0 = zaklad_0
			invoice_data.zaklad_dane_12 = zaklad_12
			invoice_data.zaklad_dane_21 = zaklad_21
			invoice_data.vyse_dph_12 = vyse_dph_12
			invoice_data.vyse_dph_21 = vyse_dph_21
			invoice_data.celkova_cena = celkem

			# Nově položky nevytváříme – pracujeme jen s horními souhrny
			invoice_data.položky = None
			invoice_data.mena = "CZK"

			date_payload = {
				"datum_vystaveni": invoice_data.datum_vystaveni,
				"datum_splatnosti": getattr(invoice_data, "datum_splatnosti", None),
				"datum_duzp": invoice_data.datum_duzp,
			}
			inferred_warnings: List[str] = []
			if getattr(cfg, "infer_missing_dates", False):
				date_payload, inferred_warnings = domysleni_chybejicich_datumu(date_payload, day_first=day_first)
			invoice_data.datum_vystaveni = date_payload.get("datum_vystaveni")
			invoice_data.datum_splatnosti = date_payload.get("datum_splatnosti")
			invoice_data.datum_duzp = date_payload.get("datum_duzp")
			append_warnings(invoice_data, inferred_warnings)
			if row_number <= 5:
				preview = {
					"cislo_dokladu": invoice_data.cislo_dokladu,
					"variabilni_symbol": invoice_data.variabilni_symbol,
					"odberatel_jmeno": invoice_data.odberatel_jmeno,
				}
				self.logger.info("CSV řádek %s náhled: %s", row_number, preview)
			self.logger.debug(
				"Řádek %s: %s - Z12=%s Z21=%s",
				row_number,
				invoice_data.cislo_dokladu,
				zaklad_12,
				zaklad_21,
			)
			return invoice_data
		except Exception as exc:  # noqa: BLE001
			self.logger.error("Chyba při mapování řádku %s: %s", row_number, exc)
			return None

	def _safe_float(self, value: Optional[str]) -> Optional[float]:
		if not value:
			return None
		try:
			clean_value = str(value)
			# Remove currency symbols and spaces
			clean_value = re.sub(r"[\s\u00A0\u202F]", "", clean_value)
			# Replace comma decimal separator with dot
			clean_value = clean_value.replace(',', '.')
			# Remove thousands separators like 1.234.567,89 → 1234567.89
			clean_value = re.sub(r"(?<=\d)\.(?=\d{3}(?:\D|$))", "", clean_value)
			# Keep leading minus if present
			clean_value = re.sub(r"^(-)?(.*)$", lambda m: (m.group(1) or '') + m.group(2), clean_value)
			if clean_value == '' or clean_value == '-':
				return None
			return float(clean_value)
		except (ValueError, TypeError):
			return None

	def _safe_get(self, row: pd.Series, column_name: str) -> Optional[str]:
		try:
			# 1) Přímý název sloupce
			if column_name in row.index:
				value = row[column_name]
				if pd.isna(value) or value == "":
					return None
				return str(value).strip()
			# 2) Mapování: český label → interní pole → skutečná hlavička
			target = self._target_by_label.get(column_name)
			if target:
				mapped_header = self._column_map.get(target)
				if mapped_header and mapped_header in row.index:
					value = row[mapped_header]
					if pd.isna(value) or value == "":
						return None
					return str(value).strip()
			return None
		except Exception:
			return None

	def _infer_column_map(self, df: pd.DataFrame) -> Dict[str, str]:
		"""Zkusí odvodit mapování sloupců:
		1) Přímé shody s očekávanými názvy
		2) Heuristické shody (normalizace, synonyma)
		3) LLM (OpenAI) na základě hlaviček + prvních dvou řádků
		"""
		cols = list(df.columns)
		mapping: Dict[str, str] = {}
		# 1) Přímé shody
		for target, expected_label in self._expected_headers_by_target.items():
			if expected_label in df.columns:
				mapping[target] = expected_label
		# 2) Heuristika pro chybějící
		remaining = [t for t in self._expected_headers_by_target.keys() if t not in mapping]
		norm_cols = {c: self._normalize(c) for c in cols}
		if remaining:
			for target in list(remaining):
				cand = self._heuristic_match(target, norm_cols, df)
				if cand:
					mapping[target] = cand
		existing_headers = set(mapping.values())
		# 3) LLM – pokud máme klíč a něco chybí, nebo vždy, aby pokryl i jiné formáty
		cfg = self._get_config()
		use_llm = bool(cfg.openai_api_key) and bool(getattr(cfg, 'csv_enable_llm_mapping', False))
		need_llm = use_llm and (len(mapping) < len(self._expected_headers_by_target))
		if need_llm:
			self.logger.info("Mapování sloupců (LLM) – model %s", cfg.openai_model or DEFAULT_OPENAI_MODEL)
			try:
				llm_map = self._ask_llm_for_mapping(df)
				llm_map = self._sanitize_llm_mapping(llm_map, df, reserved_headers=existing_headers)
				for target, header in llm_map.items():
					if target in mapping:
						continue
					if header and header in df.columns and header not in existing_headers:
						mapping[target] = header
						existing_headers.add(header)
			except Exception as e:  # noqa: BLE001
				self.logger.warning("LLM mapování selhalo: %s", e)
		# 4) Fallback synonyma pro zbylé cílové klíče
		self._apply_synonym_fallback(mapping, df, existing_headers)
		return mapping

	def _normalize(self, s: str) -> str:
		val = str(s or "").replace("\ufeff", "").strip().strip('"').lower()
		val = ''.join(ch for ch in unicodedata.normalize('NFKD', val) if not unicodedata.combining(ch))
		val = re.sub(r"\s+", " ", val)
		return val

	def _heuristic_match(self, target: str, norm_cols: Dict[str, str], df: Optional[pd.DataFrame]) -> Optional[str]:
		"""Pokusí se spárovat sloupec podle klíčových slov a typu hodnot."""
		spec = self._field_specs.get(target, {})
		patterns = spec.get("keywords", []) or []
		expected_type = spec.get("type")
		best_col: Optional[str] = None
		best_score: Optional[tuple[int, int]] = None
		for col, norm in norm_cols.items():
			token_score = 0
			for token_set in patterns:
				if all(ts in norm for ts in token_set):
					token_score = max(token_score, len(token_set))
			if token_score == 0:
				continue
			type_score = 0
			if df is not None and expected_type:
				type_score = self._type_match_score(df[col], expected_type)
			composite = (token_score + type_score, type_score, token_score)
			if best_score is None or composite > best_score:
				best_score = composite
				best_col = col
		return best_col

	def _fallback_synonym_match(self, target: str, norm_cols: Dict[str, str], used_headers: Set[str]) -> Optional[str]:
		patterns = FALLBACK_SYNONYMS.get(target, [])
		for col, norm in norm_cols.items():
			if col in used_headers:
				continue
			for pat in patterns:
				if pat in norm:
					return col
		return None

	def _apply_synonym_fallback(self, mapping: Dict[str, str], df: pd.DataFrame, used_headers: Set[str]) -> None:
		norm_cols = {c: self._normalize(c) for c in df.columns}
		added = 0
		for target in self._expected_headers_by_target.keys():
			if target in mapping:
				continue
			cand = self._fallback_synonym_match(target, norm_cols, used_headers)
			if cand:
				mapping[target] = cand
				used_headers.add(cand)
				added += 1
		if added:
			self.logger.info("Mapování sloupců (fallback) doplnilo %s položek", added)

	def _type_match_score(self, series: pd.Series, expected_type: str) -> int:
		"""Vyšší skóre znamená lepší shodu s očekávaným typem."""
		values = [val for val in series.head(10) if not pd.isna(val) and str(val).strip() != ""]
		if not values:
			return 0
		if expected_type == "number":
			for val in values:
				if isinstance(val, (int, float)):
					return 2
				if self._safe_float(str(val)) is not None:
					return 1
			return -1
		if expected_type == "date":
			day_first = self._day_first()
			for val in values:
				text = str(val).strip()
				if not text:
					continue
				parsed = None
				try:
					parsed = parse_invoice_date(text, day_first=day_first)
				except Exception:
					parsed = None
				if parsed:
					return 2
				if any(sep in text for sep in ("-", ".", "/")) or text.isdigit() and len(text) == 8:
					return 1
			return -1
		return 0

	def _best_header_fuzzy(self, reported: str, cols: List[str]) -> Optional[str]:
		rep = self._normalize(reported)
		cands = [(c, self._normalize(c)) for c in cols]
		# exact normalized match
		for c, n in cands:
			if n == rep:
				return c
		# startswith/contains
		for c, n in cands:
			if n.startswith(rep) or rep.startswith(n) or rep in n or n in rep:
				return c
		return None

	def _column_summaries(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
		"""Postaví stručný popis sloupců pro prompt (typ + vzorky)."""
		summaries: List[Dict[str, Any]] = []
		for col in df.columns:
			series = df[col]
			summaries.append(
				{
					"name": str(col),
					"normalized": self._normalize(col),
					"type_hint": self._infer_series_kind(series),
					"samples": self._sample_values(series),
				}
			)
		return summaries

	def _sample_values(self, series: pd.Series, limit: int = 3) -> List[str]:
		samples: List[str] = []
		for val in series:
			if pd.isna(val):
				continue
			text = str(val).strip()
			if not text or text in samples:
				continue
			if len(text) > 80:
				text = text[:77] + "..."
			samples.append(text)
			if len(samples) >= limit:
				break
		return samples

	def _infer_series_kind(self, series: pd.Series) -> str:
		values = [val for val in series.head(8) if not pd.isna(val) and str(val).strip() != ""]
		if not values:
			return "unknown"
		number_hits = 0
		date_hits = 0
		code_hits = 0
		day_first = self._day_first()
		for val in values:
			text = str(val).strip()
			if self._safe_float(text) is not None:
				number_hits += 1
			try:
				if parse_invoice_date(text, day_first=day_first):
					date_hits += 1
			except Exception:
				pass
			if re.fullmatch(r"[A-Za-z]{2,3}", text) or re.fullmatch(r"[A-Za-z]{2}\d{2,}", text):
				code_hits += 1
		if number_hits >= max(date_hits, code_hits) and number_hits > 0:
			return "number-like"
		if date_hits >= max(number_hits, code_hits) and date_hits > 0:
			return "date-like"
		if code_hits > 0:
			return "code/id"
		return "text"

	def _target_header_score(self, target: str, header: str, df: pd.DataFrame) -> Tuple[int, int, int]:
		spec = self._field_specs.get(target, {})
		norm_header = self._normalize(header)
		token_score = 0
		for token_set in spec.get("keywords", []):
			if all(ts in norm_header for ts in token_set):
				token_score = max(token_score, len(token_set))
		type_score = 0
		expected_type = spec.get("type")
		if expected_type and header in df.columns:
			type_score = self._type_match_score(df[header], expected_type)
		priority = 1 if target in ("celkova_cena", "cislo_dokladu", "datum_vystaveni") else 0
		return (token_score, type_score, priority)

	def _pick_best_target_for_header(self, header: str, targets: List[str], df: pd.DataFrame) -> Optional[str]:
		best_target: Optional[str] = None
		best_score: Optional[Tuple[int, int, int]] = None
		for target in targets:
			score = self._target_header_score(target, header, df)
			if best_score is None or score > best_score:
				best_score = score
				best_target = target
		return best_target

	def _sanitize_llm_mapping(
		self,
		llm_map: Dict[str, Optional[str]],
		df: pd.DataFrame,
		reserved_headers: Optional[Set[str]] = None,
	) -> Dict[str, str]:
		"""Validuje a zpřesní mapu z LLM: odstraní neznámé hlavičky a duplicity."""
		if not isinstance(llm_map, dict):
			return {}
		allowed_targets = set(self._expected_headers_by_target.keys())
		columns = [str(c) for c in df.columns]
		header_set = set(columns)
		used_headers: Set[str] = set(reserved_headers or set())
		validated: Dict[str, Optional[str]] = {}
		for target, raw_header in llm_map.items():
			if target not in allowed_targets:
				continue
			header = None
			if raw_header and raw_header in header_set:
				header = raw_header
			elif raw_header:
				header = self._best_header_fuzzy(str(raw_header), columns)
			if header in used_headers:
				header = None
			validated[target] = header
		header_to_targets: Dict[str, List[str]] = {}
		for target, header in validated.items():
			if header:
				header_to_targets.setdefault(header, []).append(target)
		result: Dict[str, str] = {}
		for target, header in validated.items():
			if header and len(header_to_targets.get(header, [])) == 1 and header not in used_headers:
				result[target] = header
				used_headers.add(header)
		for header, targets in header_to_targets.items():
			if len(targets) <= 1 or header in used_headers:
				continue
			best_target = self._pick_best_target_for_header(header, targets, df)
			if best_target:
				result[best_target] = header
				used_headers.add(header)
		available_norm = {c: self._normalize(c) for c in columns if c not in used_headers}
		for target, header in validated.items():
			if target in result:
				continue
			cand = self._heuristic_match(target, available_norm, df)
			if cand and cand not in used_headers:
				result[target] = cand
				used_headers.add(cand)
		return result

	def _ask_llm_for_mapping(self, df: pd.DataFrame) -> Dict[str, Optional[str]]:
		"""Zavolá OpenAI a požádá o mapování hlaviček na interní klíče.
		Vrací dict: interní_pole → název hlavičky nebo None.
		"""
		cfg = self._get_config()
		if not cfg.openai_api_key:
			return {}
		client = OpenAI(api_key=cfg.openai_api_key)
		# Připrav vzorek: prvních 10 řádků pro lepší kontext
		sample_count = min(10, len(df))
		sample_rows: List[Dict[str, Any]] = []
		for i in range(sample_count):
			row = {}
			for col in df.columns:
				val = df.iloc[i][col]
				row[str(col)] = None if pd.isna(val) else (str(val).strip() if not isinstance(val, (int, float)) else float(val))
			sample_rows.append(row)
		columns = [str(c) for c in df.columns]
		column_summaries = self._column_summaries(df)
		# Sestav JSON schema pro výstup
		properties = {}
		allowed_headers = [str(col) for col in df.columns]
		for key in self._expected_headers_by_target.keys():
			spec = self._field_specs.get(key, {})
			prop: Dict[str, Any] = {
				"type": ["string", "null"],
				"enum": allowed_headers + [None],
			}
			description = spec.get("description")
			if description:
				prop["description"] = description
			properties[key] = prop
		response_format = {
			"type": "json_schema",
			"json_schema": {
				"name": "CsvColumnMapping",
				"schema": {
					"type": "object",
					"additionalProperties": False,
					"properties": properties,
					"required": list(properties.keys()),
				},
				"strict": True,
			},
		}
		field_lines = []
		for key in self._expected_headers_by_target.keys():
			spec = self._field_specs.get(key, {})
			label = spec.get("label")
			description = spec.get("description")
			keywords = [" ".join(tokens) for tokens in spec.get("keywords", [])[:3]]
			alias_hint = ", ".join(filter(None, [label, *keywords]))
			type_hint = spec.get("type") or "string"
			field_lines.append(f"- {key}: {description or ''} (typ: {type_hint}, aliasy: {alias_hint})")
		fields_text = "\n".join(field_lines)
		column_lines = []
		for item in column_summaries:
			sample_preview = ", ".join(item["samples"]) if item["samples"] else "bez hodnot"
			column_lines.append(
				f"- {item['name']} | typ: {item['type_hint']} | vzorky: {sample_preview}"
			)
		columns_text = "\n".join(column_lines)
		messages = [
			{
				"role": "system",
				"content": (
					"Jsi mapovač sloupců tabulek faktur do interní struktury EasyFlex. Odpověz pouze JSONem dle schématu, bez textu navíc. "
					"Pravidla: (1) vybírej výhradně z dostupných hlaviček, nikdy nevytvářej nové; (2) jeden sloupec přiřaď k nejvýše jednomu poli; "
					"(3) pokud vhodný sloupec chybí nebo je nejasný, nastav null; (4) respektuj datové typy (datum, číslo, text) a ignoruj popisné/poznámkové sloupce; "
					"(5) toleruj diakritiku, varianty názvů i drobné překlepy, ale nehádej hodnoty, které nedávají smysl podle ukázkových dat; "
					"(6) Dodavatel = supplier/vendor, odběratel = customer/client. "
					"(7) Nepřidávej žádné klíče ani komentáře a nevracej duplicitní hlavičky. "
					"Výstupní JSON musí obsahovat přesně interní klíče a hodnotou je přesná hlavička z tabulky nebo null.\n"
					+ fields_text
					+ "\nDostupné sloupce s ukázkami:\n"
					+ columns_text
					+ "\nVýsledek vrať pouze jako JSON dle schématu.")
			},
			{
				"role": "user",
				"content": [
					{"type": "text", "text": "Hlavičky a ukázkové řádky:"},
					{"type": "text", "text": f"columns: {columns}"},
					{"type": "text", "text": f"rows: {sample_rows}"},
				],
			},
		]
		try:
			model_name = (cfg.openai_model or DEFAULT_OPENAI_MODEL)
			max_out_tokens = min(768, max(256, getattr(cfg, 'max_tokens', 1024)))
			request_kwargs = {
				"model": model_name,
				"messages": messages,
				"response_format": response_format,
				"timeout": getattr(cfg, 'openai_timeout_s', 30),
			}
			if model_supports_sampling_params(model_name):
				request_kwargs["temperature"] = float(getattr(cfg, "openai_temperature", 0.0))
				request_kwargs["top_p"] = float(getattr(cfg, "openai_top_p", 0.15))
			token_param = "max_completion_tokens" if "gpt-5" in str(model_name) else "max_tokens"
			request_kwargs[token_param] = max_out_tokens
			if getattr(cfg, "openai_reasoning_effort", None) and "gpt-5" in str(model_name):
				request_kwargs["reasoning_effort"] = getattr(cfg, "openai_reasoning_effort")
			resp = client.chat.completions.create(**request_kwargs)
			choice = resp.choices[0]
			parsed = getattr(getattr(choice, "message", None), "parsed", None)
			if isinstance(parsed, dict):
				return parsed  # type: ignore[return-value]
			# fallback: obsah
			content = choice.message.content  # type: ignore[attr-defined]
			if not content:
				return {}
			import json as _json
			try:
				loaded = _json.loads(content)
				return loaded if isinstance(loaded, dict) else {}
			except Exception:
				# zkuste vyextrahovat první JSON objekt
				start = content.find("{")
				end = content.rfind("}")
				if start != -1 and end != -1 and end > start:
					try:
						loaded = _json.loads(content[start:end+1])
						return loaded if isinstance(loaded, dict) else {}
					except Exception:
						return {}
				return {}
		except (RateLimitError, APITimeoutError, APIConnectionError) as e:
			self.logger.warning("OpenAI dočasná chyba při mapování: %s", e)
			return {}
		except Exception as e:  # noqa: BLE001
			self.logger.warning("OpenAI chyba při mapování: %s", e)
			return {}


