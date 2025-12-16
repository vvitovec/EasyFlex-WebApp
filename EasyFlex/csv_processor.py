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
import json
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
CONFIDENT_CONFIDENCE = 0.72
DEFAULT_MIN_GUESS_CONF = 0.58
DEFAULT_PATTERN_SAMPLE_ROWS = 20


FIELD_SPECS: Dict[str, Dict[str, Any]] = {
	"cislo_dokladu": {
		"label": "Číslo faktury",
		"description": "Jedinečné číslo dokladu nebo faktury.",
		"keywords": [
			["cislo", "fakt"],
			["invoice", "number"],
			["cislo", "doklad"],
			["document", "number"],
			["interni", "cislo"],
			["interni", "doklad"],
			["doklad"],
		],
		"type": "string",
	},
	"variabilni_symbol": {
		"label": "Číslo Objednávky",
		"description": "Variabilní symbol nebo číslo objednávky.",
		"keywords": [
			["variabil"],
			["variabilni", "symbol"],
			["vs"],
			["order", "number"],
			["po", "number"],
			["purchase", "order"],
			["reference", "number"],
			["po", "reference"],
			["order", "reference"],
			["customer", "reference"],
		],
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
		"keywords": [
			["odberatel", "nazev"],
			["odberatel", "jmeno"],
			["customer", "name"],
			["client", "name"],
			["buyer", "name"],
			["customer", "company"],
			["firma"],
			["nazev"],
			["jmeno"],
			["obchodni", "jmeno"],
		],
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
	"cislo_dokladu": ["cislo dokladu", "interni cislo", "interni doklad", "doklad"],
	"variabilni_symbol": ["variabilni symbol", "vs"],
	"odberatel_jmeno": ["nazev/jmeno", "nazev jmeno", "odberatel", "firma", "nazev", "jmeno"],
	"odberatel_ic": ["ic"],
	"odberatel_dic": ["dic / ic dph", "dic", "ic dph"],
	"datum_vystaveni": ["vystaveno", "datum vystaveni", "vystaveni"],
	"datum_splatnosti": ["splatnost"],
	"datum_duzp": ["duzp"],
	"celkova_cena": ["celkem s dph", "celkem s dani"],
	"zaklad_dane_21": ["celkem bez dph", "zaklad dane"],
	"vyse_dph_21": ["dph"],
	"psc": ["psc"],
	"mesto": ["mesto"],
}


class CSVProcessor:
	"""Procesor pro tabulkové soubory s daty faktur."""

	def __init__(self, config: Optional[AppConfig] = None) -> None:
		self.logger = logger
		self._config: Optional[AppConfig] = config
		self._field_specs: Dict[str, Dict[str, Any]] = FIELD_SPECS
		cfg = self._get_config()
		self._min_guess_conf = max(0.0, min(0.95, getattr(cfg, "csv_min_guess_conf", DEFAULT_MIN_GUESS_CONF)))
		self._pattern_sample_rows = max(5, min(100, getattr(cfg, "csv_pattern_sample_rows", DEFAULT_PATTERN_SAMPLE_ROWS)))
		self._mapping_debug_path = getattr(cfg, "csv_mapping_debug_path", None)
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
		# Detailní metadata mapování (confidence, zdroj, důvod)
		self._column_map_details: Dict[str, Dict[str, Any]] = {}

	def _get_config(self) -> AppConfig:
		return self._config or load_config()

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
			invoice_data = InvoiceData()

			# Základní informace
			invoice_data.cislo_dokladu = self._safe_get(row, "Číslo faktury")
			invoice_data.variabilni_symbol = self._safe_get(row, "Číslo Objednávky")

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
			invoice_data.datum_vystaveni = parse_invoice_date(self._safe_get(row, "Datum vystavení"))
			invoice_data.datum_duzp = parse_invoice_date(self._safe_get(row, "Datum objednávky"))
			invoice_data.datum_splatnosti = parse_invoice_date(self._safe_get(row, "Datum splatnosti"))

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
			date_payload, inferred_warnings = domysleni_chybejicich_datumu(date_payload)
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
		"""Dvoufázové mapování sloupců s confidence a auditními důvody."""
		cfg = self._get_config()
		min_guess_conf = max(0.0, min(0.95, getattr(cfg, "csv_min_guess_conf", self._min_guess_conf)))
		cols = list(df.columns)
		mapping: Dict[str, str] = {}
		self._column_map_details = {}
		used_headers: Set[str] = set()
		norm_cols = {c: self._normalize(c) for c in cols}
		candidates_by_target: Dict[str, List[Dict[str, Any]]] = {}

		def _clamp_conf(value: float) -> float:
			try:
				val = float(value)
			except Exception:
				return 0.0
			return max(0.0, min(0.99, val))

		def _decision_rank(decision: str) -> int:
			return {"certain": 2, "guess": 1, "reject": 0}.get(decision, 0)

		def _source_rank(source: str) -> int:
			return {
				"exact": 5,
				"pattern": 4,
				"heuristic": 3,
				"fallback": 2,
				"llm": 1,
			}.get(source, 0)

		def _is_sane_candidate(target: str, stats: Optional[Dict[str, float]]) -> Tuple[bool, str, float]:
			if not stats:
				return True, "", 0.0
			reasons = []
			penalty = 0.0
			null_ratio = stats.get("null_ratio", 0.0)
			if null_ratio > 0.65:
				penalty += 0.2
				reasons.append(f"mnoho prázdných {null_ratio:.2f}")
			if target in {"psc", "variabilni_symbol"} and stats.get("phone_ratio", 0.0) > 0.4:
				penalty += 0.25
				reasons.append("vypadá jako telefon")
			if target == "psc" and stats.get("long_ratio", 0.0) > 0.3:
				penalty += 0.2
				reasons.append("PSČ příliš dlouhé")
			if target == "variabilni_symbol" and stats.get("short_ratio", 0.0) > 0.4:
				penalty += 0.15
				reasons.append("VS příliš krátký")
			if target in {"psc"} and stats.get("psc", 0.0) < 0.3:
				penalty += 0.15
			if target in {"variabilni_symbol"} and stats.get("vs", 0.0) < 0.25:
				penalty += 0.15
			if target in {"dodavatel_dic", "odberatel_dic"} and stats.get("dic", 0.0) < 0.25:
				penalty += 0.25
			if target in {"dodavatel_ic", "odberatel_ic"} and stats.get("ic", 0.0) < 0.25:
				penalty += 0.2
			if target in {"celkova_cena", "zaklad_dane_0", "zaklad_dane_12", "zaklad_dane_21", "vyse_dph_12", "vyse_dph_21"} and stats.get("amount", 0.0) < 0.25:
				penalty += 0.25
				reasons.append("málo částkových hodnot")
			if target in {"datum_vystaveni", "datum_duzp", "datum_splatnosti"} and stats.get("date", 0.0) < 0.25:
				penalty += 0.25
				reasons.append("málo datumů")
			return penalty < 0.45, "; ".join(reasons), penalty

		def register_candidate(
			target: str,
			header: str,
			confidence: float,
			reason: str,
			source: str,
			decision: str,
			token_score: int = 0,
			type_score: int = 0,
			pattern_score: float = 0.0,
			stats: Optional[Dict[str, float]] = None,
		) -> None:
			conf = _clamp_conf(confidence)
			dec_flag = "certain" if (conf >= CONFIDENT_CONFIDENCE or decision == "certain") else "guess"
			ok, penalty_reason, penalty = _is_sane_candidate(target, stats)
			if penalty:
				conf = _clamp_conf(conf - penalty)
				if penalty_reason:
					reason = f"{reason}; penalizace: {penalty_reason}"
			if not ok:
				return
			candidate = {
				"target": target,
				"header": header,
				"confidence": conf,
				"reason": reason,
				"source": source,
				"decision": dec_flag,
				"token_score": token_score,
				"type_score": type_score,
				"pattern_score": pattern_score,
				"stats": stats or {},
				"ranking": (
					_decision_rank(dec_flag),
					_source_rank(source),
					round(pattern_score, 3),
					token_score,
					type_score,
					-len(str(header or "")),
					target,
				),
			}
			candidates_by_target.setdefault(target, []).append(candidate)

		# 1) Přímé shody (jisté)
		for target, expected_label in self._expected_headers_by_target.items():
			if expected_label in df.columns:
				register_candidate(
					target,
					expected_label,
					1.0,
					"Přesný název sloupce",
					"exact",
					"certain",
					token_score=3,
					type_score=2,
					pattern_score=1.0,
					stats=self._analyze_value_patterns(df[expected_label], self._pattern_sample_rows),
				)

		# 2) Heuristiky (silné/odhad)
		for target in self._expected_headers_by_target.keys():
			heuristic = self._heuristic_candidate(target, norm_cols, df)
			if heuristic:
				register_candidate(
					target,
					heuristic["header"],
					heuristic["confidence"],
					heuristic["reason"],
					heuristic["source"],
					heuristic["decision"],
					token_score=heuristic.get("token_score", 0),
					type_score=heuristic.get("type_score", 0),
					pattern_score=heuristic.get("pattern_score", 0.0),
					stats=heuristic.get("stats"),
				)

		existing_headers = set(mapping.values())

		# 3) LLM návrhy (vracejí jisté i odhadované)
		use_llm = bool(cfg.openai_api_key) and bool(getattr(cfg, 'csv_enable_llm_mapping', False))
		need_llm = use_llm and (len(mapping) < len(self._expected_headers_by_target))
		if need_llm:
			self.logger.info("Mapování sloupců (LLM) – model %s", cfg.openai_model or DEFAULT_OPENAI_MODEL)
			try:
				llm_map = self._ask_llm_for_mapping(df)
				llm_map = self._sanitize_llm_mapping(llm_map, df, reserved_headers=existing_headers)
				for target, info in llm_map.items():
					header = info.get("header")
					if not header:
						continue
					register_candidate(
						target,
						header,
						info.get("confidence", 0.0),
						info.get("reason") or "LLM návrh",
						info.get("source") or "llm",
						info.get("decision") or "guess",
						pattern_score=0.0,
						token_score=0,
						type_score=0,
					)
			except Exception as e:  # noqa: BLE001
				self.logger.warning("LLM mapování selhalo: %s", e)

		# 4) Fallback synonyma pro zbylé cílové klíče (slabší odhady)
		for target in self._expected_headers_by_target.keys():
			fallback = self._fallback_synonym_candidate(target, norm_cols, df)
			if fallback:
				register_candidate(
					target,
					fallback["header"],
					fallback["confidence"],
					fallback["reason"],
					fallback["source"],
					fallback["decision"],
					pattern_score=fallback.get("pattern_score", 0.0),
					token_score=fallback.get("token_score", 0),
					type_score=fallback.get("type_score", 0),
					stats=fallback.get("stats"),
				)

		def commit_candidate(candidate: Dict[str, Any]) -> bool:
			target = candidate["target"]
			header = candidate["header"]
			conf = candidate["confidence"]
			if not header or target in mapping or header in used_headers:
				return False
			if candidate["decision"] == "guess" and conf < min_guess_conf:
				return False
			mapping[target] = header
			self._column_map_details[target] = {
				"header": header,
				"confidence": _clamp_conf(conf),
				"reason": candidate["reason"],
				"source": candidate["source"],
				"decision": candidate["decision"],
			}
			used_headers.add(header)
			return True

		# Fáze 1: jisté kandidáty podle rankingu
		certain_candidates: List[Dict[str, Any]] = []
		for target, cands in candidates_by_target.items():
			certain_candidates.extend([c for c in cands if c["decision"] == "certain"])
		for cand in sorted(certain_candidates, key=lambda c: c["ranking"], reverse=True):
			commit_candidate(cand)

		# Fáze 2: nejlepší odhady bez kolizí, včetně second-best fallback
		for target in self._expected_headers_by_target.keys():
			if target in mapping:
				continue
			cands = sorted(candidates_by_target.get(target, []), key=lambda c: c["ranking"], reverse=True)
			for cand in cands:
				if cand["decision"] == "guess" and cand["confidence"] < min_guess_conf:
					continue
				if commit_candidate(cand):
					break

		# Doplň detaily pro nenamapované cíle (audit)
		for target in self._expected_headers_by_target.keys():
			if target not in self._column_map_details:
				cands = sorted(candidates_by_target.get(target, []), key=lambda c: c["ranking"], reverse=True)
				reason = "Nenašel se vhodný sloupec"
				if cands:
					top = cands[0]
					if top["confidence"] < min_guess_conf:
						reason = f"Kandidáti pod prahem {min_guess_conf:.2f}"
					elif top["header"] in used_headers:
						reason = f"Kolize hlavičky {top['header']}"
				self._column_map_details[target] = {
					"header": None,
					"confidence": 0.0,
					"reason": reason,
					"source": "reject",
					"decision": "reject",
				}

		summary = []
		for target in sorted(self._expected_headers_by_target.keys()):
			detail = self._column_map_details.get(target, {})
			status = detail.get("decision")
			status_label = {"certain": "jisté", "guess": "odhad", "reject": "zamítnuto"}.get(status, "neznámé")
			summary.append(
				f"{target}->{detail.get('header') or '-'} "
				f"({status_label}, conf={detail.get('confidence', 0):.2f}, zdroj={detail.get('source')}, důvod={detail.get('reason')})"
			)
		if summary:
			self.logger.info("Detaily mapování sloupců: %s", " | ".join(summary))
		self._export_mapping_debug(df, mapping)
		return mapping

	def _export_mapping_debug(self, df: pd.DataFrame, mapping: Dict[str, str]) -> None:
		"""Při zapnutém debug exportu uloží detail mapování do JSONu."""
		if not self._mapping_debug_path:
			return
		try:
			path = Path(self._mapping_debug_path)
			path.parent.mkdir(parents=True, exist_ok=True)
			payload = {
				"headers": [str(c) for c in df.columns],
				"mapping": mapping,
				"details": self._column_map_details,
			}
			path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
			self.logger.info("Mapping debug export uložen do %s", path)
		except Exception as exc:  # noqa: BLE001
			self.logger.warning("Mapping debug export selhal: %s", exc)

	def _normalize(self, s: str) -> str:
		val = str(s or "").replace("\ufeff", "").strip().strip('"').lower()
		val = ''.join(ch for ch in unicodedata.normalize('NFKD', val) if not unicodedata.combining(ch))
		val = re.sub(r"\s+", " ", val)
		return val

	def _confidence_from_scores(self, token_score: int, type_score: int) -> float:
		"""Odhad jistoty z heuristických skóre (deterministicky)."""
		conf = 0.35 + 0.2 * token_score + 0.15 * max(type_score, 0)
		return max(0.05, min(0.98, conf))

	def _analyze_value_patterns(self, series: Optional[pd.Series], limit: int = 20) -> Dict[str, float]:
		"""Vyhodnotí, jak moc hodnoty připomínají DIČ/IČ/PSČ/VS/datum/částku + základní statistiky."""
		if series is None:
			return {}
		total = 0
		hits = {"dic": 0, "ic": 0, "psc": 0, "vs": 0, "date": 0, "amount": 0, "phone_like": 0}
		non_empty = 0
		unique_values: Set[str] = set()
		lengths: List[int] = []
		numericish = 0
		for val in series.head(limit):
			total += 1
			if pd.isna(val):
				continue
			text = str(val).strip()
			if not text:
				continue
			non_empty += 1
			unique_values.add(text)
			lengths.append(len(text))
			clean = re.sub(r"[\\s-]", "", text.upper())
			if re.fullmatch(r"[A-Z]{2}[0-9]{6,12}", clean):
				hits["dic"] += 1
			if re.fullmatch(r"[0-9]{7,10}", clean):
				hits["ic"] += 1
			if re.fullmatch(r"[0-9]{3,6}", clean):
				hits["psc"] += 1
			if re.fullmatch(r"[0-9]{5,15}", clean):
				hits["vs"] += 1
			if re.fullmatch(r"[0-9]{9,12}", clean):
				hits["phone_like"] += 1
			try:
				if parse_invoice_date(text):
					hits["date"] += 1
			except Exception:
				pass
			if self._safe_float(text) is not None:
				hits["amount"] += 1
				numericish += 1
			elif text.isdigit():
				numericish += 1
		safe_total = max(1, total)
		stats: Dict[str, float] = {key: (hits[key] / max(1, non_empty)) for key in hits}
		stats["null_ratio"] = 1 - (non_empty / safe_total)
		stats["unique_ratio"] = (len(unique_values) / non_empty) if non_empty else 0.0
		stats["avg_len"] = (sum(lengths) / non_empty) if non_empty else 0.0
		stats["short_ratio"] = len([l for l in lengths if l <= 3]) / max(1, non_empty)
		stats["long_ratio"] = len([l for l in lengths if l >= 14]) / max(1, non_empty)
		stats["phone_ratio"] = stats.get("phone_like", 0.0)
		stats["numeric_ratio"] = numericish / max(1, non_empty)
		return stats

	def _value_pattern_hint(self, target: str, series: Optional[pd.Series]) -> Tuple[float, Optional[str]]:
		patterns = self._analyze_value_patterns(series, self._pattern_sample_rows)
		target_patterns: Dict[str, List[Tuple[str, str]]] = {
			"dodavatel_dic": [("dic", "DIČ")],
			"odberatel_dic": [("dic", "DIČ")],
			"dodavatel_ic": [("ic", "IČ")],
			"odberatel_ic": [("ic", "IČ")],
			"psc": [("psc", "PSČ")],
			"variabilni_symbol": [("vs", "VS")],
			"datum_vystaveni": [("date", "datum")],
			"datum_duzp": [("date", "datum")],
			"datum_splatnosti": [("date", "datum")],
			"zaklad_dane_0": [("amount", "částka")],
			"zaklad_dane_12": [("amount", "částka")],
			"zaklad_dane_21": [("amount", "částka")],
			"vyse_dph_12": [("amount", "částka")],
			"vyse_dph_21": [("amount", "částka")],
			"celkova_cena": [("amount", "částka")],
		}
		candidates = target_patterns.get(target, [])
		best_ratio = 0.0
		best_label: Optional[str] = None
		for key, label in candidates:
			ratio = patterns.get(key, 0.0)
			if ratio > best_ratio:
				best_ratio = ratio
				best_label = label
		return best_ratio, best_label

	def _heuristic_match(self, target: str, norm_cols: Dict[str, str], df: Optional[pd.DataFrame]) -> Optional[str]:
		candidate = self._heuristic_candidate(target, norm_cols, df)
		return candidate["header"] if candidate else None

	def _heuristic_candidate(self, target: str, norm_cols: Dict[str, str], df: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
		"""Pokusí se spárovat sloupec podle klíčových slov a typu hodnot."""
		spec = self._field_specs.get(target, {})
		patterns = spec.get("keywords", []) or []
		expected_type = spec.get("type")
		best_col: Optional[str] = None
		best_score: Optional[Tuple[int, int, int, int]] = None
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
			composite = (token_score + type_score, type_score, token_score, -len(norm))
			if best_score is None or composite > best_score:
				best_score = composite
				best_col = col
		if best_col is None:
			return None
		token_score = best_score[2] if best_score else 0
		type_score = best_score[1] if best_score else 0
		series = df[best_col] if df is not None else None
		stats = self._analyze_value_patterns(series, self._pattern_sample_rows)
		value_ratio, value_label = self._value_pattern_hint(target, series)

		strong_pattern_targets = {
			"dodavatel_dic",
			"odberatel_dic",
			"dodavatel_ic",
			"odberatel_ic",
			"psc",
			"variabilni_symbol",
		}
		source = "heuristic"
		if token_score == 0 and type_score <= 0:
			if value_ratio >= 0.55 and target in strong_pattern_targets:
				conf = min(0.99, 0.45 + 0.4 * value_ratio)
				reason = f"Datový vzor {value_label or 'data'} {value_ratio:.2f}"
				source = "pattern"
			else:
				return None
		else:
			conf = self._confidence_from_scores(token_score, type_score)
			reason = f"Heuristika: klíčová slova {token_score}, typ {type_score}"
			if value_ratio > 0:
				conf = min(0.99, conf + 0.25 * value_ratio)
				reason += f"; data vzor {value_label or 'data'} {value_ratio:.2f}"
		pattern_score = value_ratio
		return {
			"target": target,
			"header": best_col,
			"confidence": conf,
			"reason": reason,
			"source": source,
			"decision": decision,
			"token_score": token_score,
			"type_score": type_score,
			"pattern_score": pattern_score,
			"stats": stats,
		}

	def _fallback_synonym_match(self, target: str, norm_cols: Dict[str, str], used_headers: Set[str]) -> Optional[str]:
		patterns = FALLBACK_SYNONYMS.get(target, [])
		for col, norm in norm_cols.items():
			if col in used_headers:
				continue
			for pat in patterns:
				if pat in norm:
					return col
		return None

	def _fallback_synonym_candidate(self, target: str, norm_cols: Dict[str, str], df: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
		cand = self._fallback_synonym_match(target, norm_cols, set())
		if not cand:
			return None
		conf = 0.55
		reason = "Fallback synonymum"
		stats = None
		pattern_score = 0.0
		if df is not None and cand in df.columns:
			stats = self._analyze_value_patterns(df[cand], self._pattern_sample_rows)
			value_ratio, value_label = self._value_pattern_hint(target, df[cand])
			pattern_score = value_ratio
			if value_ratio > 0:
				conf = min(0.99, conf + 0.25 * value_ratio)
				reason += f"; data vzor {value_label or 'data'} {value_ratio:.2f}"
		decision = "certain" if conf >= CONFIDENT_CONFIDENCE else "guess"
		return {
			"target": target,
			"header": cand,
			"confidence": conf,
			"reason": reason,
			"source": "fallback",
			"decision": decision,
			"pattern_score": pattern_score,
			"token_score": 0,
			"type_score": 0,
			"stats": stats,
		}

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
			for val in values:
				text = str(val).strip()
				if not text:
					continue
				parsed = None
				try:
					parsed = parse_invoice_date(text)
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
		for val in values:
			text = str(val).strip()
			if self._safe_float(text) is not None:
				number_hits += 1
			try:
				if parse_invoice_date(text):
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
		priority = 1 if target in ("celkova_cena", "cislo_dokladu", "datum_vystaveni", "odberatel_jmeno") else 0
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
		llm_map: Dict[str, Any],
		df: pd.DataFrame,
		reserved_headers: Optional[Set[str]] = None,
	) -> Dict[str, Dict[str, Any]]:
		"""Validuje a zpřesní mapu z LLM: odstraní neznámé hlavičky, kolize a doplní confidence."""
		if not isinstance(llm_map, dict):
			return {}
		allowed_targets = set(self._expected_headers_by_target.keys())
		columns = [str(c) for c in df.columns]
		header_set = set(columns)
		used_headers: Set[str] = set(reserved_headers or set())
		normalized: Dict[str, Dict[str, Any]] = {}
		for target, raw_header in llm_map.items():
			if target not in allowed_targets:
				continue
			header_candidate = None
			confidence = 0.0
			reason = "LLM návrh"
			decision = "guess"
			if isinstance(raw_header, dict):
				header_candidate = (
					raw_header.get("header")
					or raw_header.get("column")
					or raw_header.get("name")
					or raw_header.get("value")
				)
				try:
					confidence = float(raw_header.get("confidence", 0.0))
				except Exception:
					confidence = 0.0
				reason = str(raw_header.get("reason") or reason)
				if raw_header.get("decision"):
					decision = str(raw_header.get("decision")).lower()
			else:
				header_candidate = raw_header
				reason = "LLM návrh (jednoduchý formát)"

			header = None
			if header_candidate and header_candidate in header_set:
				header = str(header_candidate)
			elif header_candidate:
				header = self._best_header_fuzzy(str(header_candidate), columns)
			if header in used_headers:
				reason = f"Kolize s již obsazenou hlavičkou {header}"
				header = None
			confidence = max(0.0, min(0.99, confidence if confidence else (0.65 if header else 0.0)))
			if str(decision).lower() == "certain":
				decision = "certain"
			decision_flag = "certain" if (confidence >= CONFIDENT_CONFIDENCE or decision == "certain") else "guess"
			normalized[target] = {
				"header": header,
				"confidence": confidence,
				"reason": reason,
				"source": "llm",
				"decision": decision_flag,
			}

		header_best: Dict[str, str] = {}
		for target, info in normalized.items():
			header = info.get("header")
			if not header:
				continue
			prev_target = header_best.get(header)
			if not prev_target or info.get("confidence", 0.0) > normalized[prev_target].get("confidence", 0.0):
				header_best[header] = target
		for target, info in normalized.items():
			header = info.get("header")
			if not header:
				continue
			if header_best.get(header) != target:
				info["reason"] = f"Kolize s cílem {header_best.get(header)}"
				info["header"] = None
					info["decision"] = "guess"
		return normalized

	def _ask_llm_for_mapping(self, df: pd.DataFrame) -> Dict[str, Any]:
		"""Zavolá OpenAI a požádá o mapování hlaviček na interní klíče.
		Vrací dict: interní_pole → {header, confidence, reason, decision}.
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
			properties[key] = {
				"type": "object",
				"additionalProperties": False,
				"properties": {
					"header": {
						"type": ["string", "null"],
						"enum": allowed_headers + [None],
						"description": "Přesná hlavička z tabulky nebo null, pokud žádná nedává smysl.",
					},
					"confidence": {
						"type": "number",
						"minimum": 0,
						"maximum": 1,
						"description": "0-1 jistota přiřazení (vyšší = jistější).",
					},
					"reason": {
						"type": "string",
						"description": "Krátký důvod proč byla hlavička vybrána.",
					},
						"decision": {
							"type": "string",
							"enum": ["certain", "guess"],
							"description": "certain = jasná shoda, guess = nejlepší odhad",
						},
					},
					"required": ["header", "confidence", "reason"],
				}
		response_format = {
			"type": "json_schema",
			"json_schema": {
				"name": "CsvColumnMappingRich",
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
					"Pro každý interní klíč vrať objekt {header, confidence, reason, decision}. decision='certain', pokud je shoda jasná; decision='guess' použij pro nejlepší odhad. "
					"Pravidla: (1) vybírej výhradně z dostupných hlaviček, nikdy nevytvářej nové; (2) stejnou hlavičku nepřiřazuj více polím – vyber to nejpravděpodobnější; "
					"(3) pokud si nejsi jistý, preferuj nejlepší odhad (confidence 0.35–0.7) místo \"nevím\"; null použij jen pokud opravdu není žádný rozumný kandidát; "
					"(4) respektuj datové typy (datum, číslo, text) a ignoruj popisné/poznámkové sloupce; "
					"(5) toleruj diakritiku, varianty názvů i překlepy a využij ukázkové hodnoty; "
					"(6) Dodavatel = supplier/vendor, odběratel = customer/client; interní čísla dokladu patří do pole 'cislo_dokladu'; "
					"(7) Nepřidávej žádné klíče ani komentáře.\n"
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
				request_kwargs["temperature"] = 0.0
				request_kwargs["top_p"] = 0.0
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


