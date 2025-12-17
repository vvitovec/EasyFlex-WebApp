import asyncio
import os
import re
import threading
import tkinter as tk
from datetime import datetime, date
from tkinter import filedialog, messagebox, ttk, simpledialog, scrolledtext
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
import logging

from .models import InvoiceData
from .abra import import_to_abra
from .config import (
	load_config,
	read_settings,
	write_settings,
	get_companies_from_settings,
	add_company_to_settings,
	get_doc_types,
	add_doc_type,
	get_log_messages,
)
from .date_helpers import DATE_FIELDS, domysleni_chybejicich_datumu, parse_invoice_date
from .invoice_warnings import clear_warning, get_warning, set_warning

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
	from .extractor import InvoiceExtractor
	from .csv_processor import CSVProcessor
	import pandas as pd



class InvoiceEditDialog:
	FLOAT_FIELDS = {
		"zaklad_dane",
		"zaklad_dane_0",
		"zaklad_dane_12",
		"zaklad_dane_21",
		"vyse_dph",
		"vyse_dph_12",
		"vyse_dph_21",
		"celkova_cena",
	}

	FIELD_SECTIONS = [
		("Obecné", [
			("cislo_dokladu", "Číslo dokladu"),
			("variabilni_symbol", "Variabilní symbol"),
			("dodavatel_jmeno", "Dodavatel – název"),
			("dodavatel_ic", "Dodavatel – IČ"),
			("dodavatel_dic", "Dodavatel – DIČ"),
			("odberatel_jmeno", "Odběratel – název"),
			("mena", "Měna"),
		]),
		("Datumy", [
			("datum_vystaveni", "Datum vystavení"),
			("datum_duzp", "Datum DUZP"),
			("datum_splatnosti", "Datum splatnosti"),
		]),
		("Částky", [
			("zaklad_dane", "Základ daně (celkem)"),
			("vyse_dph", "DPH celkem"),
			("celkova_cena", "Celkem k úhradě"),
			("zaklad_dane_0", "Základ daně 0 %"),
			("zaklad_dane_12", "Základ daně 12 %"),
			("zaklad_dane_21", "Základ daně 21 %"),
			("vyse_dph_12", "DPH 12 %"),
			("vyse_dph_21", "DPH 21 %"),
		]),
	]

	def __init__(self, parent: tk.Tk, invoice: InvoiceData) -> None:
		self.parent = parent
		self.invoice = invoice
		self.result: Optional[Dict[str, Any]] = None
		self._vars: Dict[str, tk.StringVar] = {}
		self.win = tk.Toplevel(parent)
		self.win.title("Upravit fakturu")
		self.win.transient(parent)
		self.win.grab_set()
		self.win.resizable(False, False)
		self._build()
		self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)
		self.win.focus()

	def _build(self) -> None:
		content = ttk.Frame(self.win)
		content.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
		for section_title, fields in self.FIELD_SECTIONS:
			frame = ttk.LabelFrame(content, text=section_title)
			frame.pack(fill=tk.X, expand=True, padx=5, pady=5)
			for idx, (field, label) in enumerate(fields):
				self._add_entry(frame, field, label, row=idx)
		btn_frame = ttk.Frame(self.win)
		btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
		btn_save = ttk.Button(btn_frame, text="Uložit", command=self._on_save)
		btn_save.pack(side=tk.RIGHT, padx=5)
		btn_cancel = ttk.Button(btn_frame, text="Zrušit", command=self._on_cancel)
		btn_cancel.pack(side=tk.RIGHT, padx=5)

	def _add_entry(self, parent: ttk.LabelFrame, field: str, label: str, row: int) -> None:
		lbl = ttk.Label(parent, text=label + ":")
		lbl.grid(row=row, column=0, sticky=tk.W, padx=(5, 10), pady=2)
		var = tk.StringVar(value=self._initial_value(field))
		self._vars[field] = var
		entry = ttk.Entry(parent, textvariable=var, width=30)
		entry.grid(row=row, column=1, sticky=tk.W, padx=(0, 5), pady=2)

	def _initial_value(self, field: str) -> str:
		value = getattr(self.invoice, field, None)
		if value is None:
			return ""
		if isinstance(value, float):
			return ("%f" % value).rstrip("0").rstrip(".") if value != int(value) else str(int(value))
		return str(value)

	def _on_save(self) -> None:
		collected = self._collect_values()
		if collected is None:
			return
		self.result = collected
		self.win.destroy()

	def _on_cancel(self) -> None:
		self.result = None
		self.win.destroy()

	def _collect_values(self) -> Optional[Dict[str, Any]]:
		updates: Dict[str, Any] = {}
		date_fields = set(DATE_FIELDS)
		for field, var in self._vars.items():
			raw = var.get().strip()
			if not raw:
				updates[field] = None
				continue
			if field in date_fields:
				parsed = parse_invoice_date(raw)
				updates[field] = parsed if parsed is not None else None
			elif field in self.FLOAT_FIELDS:
				try:
					updates[field] = self._parse_float(raw)
				except ValueError:
					updates[field] = None
			else:
				updates[field] = raw
		return updates

	@staticmethod
	def _parse_float(value: str) -> float:
		clean_value = re.sub(r"[\s\u00A0\u202F]", "", value)
		clean_value = clean_value.replace(',', '.')
		clean_value = re.sub(r"(?<=\d)\.(?=\d{3}(?:\D|$))", "", clean_value)
		if clean_value in {"", "-"}:
			raise ValueError("Empty numeric value")
		return float(clean_value)


class AppGUI:
	def __init__(self, root: tk.Tk, extractor: Optional["InvoiceExtractor"] = None) -> None:
		self.root = root
		self.extractor = extractor
		self._extractor_error: Optional[Exception] = None
		self._extractor_ready = threading.Event()
		if extractor is not None:
			self._extractor_ready.set()
		self._csv_processor: Optional["CSVProcessor"] = None
		self._pd: Optional[Any] = None
		self.root.title("EasyFlex by Viktor Vítovec")
		
		self.root.geometry("1000x450")

		self.progress_var = tk.DoubleVar(value=0.0)
		self.status_var = tk.StringVar(value="Připraven")
		self.results: List[Any] = []
		self.invoices: List[InvoiceData] = []  # for CSV-loaded invoices
		self.df: Optional["pd.DataFrame"] = None
		self.auto_import_var = tk.BooleanVar(value=False)  # Default disabled
		self._current_view: str = "results"
		self._current_source_label: str = ""
		
		# Track which rows are marked for import (True = import, False = skip)
		self.import_flags: List[bool] = []

		# Context for ABRA import comboboxes
		self.company_var = tk.StringVar(value="")
		self.direction_var = tk.StringVar(value="faktura-prijata")
		self.doc_type_var = tk.StringVar(value="")
		self._company_items: List[Tuple[str, str]] = []  # (code, name)
		self._doc_type_items: List[Dict[str, str]] = []  # {name, code}
		self._load_initial_context()

		# Pretty labels for tree columns (init before widgets)
		self._pretty_labels = {
			"importovat": "Import",
			"soubor": "Soubor",
			"cislo_dokladu": "Číslo dokladu",
			"variabilni_symbol": "Variabilní symbol",
			"dodavatel_jmeno": "Dodavatel – název",
			"dodavatel_adresa": "Dodavatel – adresa",
			"dodavatel_stat": "Dodavatel – stát",
			"dodavatel_ic": "Dodavatel – IČ",
			"dodavatel_dic": "Dodavatel – DIČ",
			"odberatel_jmeno": "Odběratel – název",
			"odberatel_adresa": "Odběratel – adresa",
			"odberatel_stat": "Odběratel – stát",
			"odberatel_ic": "Odběratel – IČ",
			"odberatel_dic": "Odběratel – DIČ",
			"datum_vystaveni": "Datum vystavení",
			"datum_duzp": "Datum DUZP",
			"datum_splatnosti": "Datum splatnosti",
			"zaklad_dane_0": "Základ daně 0 %",
			"zaklad_dane_12": "Základ daně 12 %",
			"zaklad_dane_21": "Základ daně 21 %",
			"vyse_dph_12": "DPH 12 %",
			"vyse_dph_21": "DPH 21 %",
			"celkova_cena": "Celkem k úhradě",
			"upozorneni": "Upozornění",
		}

		# Sorting metadata for Treeview columns
		self._column_base_labels: Dict[str, str] = {}
		self._column_heading_anchor: Dict[str, str] = {}
		self._sort_directions: Dict[str, bool] = {}
		self._sorted_column: Optional[str] = None
		self._numeric_columns = {
			"zaklad_dane_0",
			"zaklad_dane_12",
			"zaklad_dane_21",
			"vyse_dph_12",
			"vyse_dph_21",
			"celkova_cena",
		}
		self._date_columns = set(DATE_FIELDS)

		self._build_widgets()

	def set_extractor(self, extractor: "InvoiceExtractor") -> None:
		"""Attach a ready extractor instance from background initialization."""
		self.extractor = extractor
		self._extractor_error = None
		self._extractor_ready.set()
		self.root.after(0, self._on_extractor_ready_ui)

	def notify_extractor_error(self, exc: Exception) -> None:
		"""Allow background initialization to report a fatal error."""
		self.extractor = None
		self._extractor_error = exc
		self._extractor_ready.set()
		self.root.after(0, lambda: self._show_startup_error(str(exc)))

	def _on_extractor_ready_ui(self) -> None:
		cfg = load_config()
		if cfg.openai_api_key:
			self.btn_open_pdf.configure(state=tk.NORMAL)
			self.btn_open_dir.configure(state=tk.NORMAL)
		self.status_var.set("Nástroje připraveny")

	def _show_startup_error(self, message: str) -> None:
		self.status_var.set("Start selhal")
		self.btn_open_pdf.configure(state=tk.DISABLED)
		self.btn_open_dir.configure(state=tk.DISABLED)
		messagebox.showerror("EasyFlex", f"Inicializace nástrojů selhala: {message}")

	def _ensure_extractor_ready(self) -> bool:
		if self._extractor_error is not None:
			messagebox.showerror("EasyFlex", f"Extractor není dostupný: {self._extractor_error}")
			return False
		if not self._extractor_ready.is_set() or self.extractor is None:
			messagebox.showinfo("EasyFlex", "Aplikace stále načítá potřebné moduly. Zkuste to prosím znovu za okamžik.")
			return False
		return True

	def _get_csv_processor(self) -> "CSVProcessor":
		if self._csv_processor is None:
			from .csv_processor import CSVProcessor  # Lazy import to speed up start
			self._csv_processor = CSVProcessor()
		return self._csv_processor

	def _ensure_pandas(self):
		if self._pd is None:
			import pandas as pd  # Lazy import to defer heavy module loading
			self._pd = pd
		return self._pd

	def _build_widgets(self) -> None:
		frm_top = ttk.Frame(self.root)
		frm_top.pack(fill=tk.X, padx=10, pady=10)

		self.btn_open_pdf = ttk.Button(frm_top, text="Vybrat PDF", command=self._choose_pdf)
		self.btn_open_pdf.pack(side=tk.LEFT, padx=5)

		self.btn_open_dir = ttk.Button(frm_top, text="Vybrat složku", command=self._choose_folder)
		self.btn_open_dir.pack(side=tk.LEFT, padx=5)

		btn_open_table = ttk.Button(frm_top, text="Vybrat tabulku", command=self._choose_table)
		btn_open_table.pack(side=tk.LEFT, padx=5)

		btn_export = ttk.Button(frm_top, text="Exportovat CSV", command=self._export_csv)
		btn_export.pack(side=tk.LEFT, padx=5)

		btn_clear = ttk.Button(frm_top, text="Vymazat", command=self._clear)
		btn_clear.pack(side=tk.LEFT, padx=5)

		btn_edit = ttk.Button(frm_top, text="Upravit fakturu", command=self._edit_selected_invoice)
		btn_edit.pack(side=tk.LEFT, padx=5)

		self.btn_settings = ttk.Button(frm_top, text="Nastavení", command=self._open_settings)
		self.btn_settings.pack(side=tk.RIGHT, padx=5)

		# Manual import button
		self.btn_import_abra = ttk.Button(frm_top, text="Importovat do ABRA", command=self._manual_import_to_abra)
		self.btn_import_abra.pack(side=tk.LEFT, padx=5)

		# Auto-import checkbox přesunut do Nastavení → Extrakce

		# ABRA context controls frame
		frm_ctx = ttk.Frame(self.root)
		frm_ctx.pack(fill=tk.X, padx=10, pady=5)

		# Company combobox
		lbl_company = ttk.Label(frm_ctx, text="Firma:")
		lbl_company.pack(side=tk.LEFT, padx=(0, 5))
		self.cbo_company = ttk.Combobox(frm_ctx, textvariable=self.company_var, state="readonly")
		self.cbo_company.pack(side=tk.LEFT, padx=5)
		self.cbo_company.bind("<<ComboboxSelected>>", lambda e: self._on_company_changed())

		# Direction combobox
		lbl_direction = ttk.Label(frm_ctx, text="Směr:")
		lbl_direction.pack(side=tk.LEFT, padx=(15, 5))
		self.cbo_direction = ttk.Combobox(frm_ctx, textvariable=self.direction_var, state="readonly",
			values=("faktura-vydana", "faktura-prijata"))
		self.cbo_direction.pack(side=tk.LEFT, padx=5)
		self.cbo_direction.bind("<<ComboboxSelected>>", lambda e: self._on_direction_changed())

		# Doc type combobox
		lbl_doc_type = ttk.Label(frm_ctx, text="Typ dokladu:")
		lbl_doc_type.pack(side=tk.LEFT, padx=(15, 5))
		self.cbo_doc_type = ttk.Combobox(frm_ctx, textvariable=self.doc_type_var, state="readonly")
		self.cbo_doc_type.pack(side=tk.LEFT, padx=5)
		self.cbo_doc_type.bind("<<ComboboxSelected>>", lambda e: self._on_doc_type_selected())
		# Update items on dropdown open
		self.cbo_doc_type.configure(postcommand=self._refresh_doc_type_values)

		self._refresh_company_values()
		self._refresh_doc_type_values()
		# Disable extraction buttons if OpenAI API key is missing
		cfg = load_config()
		if not cfg.openai_api_key:
			self.btn_open_pdf.configure(state=tk.DISABLED)
			self.btn_open_dir.configure(state=tk.DISABLED)
		if not self._extractor_ready.is_set():
			self.btn_open_pdf.configure(state=tk.DISABLED)
			self.btn_open_dir.configure(state=tk.DISABLED)

		progress = ttk.Progressbar(self.root, variable=self.progress_var, maximum=100)
		progress.pack(fill=tk.X, padx=10, pady=5)

		lbl_status = ttk.Label(self.root, textvariable=self.status_var)
		lbl_status.pack(fill=tk.X, padx=10, pady=5)

		# Container for tree with scrollbars
		tree_frame = ttk.Frame(self.root)
		tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
		tree_frame.grid_columnconfigure(0, weight=1)
		tree_frame.grid_rowconfigure(0, weight=1)

		# Improve heading visuals: bold font and extra padding
		style = ttk.Style(self.root)
		try:
			style.configure("Treeview.Heading", font=("TkDefaultFont", 10, "bold"), padding=(8, 6))
		except Exception:
			# Fallback without font tuple issues on some platforms
			style.configure("Treeview.Heading", padding=(8, 6))

		self.tree = ttk.Treeview(
			tree_frame,
			columns=(
				"importovat",
				"upozorneni",
				"soubor",
				"cislo_dokladu",
				"variabilni_symbol",
				"dodavatel_jmeno",
				"dodavatel_adresa",
				"dodavatel_stat",
				"dodavatel_ic",
				"dodavatel_dic",
				"odberatel_jmeno",
				"odberatel_adresa",
				"odberatel_stat",
				"odberatel_ic",
				"odberatel_dic",
				"datum_vystaveni",
				"datum_duzp",
				"datum_splatnosti",
				"zaklad_dane_0",
				"zaklad_dane_12",
				"zaklad_dane_21",
				"vyse_dph_12",
				"vyse_dph_21",
				"celkova_cena",
			),
			show="headings",
			height=16,
		)

		# Scrollbars (horizontal + vertical)
		self._tree_vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
		self._tree_hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
		self.tree.configure(yscrollcommand=self._tree_vsb.set, xscrollcommand=self._tree_hsb.set)

		self.tree.grid(row=0, column=0, sticky="nsew")
		self._tree_vsb.grid(row=0, column=1, sticky="ns")
		self._tree_hsb.grid(row=1, column=0, sticky="ew")

		# Column widths tuned for readability; left-align text
		_column_widths = {
			"importovat": 80,
			"upozorneni": 160,
			"soubor": 220,
			"cislo_dokladu": 140,
			"variabilni_symbol": 140,
			"dodavatel_jmeno": 220,
			"dodavatel_adresa": 300,
			"dodavatel_stat": 90,
			"dodavatel_ic": 120,
			"dodavatel_dic": 140,
			"odberatel_jmeno": 220,
			"odberatel_adresa": 300,
			"odberatel_stat": 90,
			"odberatel_ic": 120,
			"odberatel_dic": 140,
			"datum_vystaveni": 130,
			"datum_duzp": 130,
			"datum_splatnosti": 130,
			"zaklad_dane_0": 140,
			"zaklad_dane_12": 160,
			"zaklad_dane_21": 160,
			"vyse_dph_12": 120,
			"vyse_dph_21": 120,
			"celkova_cena": 140,
		}

		for col in self.tree["columns"]:
			label = getattr(self, "_pretty_labels", {}).get(col, col)
			anchor = tk.CENTER if col == "importovat" else tk.W
			self._column_base_labels[col] = label
			self._column_heading_anchor[col] = anchor
			self.tree.heading(col, text=label, anchor=anchor, command=lambda c=col: self._on_column_sort(c))
			self.tree.column(col, width=_column_widths.get(col, 140), anchor=tk.W, stretch=False)

		self.tree.bind('<Double-1>', self._on_cell_double_click)
		self.tree.bind('<Button-1>', self._on_tree_click)
		self._setup_tree_context_menu()

	def _setup_tree_context_menu(self) -> None:
		"""Bind context menu events in a cross-platform friendly way."""
		menu_events = ['<Button-3>']  # Standard right-click on Windows/Linux
		try:
			if self.root.tk.call('tk', 'windowingsystem') == 'aqua':
				menu_events.extend(['<Button-2>', '<Control-Button-1>'])  # macOS secondary click variants
		except tk.TclError:
			pass
		for sequence in menu_events:
			self.tree.bind(sequence, self._on_right_click)

	def _on_column_sort(self, column: str) -> None:
		if self.df is None or column not in self.tree["columns"]:
			return
		if column not in self.df.columns:
			return
		children = list(self.tree.get_children())
		selected_indices = [children.index(item_id) for item_id in self.tree.selection() if item_id in children]
		ascending = True
		if self._sorted_column == column:
			ascending = not self._sort_directions.get(column, True)
		self._perform_sort(column, ascending, selected_indices)

	def _perform_sort(self, column: str, ascending: bool, selected_indices: Optional[List[int]] = None) -> None:
		if self.df is None or column not in self.df.columns:
			return
		order = self._compute_sort_order(column, ascending)
		if order is None:
			return
		self._sort_directions[column] = ascending
		self._sorted_column = column
		self._apply_sorted_order(order, selected_indices)
		self._apply_heading_labels(column, ascending)

	def _compute_sort_order(self, column: str, ascending: bool) -> Optional[List[int]]:
		if self.df is None or column not in self.df.columns:
			return None
		pd_module = self._ensure_pandas()
		present, missing = self._prepare_sort_values(column, pd_module)
		if not present and not missing:
			return list(range(len(self.df)))
		present.sort(key=lambda item: item[0])
		if not ascending:
			present.reverse()
		order = [idx for _, idx in present]
		order.extend(missing)
		return order

	def _prepare_sort_values(self, column: str, pd_module) -> Tuple[List[Tuple[Tuple[int, Any], int]], List[int]]:
		present: List[Tuple[Tuple[int, Any], int]] = []
		missing: List[int] = []
		if self.df is None:
			return present, missing
		for idx in range(len(self.df)):
			raw = self.df.iloc[idx][column]
			if self._is_missing_value(raw, pd_module):
				missing.append(idx)
				continue
			present.append((self._build_sort_key(column, raw), idx))
		return present, missing

	def _is_missing_value(self, value: Any, pd_module) -> bool:
		if value is None:
			return True
		if isinstance(value, str) and value.strip() == "":
			return True
		try:
			if pd_module.isna(value):  # type: ignore[attr-defined]
				return True
		except Exception:
			return False
		return False

	def _build_sort_key(self, column: str, value: Any) -> Tuple[int, Any]:
		if column == "importovat":
			return (0, 0 if self._is_marked_for_import(value) else 1)
		if isinstance(value, (datetime, date)):
			iso_value = value.strftime("%Y-%m-%d")
			if column in self._date_columns:
				return (0, iso_value)
			return (1, self._natural_sort_key(iso_value))
		if column in self._date_columns:
			normalized = parse_invoice_date(value)
			if normalized is not None:
				return (0, normalized)
			return (1, self._natural_sort_key(str(value)))
		if column in self._numeric_columns:
			number = self._coerce_to_float(value)
			if number is not None:
				return (0, number)
			return (1, self._natural_sort_key(str(value)))
		if isinstance(value, (int, float)):
			return (0, float(value))
		return (1, self._natural_sort_key(str(value)))

	def _is_marked_for_import(self, value: Any) -> bool:
		if isinstance(value, bool):
			return value
		if isinstance(value, str):
			return value.strip() in {"✓", "1", "true", "True"}
		return False

	def _coerce_to_float(self, value: Any) -> Optional[float]:
		if isinstance(value, (int, float)):
			return float(value)
		if isinstance(value, str):
			clean_value = re.sub(r"[\s\u00A0\u202F]", "", value)
			clean_value = clean_value.replace(',', '.')
			clean_value = re.sub(r"(?<=\d)\.(?=\d{3}(?:\D|$))", "", clean_value)
			if clean_value in {"", "-"}:
				return None
			try:
				return float(clean_value)
			except ValueError:
				return None
		return None

	def _natural_sort_key(self, text: str) -> Tuple[Any, ...]:
		parts = re.split(r"(\d+)", text)
		key_parts: List[Any] = []
		for part in parts:
			if not part:
				continue
			if part.isdigit():
				key_parts.append(int(part))
			else:
				key_parts.append(part.casefold())
		return tuple(key_parts) if key_parts else (text.casefold(),)

	def _apply_sorted_order(self, order: List[int], selected_indices: Optional[List[int]] = None) -> None:
		if self.df is None:
			return
		self.df = self.df.iloc[order].reset_index(drop=True)
		if len(self.import_flags) == len(order):
			self.import_flags = [self.import_flags[i] for i in order]
		else:
			new_flags = []
			for i in order:
				if 0 <= i < len(self.import_flags):
					new_flags.append(self.import_flags[i])
				else:
					new_flags.append(True)
			self.import_flags = new_flags
		if "importovat" in self.df.columns:
			self.df["importovat"] = ["✓" if flag else "✗" for flag in self.import_flags]
		if self._current_view == "results" and len(self.results) == len(order):
			self.results = [self.results[i] for i in order]
		elif self._current_view == "invoices" and len(self.invoices) == len(order):
			self.invoices = [self.invoices[i] for i in order]
		self._refresh_tree_from_df()
		if selected_indices:
			self._restore_selection(selected_indices, order)

	def _refresh_tree_from_df(self) -> None:
		if self.df is None:
			return
		pd_module = self._ensure_pandas()
		columns = list(self.tree["columns"])
		self.tree.delete(*self.tree.get_children())
		for idx in range(len(self.df)):
			row = self.df.iloc[idx]
			values: List[Any] = []
			for col in columns:
				value = row[col] if col in row else ""
				try:
					if pd_module.isna(value):  # type: ignore[attr-defined]
						value = ""
				except Exception:
					pass
				if value is None:
					value = ""
				values.append(value)
			item_id = self.tree.insert('', tk.END, values=values)
			if idx < len(self.import_flags):
				self.tree.set(item_id, "importovat", "✓" if self.import_flags[idx] else "✗")

	def _restore_selection(self, old_indices: List[int], order: List[int]) -> None:
		mapping = {old_idx: new_pos for new_pos, old_idx in enumerate(order)}
		children = list(self.tree.get_children())
		new_items: List[str] = []
		for old_idx in old_indices:
			new_pos = mapping.get(old_idx)
			if new_pos is None or new_pos >= len(children):
				continue
			new_items.append(children[new_pos])
		if new_items:
			self.tree.selection_set(new_items)
			self.tree.focus(new_items[0])

	def _apply_heading_labels(self, sorted_column: Optional[str] = None, ascending: bool = True) -> None:
		arrow = "▲" if ascending else "▼"
		for col in self.tree["columns"]:
			base = self._column_base_labels.get(col, col)
			text = base
			if sorted_column and col == sorted_column:
				text = f"{base} {arrow}"
			anchor = self._column_heading_anchor.get(col, tk.W)
			self.tree.heading(col, text=text, anchor=anchor, command=lambda c=col: self._on_column_sort(c))

	def _reset_sort_state(self) -> None:
		self._sorted_column = None
		self._sort_directions.clear()
		self._apply_heading_labels()

	def _resort_after_data_change(self) -> None:
		if self._sorted_column is None:
			return
		ascending = self._sort_directions.get(self._sorted_column, True)
		children = list(self.tree.get_children())
		selected_indices = [children.index(item_id) for item_id in self.tree.selection() if item_id in children]
		self._perform_sort(self._sorted_column, ascending, selected_indices)

	def _row_to_invoice_dict(self, row_index: int) -> Dict[str, object]:
		"""Build invoice dict from the edited table row in self.df.

		Drops UI-only columns and converts NaN to None.
		"""
		pd = self._ensure_pandas()
		data: Dict[str, object] = {}
		if self.df is None or row_index < 0 or row_index >= len(self.df):
			return data
		row = self.df.iloc[row_index]
		for key, value in row.items():
			if key in ("importovat", "soubor"):
				continue
			# Replace pandas NaN with None for JSON compatibility
			try:
				if pd.isna(value):  # type: ignore[attr-defined]
					stored = None
				else:
					stored = value
			except Exception:
				stored = value
			if key == "upozorneni" and isinstance(stored, str) and stored.strip().lower() == "bez problému":
				stored = None
			data[key] = stored
		preview = {k: data.get(k) for k in ("cislo_dokladu", "variabilni_symbol", "odberatel_jmeno")}
		logger.info("_row_to_invoice_dict[%s] → %s", row_index, preview)
		return data

	def _has_minimal_invoice_data(self, inv: Dict[str, object]) -> bool:
		"""Minimal sanity check to avoid importing empty rows."""
		return bool(inv.get("cislo_dokladu") or inv.get("variabilni_symbol") or inv.get("odberatel_jmeno"))

	def _load_initial_context(self) -> None:
		"""Load initial combobox context from settings, with defaults."""
		cfg = load_config()
		# Companies
		self._company_items = get_companies_from_settings()
		# Select current company from config if available
		if cfg.abra_company:
			for code, name in self._company_items:
				if code == cfg.abra_company:
					self.company_var.set(self._format_company_display(code, name))
					break
		# Direction
		self.direction_var.set(cfg.abra_doc_endpoint or "faktura-prijata")
		# Doc type selection by code
		if cfg.abra_doc_type_code:
			for it in get_doc_types(cfg.abra_company or "", cfg.abra_doc_endpoint or "faktura-prijata"):
				if it.get("code") == cfg.abra_doc_type_code:
					self.doc_type_var.set(self._format_doc_type_display(it["name"], it["code"]))
					break

	def _format_company_display(self, code: str, name: str) -> str:
		return f"{name} ({code})"

	def _parse_company_display(self, display: str) -> Tuple[str, str]:
		# Expect format "name (code)"; fallback to code=display
		if display.endswith(")") and "(" in display:
			name = display[: display.rfind("(")].strip()
			code = display[display.rfind("(") + 1 : -1].strip()
			return code, name
		return display, display

	def _format_doc_type_display(self, name: str, code: str) -> str:
		return f"{name} ({code})"

	def _parse_doc_type_display(self, display: str) -> Tuple[str, str]:
		# Returns (name, code)
		if display.endswith(")") and "(" in display:
			name = display[: display.rfind("(")].strip()
			code = display[display.rfind("(") + 1 : -1].strip()
			return name, code
		return display, display

	def _refresh_company_values(self) -> None:
		items = [("__NEW__", "Nová firma…")] + self._company_items
		displays = [name for _, name in [(code, label) for code, label in [(it[0], self._format_company_display(it[0], it[1])) for it in items]]]
		# The line above was too complex; build step by step for clarity
		displays = [
			"Nová firma…"
		]
		for code, name in self._company_items:
			displays.append(self._format_company_display(code, name))
		self.cbo_company.configure(values=displays)
		# If nothing selected, select first saved company if present
		if not self.company_var.get():
			if len(displays) > 1:
				self.company_var.set(displays[1])
			else:
				self.company_var.set("Nová firma…")

	def _refresh_doc_type_values(self) -> None:
		code, _ = self._get_selected_company_code_and_name()
		direction = self.direction_var.get() or "faktura-prijata"
		items = get_doc_types(code or "", direction)
		self._doc_type_items = items
		displays = ["Nový typ…"] + [self._format_doc_type_display(it["name"], it["code"]) for it in items]
		self.cbo_doc_type.configure(values=displays)
		if not self.doc_type_var.get():
			self.doc_type_var.set(displays[0])

	def _get_selected_company_code_and_name(self) -> Tuple[Optional[str], Optional[str]]:
		display = self.company_var.get()
		if not display or display == "Nová firma…":
			return None, None
		code, name = self._parse_company_display(display)
		return code, name

	def _on_company_changed(self) -> None:
		display = self.company_var.get()
		if display == "Nová firma…":
			self._prompt_new_company()
			return
		# Persist selection for ABRA company
		code, _ = self._get_selected_company_code_and_name()
		self._persist_abra_context(company=code)
		self._refresh_doc_type_values()

	def _on_direction_changed(self) -> None:
		# Persist selection for ABRA endpoint
		endpoint = self.direction_var.get() or "faktura-prijata"
		self._persist_abra_context(doc_endpoint=endpoint)
		self._refresh_doc_type_values()

	def _on_doc_type_selected(self) -> None:
		display = self.doc_type_var.get()
		if display == "Nový typ…":
			self._prompt_new_doc_type()
			return
		# Persist selection for ABRA doc type code
		_, code = self._parse_doc_type_display(display)
		if code:
			self._persist_abra_context(doc_type_code=code)

	def _prompt_new_company(self) -> None:
		name = simpledialog.askstring("Nová firma", "Zadejte název firmy:", parent=self.root)
		if not name:
			return
		code = simpledialog.askstring("Kód firmy", "Zadejte kód firmy (prefix pro URL v ABRA):", parent=self.root)
		if not code:
			return
		add_company_to_settings(code.strip(), name.strip())
		self._company_items = get_companies_from_settings()
		self._refresh_company_values()
		self.company_var.set(self._format_company_display(code.strip(), name.strip()))
		self._persist_abra_context(company=code.strip())
		self._refresh_doc_type_values()

	def _prompt_new_doc_type(self) -> None:
		code, _name = self._get_selected_company_code_and_name()
		if not code:
			messagebox.showinfo("Typ dokladu", "Nejprve vyberte firmu.")
			return
		direction = self.direction_var.get() or "faktura-prijata"
		name = simpledialog.askstring("Nový typ", "Zadejte název typu dokladu:", parent=self.root)
		if not name:
			return
		code_val = simpledialog.askstring("Kód typu", "Zadejte kód typu (např. FAKTP):", parent=self.root)
		if not code_val:
			return
		add_doc_type(code, direction, name.strip(), code_val.strip())
		self._refresh_doc_type_values()
		self.doc_type_var.set(self._format_doc_type_display(name.strip(), code_val.strip()))
		self._persist_abra_context(doc_type_code=code_val.strip())

	def _persist_abra_context(self, company: Optional[str] = None, doc_endpoint: Optional[str] = None, doc_type_code: Optional[str] = None) -> None:
		cp = read_settings()
		if not cp.has_section("abra"):
			cp.add_section("abra")
		if company is not None:
			cp.set("abra", "company", company)
		if doc_endpoint is not None:
			cp.set("abra", "doc_endpoint", doc_endpoint)
		if doc_type_code is not None:
			cp.set("abra", "doc_type_code", doc_type_code)
		write_settings(cp)

	def _open_settings(self) -> None:
		SettingsDialog(self.root, on_saved=self._on_settings_saved, auto_import_var=self.auto_import_var)

	def _on_settings_saved(self) -> None:
		# Reload local context in case ABRA values changed
		self._load_initial_context()
		self._refresh_company_values()
		self._refresh_doc_type_values()
		new_cfg = load_config(force_reload=True)
		if self._extractor_ready.is_set() and self.extractor is not None:
			self.extractor.update_config(new_cfg)
			self._on_extractor_ready_ui()

	def _set_progress(self, current: int, total: int, last_file: str) -> None:
		pct = (current / max(1, total)) * 100.0
		self.progress_var.set(pct)
		self.status_var.set(f"Zpracováno {current}/{total}: {os.path.basename(last_file)}")
		self.root.update_idletasks()

	def _fill_table(self) -> None:
		self.tree.delete(*self.tree.get_children())
		self._reset_sort_state()
		rows = []
		
		# Initialize import flags for all results (default: True = import)
		self.import_flags = [True] * len(self.results)
		self._current_view = "results"
		self._current_source_label = ""

		for i, res in enumerate(self.results):
			row = {
				"importovat": "✓" if self.import_flags[i] else "✗",
				"soubor": os.path.basename(res.file_path),
			}
			if res.data:
				row.update(res.data.model_dump())
			row["upozorneni"] = " | ".join(res.warnings) if res.warnings else "bez problému"
			rows.append(row)

		pd = self._ensure_pandas()
		self.df = pd.DataFrame(rows)
		for i, row in enumerate(rows):
			values = [row.get(col, "") for col in self.tree["columns"]]
			item_id = self.tree.insert('', tk.END, values=values)
			# Store the result index in the item for later reference
			self.tree.set(item_id, "importovat", "✓" if self.import_flags[i] else "✗")

	def _build_invoice_rows(self, invoices: List[InvoiceData], src_label: str) -> List[Dict[str, Any]]:
		rows: List[Dict[str, Any]] = []
		for i, inv in enumerate(invoices):
			flag = True
			if 0 <= i < len(self.import_flags):
				flag = self.import_flags[i]
			row = {
				"importovat": "✓" if flag else "✗",
				"soubor": src_label,
			}
			if inv:
				row.update(inv.model_dump())
				extra_warning = get_warning(inv)
				if extra_warning:
					row["upozorneni"] = extra_warning
			row["upozorneni"] = row.get("upozorneni") or "bez problému"
			rows.append(row)
		return rows

	def _fill_table_from_invoices(self, invoices: List[InvoiceData], src_label: str, prebuilt_rows: Optional[List[Dict[str, Any]]] = None) -> None:
		self.tree.delete(*self.tree.get_children())
		self._reset_sort_state()
		if len(self.import_flags) != len(invoices):
			self.import_flags = [True] * len(invoices)
		self._current_view = "invoices"
		self._current_source_label = src_label
		rows = prebuilt_rows or self._build_invoice_rows(invoices, src_label)
		pd = self._ensure_pandas()
		self.df = pd.DataFrame(rows)
		for i, row in enumerate(rows):
			values = [row.get(col, "") for col in self.tree["columns"]]
			item_id = self.tree.insert('', tk.END, values=values)
			flag = True if i >= len(self.import_flags) else self.import_flags[i]
			self.tree.set(item_id, "importovat", "✓" if flag else "✗")

	def _edit_selected_invoice(self) -> None:
		selection = self.tree.selection()
		if not selection:
			messagebox.showinfo("EasyFlex", "Vyberte prosím řádek k úpravě.")
			return
		children = list(self.tree.get_children())
		try:
			index = children.index(selection[0])
		except ValueError:
			messagebox.showerror("EasyFlex", "Vybraný řádek se nepodařilo najít.")
			return
		if self._current_view == "results":
			if index >= len(self.results):
				messagebox.showerror("EasyFlex", "Řádek je mimo rozsah výsledků.")
				return
			result = self.results[index]
			if result.data is None:
				messagebox.showinfo("EasyFlex", "Tento záznam neobsahuje žádná data k úpravě.")
				return
			dialog = InvoiceEditDialog(self.root, result.data)
			self.root.wait_window(dialog.win)
			if dialog.result is None:
				return
			self._apply_invoice_updates(result.data, dialog.result)
			new_warnings = self._recompute_invoice_dates(result.data)
			self._update_result_warnings(result, new_warnings)
			self._update_tree_row(index)
		elif self._current_view == "invoices":
			if index >= len(self.invoices):
				messagebox.showerror("EasyFlex", "Řádek je mimo rozsah načtených faktur.")
				return
			invoice = self.invoices[index]
			dialog = InvoiceEditDialog(self.root, invoice)
			self.root.wait_window(dialog.win)
			if dialog.result is None:
				return
			self._apply_invoice_updates(invoice, dialog.result)
			new_warnings = self._recompute_invoice_dates(invoice)
			if new_warnings:
				set_warning(invoice, " | ".join(new_warnings))
			else:
				clear_warning(invoice)
			self._update_tree_row(index)
		else:
			messagebox.showinfo("EasyFlex", "Není co upravovat v aktuálním zobrazení.")

	def _apply_invoice_updates(self, invoice: InvoiceData, updates: Dict[str, Any]) -> None:
		for field, value in updates.items():
			setattr(invoice, field, value)

	def _recompute_invoice_dates(self, invoice: InvoiceData) -> List[str]:
		payload = {
			"datum_vystaveni": getattr(invoice, "datum_vystaveni", None),
			"datum_splatnosti": getattr(invoice, "datum_splatnosti", None),
			"datum_duzp": getattr(invoice, "datum_duzp", None),
		}
		payload, inferred_warnings = domysleni_chybejicich_datumu(payload)
		invoice.datum_vystaveni = payload.get("datum_vystaveni")
		invoice.datum_splatnosti = payload.get("datum_splatnosti")
		invoice.datum_duzp = payload.get("datum_duzp")
		return inferred_warnings

	def _update_result_warnings(self, result: "ExtractResult", inferred_warnings: List[str]) -> None:
		def _is_inference_warning(text: str) -> bool:
			lower = text.lower()
			return "doplněno" in lower or "dopočítáno" in lower

		remaining = [w for w in result.warnings if not _is_inference_warning(w)]
		for warning in inferred_warnings:
			if warning not in remaining:
				remaining.append(warning)
		result.warnings = remaining

	def _update_tree_row(self, index: int) -> None:
		columns = list(self.tree["columns"])
		children = list(self.tree.get_children())
		if index >= len(children):
			return
		item_id = children[index]
		if self._current_view == "results" and index < len(self.results):
			res = self.results[index]
			row: Dict[str, Any] = {
				"importovat": "✓" if self.import_flags[index] else "✗",
				"soubor": os.path.basename(res.file_path),
			}
			if res.data:
				row.update(res.data.model_dump())
			row["upozorneni"] = " | ".join(res.warnings) if res.warnings else "bez problému"
		elif self._current_view == "invoices" and index < len(self.invoices):
			inv = self.invoices[index]
			row = {
				"importovat": "✓" if self.import_flags[index] else "✗",
				"soubor": self._current_source_label,
			}
			row.update(inv.model_dump())
			extra_warning = get_warning(inv)
			if extra_warning:
				row["upozorneni"] = extra_warning
			row["upozorneni"] = row.get("upozorneni") or "bez problému"
		else:
			return
		values = [row.get(col, "") for col in columns]
		self.tree.item(item_id, values=values)
		if self.df is not None and index < len(self.df):
			for col in columns:
				self.df.at[index, col] = row.get(col, "")
		self._resort_after_data_change()

	def _choose_pdf(self) -> None:
		file_path = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
		if not file_path:
			return
		if not self._ensure_extractor_ready():
			return
		self._run_async(self._extract_single(file_path))

	async def _extract_single(self, file_path: str) -> None:
		# UI updates via after to ensure Tk thread-safety
		self.root.after(0, lambda: self.status_var.set("Probíhá extrakce…"))
		self.root.after(0, lambda: self.progress_var.set(0))
		extractor = self.extractor
		if extractor is None:
			self.root.after(0, lambda: messagebox.showerror("EasyFlex", "Extractor není připraven."))
			return
		results = await extractor.extract_auto(file_path)
		self.results = results
		self.root.after(0, self._fill_table)
		# Auto-import to ABRA (pokud povoleno) s využitím UPRAVENÝCH hodnot z tabulky
		if self.auto_import_var.get() and results:
			ok = 0
			errs = 0
			skipped = 0
			for i, _ in enumerate(results):
				if i >= len(self.import_flags) or not self.import_flags[i]:
					skipped += 1
					continue
				inv_dict = self._row_to_invoice_dict(i)
				if not self._has_minimal_invoice_data(inv_dict):
					skipped += 1
					continue
				try:
					self._ensure_abra_context_for_import()
					import_to_abra(inv_dict)
					ok += 1
				except Exception:  # noqa: BLE001
					errs += 1
			if errs or skipped:
				message_parts = []
				if ok > 0:
					message_parts.append(f"OK: {ok}")
				if errs > 0:
					message_parts.append(f"Chyby: {errs}")
				if skipped > 0:
					message_parts.append(f"Přeskočeno: {skipped}")
				self.root.after(0, lambda: messagebox.showwarning("ABRA", f"Import hotov: {'; '.join(message_parts)}"))
		self.root.after(0, lambda: self.status_var.set("Hotovo"))
		self.root.after(0, lambda: self.progress_var.set(100))

	def _choose_folder(self) -> None:
		folder = filedialog.askdirectory()
		if not folder:
			return
		if not self._ensure_extractor_ready():
			return
		self._run_async(self._extract_folder(folder))

	def _choose_table(self) -> None:
		file_path = filedialog.askopenfilename(
			filetypes=[
				("Tabulky", "*.csv *.xlsx *.xls *.xml"),
				("CSV", "*.csv"),
				("Excel", "*.xlsx *.xls"),
				("XML", "*.xml"),
				("Všechny soubory", "*.*"),
			]
		)
		if not file_path:
			return
		self._run_in_thread(lambda: self._process_table_file(file_path))

	def _process_table_file(self, file_path: str) -> None:
		self.root.after(0, lambda: self.status_var.set("Probíhá zpracování tabulky…"))
		self.root.after(0, lambda: self.progress_var.set(0))
		try:
			processor = self._get_csv_processor()
			invoices = processor.process_table_file(file_path)
			self.invoices = invoices
			self.results = []  # clear PDF results to avoid confusion
			src_label = os.path.basename(file_path)
			self.import_flags = [True] * len(invoices)
			rows = self._build_invoice_rows(invoices, src_label)
			pd = self._ensure_pandas()
			self.df = pd.DataFrame(rows)
			if rows:
				preview = {k: rows[0].get(k) for k in ("cislo_dokladu", "variabilni_symbol", "odberatel_jmeno")}
				logger.info("Tabulka – první řádek náhledu: %s", preview)
			self.root.after(0, lambda: self._fill_table_from_invoices(invoices, src_label, rows))
			# Auto-import s využitím UPRAVENÝCH hodnot z tabulky
			if invoices and self.auto_import_var.get():
				ok = 0
				err = 0
				skipped = 0
				for i, _ in enumerate(invoices):
					if i >= len(self.import_flags) or not self.import_flags[i]:
						skipped += 1
						continue
					inv_dict = self._row_to_invoice_dict(i)
					if not self._has_minimal_invoice_data(inv_dict):
						logger.info("Přeskakuji řádek %s – chybí minimální data: %s", i, {k: inv_dict.get(k) for k in ("cislo_dokladu", "variabilni_symbol", "odberatel_jmeno")})
						skipped += 1
						continue
					try:
						self._ensure_abra_context_for_import()
						import_to_abra(inv_dict)
						ok += 1
					except Exception:  # noqa: BLE001
						err += 1
				if err or skipped:
					message_parts = []
					if ok > 0:
						message_parts.append(f"OK: {ok}")
					if err > 0:
						message_parts.append(f"Chyby: {err}")
					if skipped > 0:
						message_parts.append(f"Přeskočeno: {skipped}")
					self.root.after(0, lambda: messagebox.showwarning("ABRA", f"Import hotov: {'; '.join(message_parts)}"))
			self.root.after(0, lambda: self.status_var.set("Hotovo"))
			self.root.after(0, lambda: self.progress_var.set(100))
		except Exception as exc:  # noqa: BLE001
			self.root.after(0, lambda: self.status_var.set("Chyba při zpracování"))
			self.root.after(0, lambda: self.progress_var.set(0))
			self.root.after(0, lambda: messagebox.showerror("Chyba", f"Chyba při zpracování tabulky: {exc}"))

	async def _extract_folder(self, folder: str) -> None:
		self.root.after(0, lambda: self.status_var.set("Probíhá batch extrakce…"))
		self.root.after(0, lambda: self.progress_var.set(0))

		def on_progress(current: int, total: int, last_file: str) -> None:
			self.root.after(0, lambda: self._set_progress(current, total, last_file))

		extractor = self.extractor
		if extractor is None:
			self.root.after(0, lambda: messagebox.showerror("EasyFlex", "Extractor není připraven."))
			return
		results = await extractor.batch_extract(folder, on_progress=on_progress)
		self.results = results
		self.root.after(0, self._fill_table)
		# Import to ABRA (pokud povoleno) s využitím UPRAVENÝCH hodnot z tabulky
		if self.auto_import_var.get():
			ok = 0
			errs = 0
			skipped = 0
			for i, _ in enumerate(results):
				if i >= len(self.import_flags) or not self.import_flags[i]:
					skipped += 1
					continue
				inv_dict = self._row_to_invoice_dict(i)
				if not self._has_minimal_invoice_data(inv_dict):
					skipped += 1
					continue
				try:
					self._ensure_abra_context_for_import()
					import_to_abra(inv_dict)
					ok += 1
				except Exception:
					errs += 1
			if errs or skipped:
				message_parts = []
				if ok > 0:
					message_parts.append(f"OK: {ok}")
				if errs > 0:
					message_parts.append(f"Chyby: {errs}")
				if skipped > 0:
					message_parts.append(f"Přeskočeno: {skipped}")
				self.root.after(0, lambda: messagebox.showwarning("ABRA", f"Import hotov: {'; '.join(message_parts)}"))
		self.root.after(0, lambda: self.status_var.set("Hotovo"))
		self.root.after(0, lambda: self.progress_var.set(100))

	def _export_csv(self) -> None:
		if not self.results and (self.df is None or self.df.empty):
			messagebox.showinfo("Export", "Žádná data k exportu.")
			return
		file_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
		if not file_path:
			return
		if (self.df is None or self.df.empty) and not self._ensure_extractor_ready():
			return
		# Run export in a background thread to keep UI responsive
		def do_export() -> None:
			try:
				if self.df is not None and not self.df.empty:
					self.df.to_csv(file_path, index=False, encoding="utf-8")
				else:
					extractor = self.extractor
					if extractor is None:
						raise RuntimeError("Extractor není připraven.")
					extractor.save_to_csv(self.results, file_path)
				self.root.after(0, lambda: messagebox.showinfo("Export", "Export dokončen."))
			except Exception as exc:  # noqa: BLE001
				self.root.after(0, lambda: messagebox.showerror("Export", f"Chyba exportu: {exc}"))
		self._run_in_thread(do_export)

	def _clear(self) -> None:
		self.results = []
		self.df = None
		self.tree.delete(*self.tree.get_children())
		self._reset_sort_state()
		self.progress_var.set(0)
		self.status_var.set("Připraven")

	def _on_cell_double_click(self, event) -> None:
		item_id = self.tree.focus()
		if not item_id:
			return
		col = self.tree.identify_column(event.x)
		col_index = int(col.replace('#', '')) - 1
		col_name = self.tree["columns"][col_index]
		old_value = self.tree.set(item_id, col_name)

		edit_window = tk.Toplevel(self.root)
		pretty = getattr(self, "_pretty_labels", {}).get(col_name, col_name)
		edit_window.title(f"Upravit {pretty}")

		entry = ttk.Entry(edit_window)
		entry.insert(0, old_value)
		entry.pack(padx=10, pady=10)

		def save_edit() -> None:
			new_value = entry.get()
			self.tree.set(item_id, col_name, new_value)
			# Update underlying DataFrame as well
			if self.df is not None:
				row_index = self.tree.index(item_id)
				self.df.at[row_index, col_name] = new_value
			self._resort_after_data_change()
			edit_window.destroy()

		btn_save = ttk.Button(edit_window, text="Uložit", command=save_edit)
		btn_save.pack(padx=10, pady=10)

	def _on_tree_click(self, event) -> None:
		"""Handle clicks on the tree to toggle import flags."""
		item_id = self.tree.identify_row(event.y)
		if not item_id:
			return
		
		col = self.tree.identify_column(event.x)
		col_index = int(col.replace('#', '')) - 1
		col_name = self.tree["columns"][col_index]
		
		# Only handle clicks on the "importovat" column
		if col_name == "importovat":
			row_index = self.tree.index(item_id)
			if 0 <= row_index < len(self.import_flags):
				# Toggle the import flag
				self.import_flags[row_index] = not self.import_flags[row_index]
				# Update the display
				self.tree.set(item_id, "importovat", "✓" if self.import_flags[row_index] else "✗")
				# Update DataFrame if it exists
				if self.df is not None and row_index < len(self.df):
					self.df.at[row_index, "importovat"] = "✓" if self.import_flags[row_index] else "✗"
				self._resort_after_data_change()

	def _run_async(self, coro) -> None:
		# Schedule coroutine in a background thread, UI updates must use root.after
		def runner() -> None:
			try:
				asyncio.run(coro)
			except Exception as exc:  # noqa: BLE001
				self.root.after(0, lambda: messagebox.showerror("Chyba", str(exc)))
		threading.Thread(target=runner, daemon=True).start()

	def _run_in_thread(self, func) -> None:
		def runner() -> None:
			try:
				func()
			except Exception as exc:  # noqa: BLE001
				logger.exception("Background task failed")
				self.root.after(0, lambda: messagebox.showerror("EasyFlex", str(exc)))
		threading.Thread(target=runner, daemon=True).start()

	def _ensure_abra_context_for_import(self) -> None:
		"""Persist currently selected ABRA combobox values into settings before import."""
		company_code, _ = self._get_selected_company_code_and_name()
		endpoint = self.direction_var.get() or "faktura-prijata"
		# Doc type: allow empty
		name_code = self.doc_type_var.get()
		_, dt_code = self._parse_doc_type_display(name_code) if name_code and name_code != "Nový typ…" else (None, None)
		self._persist_abra_context(
			company=company_code or "",
			doc_endpoint=endpoint,
			doc_type_code=(dt_code or ""),
		)

	def _show_abra_error(self, msg: str) -> None:
		ans = messagebox.askyesno("ABRA", f"Import selhal: {msg}\n\nChcete otevřít Nastavení?")
		if ans:
			self._open_settings()

	def _manual_import_to_abra(self) -> None:
		"""Manual import to ABRA when auto-import is disabled."""
		has_any = (self.df is not None and not self.df.empty)
		if not has_any:
			messagebox.showinfo("Import", "Žádná data k importu.")
			return
		
		# Perform import directly, respecting the import flags
		self._perform_manual_import()


	def _perform_manual_import(self) -> None:
		"""Perform the actual manual import to ABRA."""
		success_count = 0
		error_count = 0
		skipped_count = 0
		
		if self.df is not None and not self.df.empty:
			for i in range(len(self.df)):
				if i >= len(self.import_flags) or not self.import_flags[i]:
					skipped_count += 1
					continue
				inv_dict = self._row_to_invoice_dict(i)
				if not self._has_minimal_invoice_data(inv_dict):
					logger.info("Manual import skip row %s – chybí minimální data: %s", i + 1, {k: inv_dict.get(k) for k in ("cislo_dokladu", "variabilni_symbol", "odberatel_jmeno")})
					skipped_count += 1
					continue
				try:
					self._ensure_abra_context_for_import()
					import_to_abra(inv_dict)
					success_count += 1
				except Exception as e:
					error_count += 1
					logger.error("Manual import failed for row %d: %s", i + 1, e)
		
		# Show summary message
		message_parts = []
		if success_count > 0:
			message_parts.append(f"Úspěšně importováno: {success_count}")
		if error_count > 0:
			message_parts.append(f"Chyby: {error_count}")
		if skipped_count > 0:
			message_parts.append(f"Přeskočeno: {skipped_count}")
		
		if message_parts:
			messagebox.showinfo("Import", "\n".join(message_parts))
		else:
			messagebox.showinfo("Import", "Žádné soubory k importu.")

	def _on_right_click(self, event) -> None:
		"""Handle right-click to show context menu."""
		item_id = self.tree.identify_row(event.y)
		if not item_id:
			return
		
		# Create context menu
		context_menu = tk.Menu(self.root, tearoff=0)
		
		row_index = self.tree.index(item_id)
		if 0 <= row_index < len(self.import_flags):
			current_state = self.import_flags[row_index]
			
			if current_state:
				context_menu.add_command(label="Vyloučit z importu", command=lambda: self._toggle_import_flag(row_index))
			else:
				context_menu.add_command(label="Zahrnout do importu", command=lambda: self._toggle_import_flag(row_index))
			
			context_menu.add_separator()
			context_menu.add_command(label="Označit všechny", command=self._mark_all_for_import)
			context_menu.add_command(label="Odznačit všechny", command=self._unmark_all_for_import)
		
		# Show context menu
		try:
			context_menu.tk_popup(event.x_root, event.y_root)
		finally:
			context_menu.grab_release()

	def _toggle_import_flag(self, row_index: int) -> None:
		"""Toggle import flag for a specific row."""
		if 0 <= row_index < len(self.import_flags):
			self.import_flags[row_index] = not self.import_flags[row_index]
			# Update the display
			item_id = self.tree.get_children()[row_index]
			self.tree.set(item_id, "importovat", "✓" if self.import_flags[row_index] else "✗")
			# Update DataFrame if it exists
			if self.df is not None and row_index < len(self.df):
				self.df.at[row_index, "importovat"] = "✓" if self.import_flags[row_index] else "✗"
		self._resort_after_data_change()

	def _mark_all_for_import(self) -> None:
		"""Mark all rows for import."""
		for i in range(len(self.import_flags)):
			self.import_flags[i] = True
		self._refresh_import_display()

	def _unmark_all_for_import(self) -> None:
		"""Unmark all rows for import."""
		for i in range(len(self.import_flags)):
			self.import_flags[i] = False
		self._refresh_import_display()

	def _refresh_import_display(self) -> None:
		"""Refresh the import column display."""
		for i, item_id in enumerate(self.tree.get_children()):
			if i < len(self.import_flags):
				self.tree.set(item_id, "importovat", "✓" if self.import_flags[i] else "✗")
				# Update DataFrame if it exists
				if self.df is not None and i < len(self.df):
					self.df.at[i, "importovat"] = "✓" if self.import_flags[i] else "✗"
		self._resort_after_data_change()



class SettingsDialog:
	def __init__(self, parent: tk.Tk, on_saved=None, auto_import_var: Optional[tk.BooleanVar] = None) -> None:
		self.parent = parent
		self.on_saved = on_saved
		self.auto_import_var = auto_import_var or tk.BooleanVar(value=False)
		self.win = tk.Toplevel(parent)
		self.win.title("Nastavení")
		self.win.transient(parent)
		self.win.grab_set()
		self._log_win: Optional[tk.Toplevel] = None
		self._log_text: Optional[scrolledtext.ScrolledText] = None
		self._log_refresh_job: Optional[str] = None

		self._build()

	def _build(self) -> None:
		cfg = load_config()
		nb = ttk.Notebook(self.win)
		frm_abra = ttk.Frame(nb)
		frm_ex = ttk.Frame(nb)
		frm_extraction = ttk.Frame(nb)
		frm_admin = ttk.Frame(nb)
		nb.add(frm_abra, text="Abra")
		nb.add(frm_ex, text="Extractor")
		nb.add(frm_extraction, text="Extrakce")
		nb.add(frm_admin, text="Admin")
		nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

		# Abra fields
		self.var_server = tk.StringVar(value=cfg.abra_server or "")
		self.var_port = tk.StringVar(value=str(cfg.abra_port or ""))
		self.var_user = tk.StringVar(value=cfg.abra_username or "")
		self.var_pass = tk.StringVar(value=cfg.abra_password or "")
		self.var_abra_verify_tls = tk.BooleanVar(value=getattr(cfg, 'abra_verify_tls', True))

		self._add_labeled_entry(frm_abra, "Server", self.var_server)
		self._add_labeled_entry(frm_abra, "Port", self.var_port)
		self._add_labeled_entry(frm_abra, "Uživatel", self.var_user)
		self._add_labeled_entry(frm_abra, "Heslo", self.var_pass, show="*")
		chk_tls = ttk.Checkbutton(frm_abra, text="Ověřovat TLS certifikát (doporučeno)", variable=self.var_abra_verify_tls)
		chk_tls.pack(anchor=tk.W, padx=10, pady=5)

		# Extractor fields with recommended defaults
		self.var_api_key = tk.StringVar(value=cfg.openai_api_key or "")
		self.var_model = tk.StringVar(value=cfg.openai_model or "gpt-5")
		self.var_conc = tk.StringVar(value=str(cfg.concurrency or 2))
		self.var_max_tokens = tk.StringVar(value=str(cfg.max_tokens or 1024))
		self.var_dpi = tk.StringVar(value=str(cfg.dpi or 200))
		self.var_max_pages = tk.StringVar(value=str(cfg.max_pages or 2))
		self.var_req_delay = tk.StringVar(value=str(cfg.openai_request_delay or 0.5))
		self.var_max_retries = tk.StringVar(value=str(cfg.openai_max_retries or 5))
		self.var_img_width = tk.StringVar(value=str(getattr(cfg, 'image_max_width', 1600)))
		self.var_img_quality = tk.StringVar(value=str(getattr(cfg, 'image_jpeg_quality', 85)))

		self._add_labeled_entry(frm_ex, "API Key", self.var_api_key)
		self._add_labeled_entry(frm_ex, "Model", self.var_model)
		self._add_labeled_entry(frm_ex, "Concurrency", self.var_conc)
		self._add_labeled_entry(frm_ex, "Max Tokens", self.var_max_tokens)
		self._add_labeled_entry(frm_ex, "PDF DPI", self.var_dpi)
		self._add_labeled_entry(frm_ex, "Max Pages", self.var_max_pages)
		self._add_labeled_entry(frm_ex, "Request Delay (s)", self.var_req_delay)
		self._add_labeled_entry(frm_ex, "Max Retries", self.var_max_retries)
		self._add_labeled_entry(frm_ex, "Max šířka obrázků (px)", self.var_img_width)
		self._add_labeled_entry(frm_ex, "JPEG kvalita (50-100)", self.var_img_quality)

		# Extraction options
		self.var_use_doc_number_as_variable_symbol = tk.BooleanVar(value=cfg.use_doc_number_as_variable_symbol)
		self.var_infer_missing_dates = tk.BooleanVar(value=getattr(cfg, "infer_missing_dates", False))
		self.var_enable_multi_invoice_segmentation = tk.BooleanVar(value=getattr(cfg, 'enable_multi_invoice_segmentation', False))
		self.var_csv_enable_llm = tk.BooleanVar(value=getattr(cfg, 'csv_enable_llm_mapping', False))
		self.var_date_order = tk.StringVar(value="dd-mm" if getattr(cfg, 'date_day_first', True) else "mm-dd")

		# Auto-import checkbox (moved here from main toolbar)
		chk_auto_import_settings = ttk.Checkbutton(
			frm_extraction,
			text="Auto-import do ABRA",
			variable=self.auto_import_var,
		)
		chk_auto_import_settings.pack(anchor=tk.W, padx=10, pady=5)

		chk_use_doc_number_as_variable_symbol = ttk.Checkbutton(
			frm_extraction, 
			text="Použít číslo dokladu jako variabilní symbol",
			variable=self.var_use_doc_number_as_variable_symbol
		)
		chk_use_doc_number_as_variable_symbol.pack(anchor=tk.W, padx=10, pady=5)

		chk_infer_missing_dates = ttk.Checkbutton(
			frm_extraction,
			text="Domyšlení chybějících datumů",
			variable=self.var_infer_missing_dates
		)
		chk_infer_missing_dates.pack(anchor=tk.W, padx=10, pady=5)

		frm_date_order = ttk.LabelFrame(frm_extraction, text="Formát dne a měsíce")
		frm_date_order.pack(fill=tk.X, padx=10, pady=5)
		rb_dd_mm = ttk.Radiobutton(
			frm_date_order,
			text="Den-Měsíc (DD-MM)",
			value="dd-mm",
			variable=self.var_date_order
		)
		rb_dd_mm.pack(anchor=tk.W, padx=8, pady=2)
		rb_mm_dd = ttk.Radiobutton(
			frm_date_order,
			text="Měsíc-Den (MM-DD)",
			value="mm-dd",
			variable=self.var_date_order
		)
		rb_mm_dd.pack(anchor=tk.W, padx=8, pady=2)

		chk_enable_multi = ttk.Checkbutton(
			frm_extraction,
			text="Povolit segmentaci více faktur v jednom PDF (dvoufázově)",
			variable=self.var_enable_multi_invoice_segmentation
		)
		chk_enable_multi.pack(anchor=tk.W, padx=10, pady=5)
		chk_csv_llm = ttk.Checkbutton(
			frm_extraction,
			text="Povolit LLM mapování CSV sloupců (odesílá ukázková data)",
			variable=self.var_csv_enable_llm
		)
		chk_csv_llm.pack(anchor=tk.W, padx=10, pady=5)

		self._build_admin_tab(frm_admin)

		frm_btns = ttk.Frame(self.win)
		frm_btns.pack(fill=tk.X, padx=10, pady=(0, 10))
		btn_ok = ttk.Button(frm_btns, text="Uložit", command=self._save)
		btn_ok.pack(side=tk.RIGHT, padx=5)
		btn_cancel = ttk.Button(frm_btns, text="Zavřít", command=self._close)
		btn_cancel.pack(side=tk.RIGHT, padx=5)

	def _add_labeled_entry(self, parent: tk.Widget, label: str, var: tk.StringVar, show: Optional[str] = None) -> None:
		row = ttk.Frame(parent)
		row.pack(fill=tk.X, padx=5, pady=3)
		lbl = ttk.Label(row, text=label + ":", width=20)
		lbl.pack(side=tk.LEFT)
		ent = ttk.Entry(row, textvariable=var, show=show)
		ent.pack(side=tk.LEFT, fill=tk.X, expand=True)

	def _build_admin_tab(self, frame: ttk.Frame) -> None:
		info = ttk.Label(
			frame,
			text=(
				"Otevře okno s textovou konzolí aplikace. "
				"Zobrazuje poslední logy pro diagnostiku problémů."
			),
			wraplength=520,
			justify=tk.LEFT,
		)
		info.pack(fill=tk.X, padx=10, pady=(10, 5))
		btn_console = ttk.Button(frame, text="Otevřít log konzoli", command=self._open_log_console)
		btn_console.pack(anchor=tk.W, padx=10, pady=(0, 10))

	def _open_log_console(self) -> None:
		if self._log_win is not None and self._log_win.winfo_exists():
			self._log_win.lift()
			self._refresh_log_console()
			return
		self._log_win = tk.Toplevel(self.win)
		self._log_win.title("EasyFlex – Konzole")
		self._log_win.transient(self.win)
		self._log_win.geometry("900x420")
		self._log_win.protocol("WM_DELETE_WINDOW", self._close_log_console)
		self._log_text = scrolledtext.ScrolledText(
			self._log_win,
			state=tk.DISABLED,
			wrap=tk.WORD,
			font=("Consolas", 10),
		)
		self._log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
		hint = ttk.Label(
			self._log_win,
			text="Výpis se obnovuje automaticky každé 2 sekundy. Označený text můžete zkopírovat (Ctrl+C).",
			wraplength=860,
			justify=tk.LEFT,
		)
		hint.pack(fill=tk.X, padx=10, pady=(0, 5))
		btn_row = ttk.Frame(self._log_win)
		btn_row.pack(fill=tk.X, padx=10, pady=(0, 10))
		btn_close = ttk.Button(btn_row, text="Zavřít", command=self._close_log_console)
		btn_close.pack(side=tk.RIGHT, padx=5)
		btn_refresh = ttk.Button(btn_row, text="Aktualizovat", command=self._refresh_log_console)
		btn_refresh.pack(side=tk.RIGHT, padx=5)
		self._refresh_log_console()
		self._schedule_log_refresh()

	def _refresh_log_console(self) -> None:
		if self._log_text is None:
			return
		messages = get_log_messages()
		content = "\n".join(messages) if messages else "Zatím žádné záznamy."
		self._log_text.configure(state=tk.NORMAL)
		self._log_text.delete("1.0", tk.END)
		self._log_text.insert(tk.END, content)
		self._log_text.configure(state=tk.DISABLED)
		self._log_text.see(tk.END)

	def _schedule_log_refresh(self) -> None:
		if self._log_win is None or not self._log_win.winfo_exists():
			return
		self._cancel_pending_log_refresh()
		self._log_refresh_job = self._log_win.after(2000, self._on_log_refresh_tick)

	def _on_log_refresh_tick(self) -> None:
		self._log_refresh_job = None
		if self._log_win is None or not self._log_win.winfo_exists():
			return
		self._refresh_log_console()
		self._schedule_log_refresh()

	def _cancel_pending_log_refresh(self) -> None:
		if self._log_refresh_job and self._log_win is not None and self._log_win.winfo_exists():
			try:
				self._log_win.after_cancel(self._log_refresh_job)
			except Exception:  # noqa: BLE001
				pass
		self._log_refresh_job = None

	def _close_log_console(self) -> None:
		self._cancel_pending_log_refresh()
		if self._log_win is not None:
			try:
				if self._log_win.winfo_exists():
					self._log_win.destroy()
			except Exception:  # noqa: BLE001
				pass
		self._log_win = None
		self._log_text = None

	def _save(self) -> None:
		cp = read_settings()
		if not cp.has_section("abra"):
			cp.add_section("abra")
		cp.set("abra", "server", self.var_server.get().strip())
		# Validate numeric fields
		def _is_int(val: str) -> bool:
			try:
				int(val)
				return True
			except Exception:
				return False
		def _is_float(val: str) -> bool:
			try:
				float(val)
				return True
			except Exception:
				return False

		port_val = self.var_port.get().strip()
		if port_val and not _is_int(port_val):
			messagebox.showerror("Nastavení", "Port musí být číslo.")
			return
		if port_val:
			cp.set("abra", "port", port_val)
		else:
			if cp.has_option("abra", "port"):
				cp.remove_option("abra", "port")
		cp.set("abra", "username", self.var_user.get().strip())
		cp.set("abra", "password", self.var_pass.get().strip())
		cp.set("abra", "verify_tls", str(self.var_abra_verify_tls.get()).lower())

		if not cp.has_section("extractor"):
			cp.add_section("extractor")
		cp.set("extractor", "api_key", self.var_api_key.get().strip())
		cp.set("extractor", "model", self.var_model.get().strip() or "gpt-5")
		conc_val = self.var_conc.get().strip() or "2"
		if not _is_int(conc_val):
			messagebox.showerror("Nastavení", "Concurrency musí být celé číslo.")
			return
		cp.set("extractor", "concurrency", conc_val)
		mtok_val = self.var_max_tokens.get().strip() or "1024"
		if not _is_int(mtok_val):
			messagebox.showerror("Nastavení", "Max Tokens musí být celé číslo.")
			return
		cp.set("extractor", "max_tokens", mtok_val)
		dpi_val = self.var_dpi.get().strip() or "200"
		if not _is_int(dpi_val):
			messagebox.showerror("Nastavení", "PDF DPI musí být celé číslo.")
			return
		cp.set("extractor", "pdf_dpi", dpi_val)
		mp_val = self.var_max_pages.get().strip() or "2"
		if not _is_int(mp_val):
			messagebox.showerror("Nastavení", "Max Pages musí být celé číslo.")
			return
		cp.set("extractor", "max_pages", mp_val)
		rd_val = self.var_req_delay.get().strip() or "0.5"
		if not _is_float(rd_val):
			messagebox.showerror("Nastavení", "Request Delay musí být číslo (sekundy).")
			return
		cp.set("extractor", "request_delay", rd_val)
		mr_val = self.var_max_retries.get().strip() or "5"
		if not _is_int(mr_val):
			messagebox.showerror("Nastavení", "Max Retries musí být celé číslo.")
			return
		cp.set("extractor", "max_retries", mr_val)
		imw_val = self.var_img_width.get().strip() or "1600"
		if not _is_int(imw_val):
			messagebox.showerror("Nastavení", "Max šířka obrázků musí být celé číslo.")
			return
		jq_val = self.var_img_quality.get().strip() or "85"
		if not _is_int(jq_val):
			messagebox.showerror("Nastavení", "JPEG kvalita musí být celé číslo (50-100).")
			return
		cp.set("extractor", "image_max_width", imw_val)
		cp.set("extractor", "image_jpeg_quality", jq_val)

		# Save extraction options
		if not cp.has_section("extraction"):
			cp.add_section("extraction")
		cp.set("extraction", "use_doc_number_as_variable_symbol", str(self.var_use_doc_number_as_variable_symbol.get()).lower())
		cp.set("extraction", "infer_missing_dates", str(self.var_infer_missing_dates.get()).lower())
		if cp.has_option("extraction", "use_issue_date_as_due_date"):
			cp.remove_option("extraction", "use_issue_date_as_due_date")
		cp.set("extraction", "enable_multi_invoice_segmentation", str(self.var_enable_multi_invoice_segmentation.get()).lower())
		cp.set("extraction", "date_order", self.var_date_order.get())
		# CSV options
		if not cp.has_section("csv"):
			cp.add_section("csv")
		cp.set("csv", "enable_llm_mapping", str(self.var_csv_enable_llm.get()).lower())

		write_settings(cp)
		if self.on_saved:
			self.on_saved()
		self._close()

	def _close(self) -> None:
		self._close_log_console()
		self.win.destroy()
