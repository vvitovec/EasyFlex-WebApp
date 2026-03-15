"""Invoice batch helpers for storing, displaying, and updating extracted data."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import joinedload

from EasyFlex.date_helpers import domysleni_chybejicich_datumu
from EasyFlex.invoice_warnings import get_warning, set_warning
from EasyFlex.models import InvoiceData

from .models import InvoiceBatch, InvoiceRow, User, db


DISPLAY_COLUMNS: List[str] = [
	"soubor",
	"cislo_dokladu",
	"variabilni_symbol",
	"dodavatel_jmeno",
	"dodavatel_ic",
	"dodavatel_dic",
	"dodavatel_adresa",
	"dodavatel_stat",
	"odberatel_jmeno",
	"odberatel_ic",
	"odberatel_dic",
	"odberatel_adresa",
	"odberatel_stat",
	"datum_vystaveni",
	"datum_duzp",
	"datum_splatnosti",
	"zaklad_dane_0",
	"zaklad_dane_12",
	"zaklad_dane_21",
	"vyse_dph_12",
	"vyse_dph_21",
	"zaklad_dane",
	"vyse_dph",
	"celkova_cena",
	"mena",
	"upozorneni",
]

COLUMN_LABELS: Dict[str, str] = {
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
	"zaklad_dane": "Základ daně (celkem)",
	"vyse_dph": "DPH celkem",
	"celkova_cena": "Celkem k úhradě",
	"mena": "Měna",
	"upozorneni": "Upozornění",
}


def _serialize_invoice(invoice: Any) -> dict:
	"""Convert invoice-like object to normalized dict for templates/storage."""
	if invoice is None:
		return {}

	# Normalize through InvoiceData to ensure we keep all buyer/supplier fields with stable keys
	try:
		normalized = InvoiceData.model_validate(invoice)
	except Exception:
		normalized = None
	if normalized is not None:
		try:
			return normalized.model_dump()
		except Exception:
			return {}

	# Fallbacks for unexpected inputs
	if isinstance(invoice, dict):
		try:
			return dict(invoice)
		except Exception:
			return {}
	try:
		return dict(invoice)
	except Exception:
		return {}


def _normalize_warning_text(warning: Optional[str]) -> Optional[str]:
	if not warning:
		return None
	text = str(warning).strip()
	return text or None


def _combine_warnings(invoice_obj: Any, extra: Iterable[str] | None = None) -> Optional[str]:
	"""Merge warnings from invoice object and provided list into one string."""
	warnings: list[str] = []
	raw = get_warning(invoice_obj)
	if raw:
		warnings.extend(part.strip() for part in str(raw).split("|") if part.strip())
	for item in extra or []:
		text = str(item).strip()
		if text and text not in warnings:
			warnings.append(text)
	joined = " | ".join(warnings)
	return joined or None


def create_batch_from_results(user: User, results: List[Any], source_label: str, source_type: str = "pdf") -> InvoiceBatch:
	"""Persist ExtractResult objects as a batch."""
	batch = InvoiceBatch(
		user=user,
		source_label=source_label,
		source_type=source_type,
		processing_status="completed",
		current_phase="done",
	)
	db.session.add(batch)
	db.session.flush()
	success_count = 0
	error_count = 0
	for idx, res in enumerate(results):
		invoice_dict = _serialize_invoice(getattr(res, "data", None))
		warning_text = _combine_warnings(getattr(res, "data", None), getattr(res, "warnings", None))
		if warning_text:
			set_warning(invoice_dict, warning_text)
		row_error = getattr(res, "error", None)
		if invoice_dict:
			success_count += 1
		if row_error:
			error_count += 1
		source_name = ""
		if getattr(res, "file_path", None):
			raw_path = getattr(res, "file_path")
			try:
				path_obj = Path(raw_path)
				source_name = str(path_obj) if not path_obj.is_absolute() else path_obj.name
			except Exception:
				source_name = str(raw_path)
		row = InvoiceRow(
			batch=batch,
			row_index=idx,
			source=source_name or source_label,
			invoice_data=invoice_dict,
			warning=warning_text,
			error=row_error,
			marked_for_import=True,
		)
		db.session.add(row)
	unique_sources = {str((getattr(res, "file_path", None) or "")).strip() for res in results if getattr(res, "file_path", None)}
	batch.total_files = len(unique_sources) if unique_sources else len(results)
	batch.processed_files = batch.total_files
	batch.processed_invoices = len(results)
	batch.total_invoices_estimate = len(results)
	batch.success_count = success_count
	batch.error_count = error_count
	batch.credits_charged = success_count if source_type == "pdf" else 0
	batch.processing_status = "completed_with_errors" if error_count else "completed"
	batch.summary_message = (
		f"Dokončeno: {batch.success_count} úspěšně, {batch.error_count} s chybou, "
		f"zpracováno {batch.processed_files}/{batch.total_files} souborů."
	)
	db.session.commit()
	return batch


def create_batch_from_invoices(user: User, invoices: List[Any], source_label: str, source_type: str = "table") -> InvoiceBatch:
	"""Persist InvoiceData list (from CSV/XLSX/XML) as a batch."""
	batch = InvoiceBatch(
		user=user,
		source_label=source_label,
		source_type=source_type,
		processing_status="completed",
		total_files=1 if invoices else 0,
		processed_files=1 if invoices else 0,
		processed_invoices=len(invoices),
		total_invoices_estimate=len(invoices),
		current_phase="done",
		success_count=len(invoices),
		error_count=0,
		credits_charged=0,
		summary_message=f"Načteno {len(invoices)} faktur z tabulky.",
	)
	db.session.add(batch)
	db.session.flush()
	for idx, inv in enumerate(invoices):
		invoice_dict = _serialize_invoice(inv)
		warning_text = _combine_warnings(inv, None)
		if warning_text:
			set_warning(invoice_dict, warning_text)
		row = InvoiceRow(
			batch=batch,
			row_index=idx,
			source=f"{source_label} #{idx + 1}",
			invoice_data=invoice_dict,
			warning=warning_text,
			error=None,
			marked_for_import=True,
		)
		db.session.add(row)
	db.session.commit()
	return batch


def load_batch_for_user(batch_id: int, user: User) -> Optional[InvoiceBatch]:
	return (
		InvoiceBatch.query.filter_by(id=batch_id, user_id=user.id)
		.options(joinedload(InvoiceBatch.rows))
		.first()
	)


def rows_for_display(batch: InvoiceBatch) -> list[dict]:
	rows: list[dict] = []
	for row in sorted(batch.rows, key=lambda r: r.row_index):
		invoice_dict = dict(row.invoice_data or {})
		warning_text = _normalize_warning_text(row.warning) or _normalize_warning_text(invoice_dict.get("upozorneni"))
		if warning_text:
			set_warning(invoice_dict, warning_text)
		rows.append({
			"id": row.id,
			"index": row.row_index,
			"source": row.source or batch.source_label or "",
			"invoice": invoice_dict,
			"warning": warning_text or "bez problému",
			"error": row.error,
			"status": row.status,
			"marked_for_import": bool(row.marked_for_import),
		})
	return rows


def _merge_inference_warnings(original: Optional[str], inferred: Iterable[str]) -> Optional[str]:
	def _is_inference(text: str) -> bool:
		lower = text.lower()
		return "dopln" in lower or "dopoč" in lower

	parts: list[str] = []
	for chunk in (original or "").split("|"):
		val = chunk.strip()
		if val and not _is_inference(val):
			if val not in parts:
				parts.append(val)
	for new in inferred:
		val = str(new).strip()
		if val and val not in parts:
			parts.append(val)
	return " | ".join(parts) if parts else None


def apply_invoice_updates(row: InvoiceRow, updates: Dict[str, Any], *, day_first: Optional[bool] = None) -> None:
	"""Update stored invoice payload and refresh warnings/dates."""
	invoice = dict(row.invoice_data or {})
	for key, value in updates.items():
		invoice[key] = value

	# Recompute missing dates and inference warnings
	date_payload = {
		"datum_vystaveni": invoice.get("datum_vystaveni"),
		"datum_splatnosti": invoice.get("datum_splatnosti"),
		"datum_duzp": invoice.get("datum_duzp"),
	}
	date_payload, inferred_warnings = domysleni_chybejicich_datumu(date_payload, day_first=day_first)
	invoice.update(date_payload)

	merged_warning = _merge_inference_warnings(row.warning or invoice.get("upozorneni"), inferred_warnings)
	if merged_warning:
		set_warning(invoice, merged_warning)
	row.invoice_data = invoice
	row.warning = merged_warning
	db.session.commit()
