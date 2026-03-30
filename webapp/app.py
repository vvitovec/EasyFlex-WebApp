"""Flask web layer for EasyFlex."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from dotenv import load_dotenv
from flask import (
	Flask,
	render_template,
	request,
	redirect,
	url_for,
	flash,
	send_file,
	abort,
	jsonify,
)
from flask_login import login_required, current_user
from sqlalchemy.exc import OperationalError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from EasyFlex.config import load_config
from EasyFlex.date_helpers import DATE_FIELDS, parse_invoice_date
from EasyFlex.invoice_warnings import get_warning, set_warning
from EasyFlex.models import InvoiceData

from .models import init_db, db, User, UserSettings, InvoiceBatch, InvoiceRow, BatchJob
from .auth import init_auth
from .config_utils import EXTRACTOR_OVERRIDE_KEYS, get_user_config, get_user_config_for_user
from .invoice_batches import (
	DISPLAY_COLUMNS,
	COLUMN_LABELS,
	_apply_row_filter,
	apply_invoice_updates,
	count_selected_rows,
	create_batch_from_invoices,
	load_batch_for_user,
	query_rows_for_batch,
	rows_for_display,
)
from .batch_jobs import (
	active_job_for_batch,
	batch_payload_path,
	is_terminal_batch_status,
	job_status_label,
	persist_batch_payload,
	queue_batch_job,
	utcnow,
	batch_storage_root,
)
from .abra_context import (
	add_company as store_company,
	add_doc_type as store_doc_type,
	apply_context_to_config,
	current_context,
	ensure_seed_data,
	list_all_doc_types,
	list_companies,
	list_doc_types,
	persist_context,
	delete_company,
	delete_doc_type,
)
from .constants import (
	BATCH_PHASE_LABELS,
	BATCH_STATUS_COMPLETED,
	BATCH_STATUS_COMPLETED_WITH_ERRORS,
	BATCH_STATUS_FAILED,
	BATCH_STATUS_IMPORTING,
	BATCH_STATUS_INTERRUPTED,
	BATCH_STATUS_LABELS,
	BATCH_STATUS_PARTIAL_TIMEOUT,
	BATCH_STATUS_QUEUED,
	BATCH_STATUS_RUNNING,
	BATCH_STATUS_WAITING_IMPORT,
	DEFAULT_MAX_BATCH_TOTAL_BYTES,
	DEFAULT_MAX_BATCH_TOTAL_PAGES,
	DEFAULT_MAX_PDF_FILE_BYTES,
	DEFAULT_MAX_PDF_FILES_PER_BATCH,
	DEFAULT_MAX_TABLE_FILE_BYTES,
	DEFAULT_MAX_UPLOAD_BYTES,
	DEFAULT_PAGE_SIZE,
	JOB_TYPE_EXTRACT_PDF,
	JOB_TYPE_IMPORT_ABRA,
	MAX_PAGE_SIZE,
	MIN_CREDIT_PURCHASE,
	PDF_INVOICE_CREDIT_COST,
	PRICE_PER_CREDIT,
	STARTING_CREDITS,
	TABLE_UPLOAD_CREDIT_COST,
)

logger = logging.getLogger(__name__)
_DEFAULT_PDF_BATCH_MAX_RUNTIME_S = 60 * 60
_DEFAULT_DB_COMMIT_RETRY_ATTEMPTS = 3

_DbResultT = TypeVar("_DbResultT")


EDITABLE_FIELDS = [
	("cislo_dokladu", "Číslo dokladu"),
	("variabilni_symbol", "Variabilní symbol"),
	("dodavatel_jmeno", "Dodavatel – název"),
	("dodavatel_ic", "Dodavatel – IČ"),
	("dodavatel_dic", "Dodavatel – DIČ"),
	("odberatel_jmeno", "Odběratel – název"),
	("mena", "Měna"),
	("datum_vystaveni", "Datum vystavení"),
	("datum_duzp", "Datum DUZP"),
	("datum_splatnosti", "Datum splatnosti"),
	("zaklad_dane", "Základ daně (celkem)"),
	("vyse_dph", "DPH celkem"),
	("celkova_cena", "Celkem k úhradě"),
	("zaklad_dane_0", "Základ daně 0 %"),
	("zaklad_dane_12", "Základ daně 12 %"),
	("zaklad_dane_21", "Základ daně 21 %"),
	("vyse_dph_12", "DPH 12 %"),
	("vyse_dph_21", "DPH 21 %"),
]

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


def _run_extraction(
	extractor: InvoiceExtractor,
	pdf_path: Path,
	*,
	on_result: Optional[Callable[[ExtractResult], None]] = None,
) -> List[ExtractResult]:
	"""Run async extractor in a blocking context."""
	try:
		return asyncio.run(extractor.extract_auto(str(pdf_path), on_result=on_result))
	except RuntimeError:
		loop = asyncio.new_event_loop()
		try:
			return loop.run_until_complete(extractor.extract_auto(str(pdf_path), on_result=on_result))
		finally:
			loop.close()


def _normalize_upload_label(filename: str, index: int) -> str:
	"""Return a safe label for displaying the uploaded path."""
	name = (filename or "").replace("\\", "/").strip().lstrip("./")
	return name or f"upload-{index + 1}.pdf"


def _dedupe_filename(base_name: str, used: set[str], *, fallback: str) -> str:
	"""Ensure uploaded files saved to disk have unique, safe names."""
	safe_base = secure_filename(base_name) or fallback
	root = Path(safe_base).stem or "upload"
	suffix = Path(safe_base).suffix or Path(fallback).suffix or ".pdf"
	candidate = f"{root}{suffix}"
	counter = 2
	while candidate in used:
		candidate = f"{root}-{counter}{suffix}"
		counter += 1
	used.add(candidate)
	return candidate


def _env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
	raw = (os.getenv(name) or "").strip()
	if not raw:
		value = default
	else:
		try:
			value = int(raw)
		except ValueError:
			value = default
	if minimum is not None:
		return max(minimum, value)
	return value


def _pdf_upload_limits() -> dict[str, int]:
	return {
		"max_request_bytes": _env_int("MAX_CONTENT_LENGTH", DEFAULT_MAX_UPLOAD_BYTES, minimum=1024 * 1024),
		"max_files": _env_int("MAX_PDF_FILES_PER_BATCH", DEFAULT_MAX_PDF_FILES_PER_BATCH, minimum=1),
		"max_file_bytes": _env_int("MAX_PDF_FILE_BYTES", DEFAULT_MAX_PDF_FILE_BYTES, minimum=1024),
		"max_total_bytes": _env_int("MAX_BATCH_TOTAL_BYTES", DEFAULT_MAX_BATCH_TOTAL_BYTES, minimum=1024),
		"max_total_pages": _env_int("MAX_BATCH_TOTAL_PAGES", DEFAULT_MAX_BATCH_TOTAL_PAGES, minimum=1),
	}


def _max_table_file_bytes() -> int:
	return _env_int("MAX_TABLE_FILE_BYTES", DEFAULT_MAX_TABLE_FILE_BYTES, minimum=1024)


def _read_pdf_page_count(pdf_path: Path, *, poppler_path: Optional[str]) -> int:
	try:
		from pdf2image import pdfinfo_from_path
	except Exception:
		return 0
	try:
		info = pdfinfo_from_path(str(pdf_path), poppler_path=poppler_path)
	except Exception:
		return 0
	try:
		return max(0, int(info.get("Pages") or 0))
	except Exception:
		return 0


def _build_csv_processor(cfg):
	from EasyFlex.csv_processor import CSVProcessor

	return CSVProcessor(config=cfg)


def _derive_batch_label(display_names: List[str]) -> str:
	"""Derive human-friendly source label for a batch."""
	if not display_names:
		return "nahraná PDF"
	if len(display_names) == 1:
		return Path(display_names[0]).name
	folder_hint = None
	for name in display_names:
		parts = name.replace("\\", "/").split("/")
		if len(parts) > 1 and parts[0]:
			folder_hint = parts[0]
			break
	if folder_hint:
		return f"Složka {folder_hint} ({len(display_names)} PDF)"
	return f"{len(display_names)} PDF souborů"


def _parse_float_value(value: str) -> float:
	import re  # local import to mirror GUI helper without global dependency
	clean_value = re.sub(r"[\s\u00A0\u202F]", "", value)
	clean_value = clean_value.replace(",", ".")
	clean_value = re.sub(r"(?<=\d)\.(?=\d{3}(?:\D|$))", "", clean_value)
	if clean_value in {"", "-"}:
		raise ValueError("Empty numeric value")
	return float(clean_value)


def _has_minimal_invoice_data(inv: Dict[str, object]) -> bool:
	"""Minimal sanity check to avoid importing empty rows."""
	return bool(inv.get("cislo_dokladu") or inv.get("variabilni_symbol") or inv.get("odberatel_jmeno"))


def _deduct_credits(user: User, amount: int) -> int:
	"""Decrease user credits by amount and persist. Returns remaining credits."""
	if amount <= 0:
		return user.credits
	user.credits = max(0, int(user.credits or 0) - amount)
	db.session.commit()
	return user.credits


def _utcnow() -> datetime:
	return utcnow()


def _batch_status_label(status: Optional[str]) -> str:
	return BATCH_STATUS_LABELS.get((status or "").strip().lower(), "Neznámý stav")


def _batch_phase_label(phase: Optional[str]) -> str:
	return BATCH_PHASE_LABELS.get((phase or "").strip().lower(), "Zpracování")


def _is_terminal_batch_status(status: Optional[str]) -> bool:
	return is_terminal_batch_status(status)


def _load_batch_runtime_limit_s() -> int:
	raw = (os.getenv("PDF_BATCH_MAX_RUNTIME_S") or "").strip()
	if not raw:
		return _DEFAULT_PDF_BATCH_MAX_RUNTIME_S
	try:
		value = int(raw)
	except ValueError:
		return _DEFAULT_PDF_BATCH_MAX_RUNTIME_S
	return max(60, value)


def _merge_warning_text(invoice_obj: Any, warnings: Optional[List[str]]) -> Optional[str]:
	parts: list[str] = []
	raw = get_warning(invoice_obj) if invoice_obj is not None else None
	if raw:
		parts.extend(part.strip() for part in str(raw).split("|") if part.strip())
	for item in warnings or []:
		text = str(item).strip()
		if text and text not in parts:
			parts.append(text)
	joined = " | ".join(parts)
	return joined or None


def _db_retry_sleep(attempt_number: int) -> float:
	return min(2.0, 0.35 * attempt_number)


def _run_db_read_with_retry(
	operation_name: str,
	reader: Callable[[], _DbResultT],
	*,
	max_attempts: int = _DEFAULT_DB_COMMIT_RETRY_ATTEMPTS,
) -> _DbResultT:
	last_exc: Optional[OperationalError] = None
	for attempt in range(1, max_attempts + 1):
		try:
			return reader()
		except OperationalError as exc:
			last_exc = exc
			db.session.rollback()
			if attempt >= max_attempts:
				raise
			delay = _db_retry_sleep(attempt)
			logger.warning(
				"DB read selhal (%s, attempt %s/%s), opakuji za %.2fs",
				operation_name,
				attempt,
				max_attempts,
				delay,
			)
			time.sleep(delay)
		finally:
			db.session.remove()
	if last_exc is not None:
		raise last_exc
	raise RuntimeError(f"DB read selhal: {operation_name}")


def _run_db_write_with_retry(
	operation_name: str,
	writer: Callable[[], _DbResultT],
	*,
	max_attempts: int = _DEFAULT_DB_COMMIT_RETRY_ATTEMPTS,
) -> _DbResultT:
	last_exc: Optional[OperationalError] = None
	for attempt in range(1, max_attempts + 1):
		try:
			result = writer()
			db.session.commit()
			return result
		except OperationalError as exc:
			last_exc = exc
			db.session.rollback()
			if attempt >= max_attempts:
				raise
			delay = _db_retry_sleep(attempt)
			logger.warning(
				"DB commit selhal (%s, attempt %s/%s), opakuji za %.2fs",
				operation_name,
				attempt,
				max_attempts,
				delay,
			)
			time.sleep(delay)
		except Exception:
			db.session.rollback()
			raise
		finally:
			db.session.remove()
	if last_exc is not None:
		raise last_exc
	raise RuntimeError(f"DB write selhal: {operation_name}")


def _batch_heartbeat_worker(app: Flask, *, batch_id: int, stop_event: threading.Event) -> None:
	while not stop_event.wait(_DEFAULT_HEARTBEAT_INTERVAL_S):
		with app.app_context():
			try:
				should_continue = _run_db_write_with_retry(
					f"heartbeat batch {batch_id}",
					lambda: _touch_batch_heartbeat(batch_id),
				)
			except Exception:
				logger.warning("Heartbeat update selhal (batch_id=%s)", batch_id, exc_info=True)
				continue
			if not should_continue:
				return


def _touch_batch_heartbeat(batch_id: int) -> bool:
	batch = db.session.get(InvoiceBatch, batch_id)
	if batch is None:
		return False
	if _is_terminal_batch_status(batch.processing_status):
		return False
	batch.last_heartbeat_at = _utcnow()
	return True


def _extract_file_with_retry(
	extractor: InvoiceExtractor,
	pdf_path: Path,
	display_name: str,
	*,
	on_result: Optional[Callable[[ExtractResult], None]] = None,
	max_attempts: int = 2,
) -> List[ExtractResult]:
	"""Extract one PDF with one automatic retry when all attempts fail."""
	last_results: list[ExtractResult] = []
	for attempt in range(1, max_attempts + 1):
		attempt_results: list[ExtractResult] = []
		streaming_started = False

		def _collect_result(res: ExtractResult) -> None:
			nonlocal streaming_started
			res.file_path = display_name
			attempt_results.append(res)
			if on_result is None:
				return
			if not streaming_started:
				if getattr(res, "data", None) is None:
					return
				for buffered in attempt_results:
					on_result(buffered)
				streaming_started = True
				return
			on_result(res)

		try:
			file_results = _run_extraction(extractor, pdf_path, on_result=_collect_result)
		except Exception as exc:  # noqa: BLE001
			logger.exception("Chyba při extrakci PDF %s (pokus %s/%s)", display_name, attempt, max_attempts)
			from EasyFlex.extractor import ExtractResult

			file_results = [ExtractResult(file_path=display_name, data=None, error=str(exc))]
			attempt_results = file_results
		if attempt_results and not file_results:
			file_results = attempt_results
		for res in file_results:
			res.file_path = display_name
		last_results = file_results
		has_success = any(getattr(res, "data", None) is not None for res in file_results)
		if on_result is not None and (has_success or attempt >= max_attempts) and not streaming_started:
			for res in file_results:
				on_result(res)
		if has_success or attempt >= max_attempts:
			return file_results
		logger.warning("PDF %s selhalo bez výsledku, opakuji pokus %s/%s", display_name, attempt + 1, max_attempts)
	return last_results


def _build_batch_summary(
	batch: InvoiceBatch,
	*,
	stop_reason: Optional[str],
	auto_import_note: Optional[str] = None,
	extra_note: Optional[str] = None,
) -> str:
	total = int(batch.total_files or 0)
	processed = int(batch.processed_files or 0)
	success = int(batch.success_count or 0)
	errors = int(batch.error_count or 0)
	charged = int(batch.credits_charged or 0)
	processed_invoices = int(batch.processed_invoices or 0)
	total_invoices = max(int(batch.total_invoices_estimate or 0), processed_invoices)
	if stop_reason == "timeout":
		prefix = "Zpracování bylo ukončeno časovým limitem."
	elif stop_reason == "credits":
		prefix = "Zpracování bylo částečně dokončeno – došly kredity."
	elif stop_reason == "db_error":
		prefix = "Zpracování bylo ukončeno kvůli výpadku databáze. Dílčí výsledky byly zachovány."
	elif stop_reason == "interrupted":
		prefix = "Zpracování bylo přerušeno."
	elif stop_reason == "failed":
		prefix = "Zpracování dávky selhalo."
	else:
		prefix = "Zpracování dávky dokončeno."
	msg = (
		f"{prefix} Zpracováno {processed}/{total} PDF, úspěšně {success} faktur, "
		f"chyb {errors}, odečteno {charged} kreditů."
	)
	if total_invoices > 0:
		msg += f" Průběh faktur: {processed_invoices}/{total_invoices}."
	if auto_import_note:
		msg += f" {auto_import_note}"
	if extra_note:
		msg += f" {extra_note}"
	return msg


def _batch_progress_payload(batch: InvoiceBatch, *, row_count: Optional[int] = None) -> dict[str, Any]:
	status = (batch.processing_status or BATCH_STATUS_COMPLETED).strip().lower()
	total = max(0, int(batch.total_files or 0))
	processed = max(0, int(batch.processed_files or 0))
	success = max(0, int(batch.success_count or 0))
	errors = max(0, int(batch.error_count or 0))
	charged = max(0, int(batch.credits_charged or 0))
	remaining = max(total - processed, 0)
	processed_invoices = max(0, int(batch.processed_invoices or 0))
	total_invoices_estimate = max(total, int(batch.total_invoices_estimate or 0))
	if processed_invoices > total_invoices_estimate:
		total_invoices_estimate = processed_invoices
	remaining_invoices = max(total_invoices_estimate - processed_invoices, 0)
	phase = (batch.current_phase or "").strip().lower()
	if total > 0:
		progress_pct = int(min(100, round((processed / total) * 100)))
	elif _is_terminal_batch_status(status):
		progress_pct = 100
	else:
		progress_pct = 0
	if total_invoices_estimate > 0:
		invoice_progress_pct = int(min(100, round((processed_invoices / total_invoices_estimate) * 100)))
	elif _is_terminal_batch_status(status):
		invoice_progress_pct = 100
	else:
		invoice_progress_pct = 0
	row_count_value = int(
		row_count
		if row_count is not None
		else (
			db.session.query(db.func.count(InvoiceRow.id))
			.filter(InvoiceRow.batch_id == batch.id)
			.scalar()
			or 0
		)
	)
	selected_count = max(0, int(batch.selected_count or 0))
	imported_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.import_status == "imported")
		.scalar()
		or 0
	)
	import_failed_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.import_status == "failed")
		.scalar()
		or 0
	)
	import_skipped_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.import_status == "skipped")
		.scalar()
		or 0
	)
	active_job = active_job_for_batch(batch.id)
	active_job_type = active_job.job_type if active_job is not None else (batch.active_job_type or None)
	active_job_status = active_job.status if active_job is not None else None
	summary_message = (
		(active_job.summary_message or "").strip()
		if active_job is not None and active_job.summary_message
		else (batch.summary_message or "")
	)
	last_heartbeat = None
	if active_job is not None and active_job.last_heartbeat_at:
		last_heartbeat = active_job.last_heartbeat_at.isoformat()
	elif batch.last_heartbeat_at:
		last_heartbeat = batch.last_heartbeat_at.isoformat()
	return {
		"batch_id": batch.id,
		"status": status,
		"status_label": _batch_status_label(status),
		"is_terminal": _is_terminal_batch_status(status),
		"total_files": total,
		"processed_files": processed,
		"remaining_files": remaining,
		"processed_invoices": processed_invoices,
		"total_invoices_estimate": total_invoices_estimate,
		"remaining_invoices": remaining_invoices,
		"success_count": success,
		"error_count": errors,
		"credits_charged": charged,
		"progress_percent": progress_pct,
		"invoice_progress_percent": invoice_progress_pct,
		"current_phase": phase,
		"current_phase_label": _batch_phase_label(phase),
		"summary_message": summary_message,
		"row_count": row_count_value,
		"selected_count": selected_count,
		"imported_count": int(imported_count),
		"import_failed_count": int(import_failed_count),
		"import_skipped_count": int(import_skipped_count),
		"active_job_type": active_job_type,
		"active_job_status": active_job_status,
		"active_job_status_label": job_status_label(active_job_status) if active_job_status else None,
		"started_at": batch.started_at.isoformat() if batch.started_at else None,
		"finished_at": batch.finished_at.isoformat() if batch.finished_at else None,
		"last_heartbeat_at": last_heartbeat,
	}


def _start_batch_run(*, batch_id: int, user_id: int, file_count: int) -> bool:
	batch = db.session.get(InvoiceBatch, batch_id)
	user = db.session.get(User, user_id)
	if batch is None or user is None:
		return False
	batch.processing_status = BATCH_STATUS_RUNNING
	batch.started_at = _utcnow()
	batch.last_heartbeat_at = _utcnow()
	batch.current_phase = "initializing"
	if int(batch.total_files or 0) <= 0:
		batch.total_files = file_count
	min_estimate = max(1, int(batch.total_files or file_count)) if file_count > 0 else 0
	batch.total_invoices_estimate = max(int(batch.total_invoices_estimate or 0), min_estimate)
	batch.summary_message = f"Spuštěno zpracování dávky ({batch.total_files} PDF)."
	return True


def _load_user_cfg(user_id: int):
	user = db.session.get(User, user_id)
	if user is None:
		return None
	return get_user_config_for_user(user)


def _load_user_credits(user_id: int) -> int:
	user = db.session.get(User, user_id)
	if user is None:
		return 0
	return max(0, int(user.credits or 0))


def _update_batch_phase(*, batch_id: int, phase: str, summary: Optional[str] = None) -> bool:
	batch = db.session.get(InvoiceBatch, batch_id)
	if batch is None:
		return False
	batch.current_phase = phase
	batch.last_heartbeat_at = _utcnow()
	if summary is not None:
		batch.summary_message = summary
	return True


def _update_invoice_estimate_for_file(
	*,
	batch_id: int,
	file_invoice_estimate: int,
	remaining_files: int,
	source_label: str,
) -> bool:
	batch = db.session.get(InvoiceBatch, batch_id)
	if batch is None:
		return False
	processed_invoices = int(batch.processed_invoices or 0)
	minimum_estimate = processed_invoices + max(1, int(file_invoice_estimate or 1)) + max(0, remaining_files)
	batch.total_invoices_estimate = max(int(batch.total_invoices_estimate or 0), minimum_estimate)
	batch.current_phase = "persisting"
	batch.last_heartbeat_at = _utcnow()
	batch.summary_message = f"Ukládám výsledky z PDF: {source_label}"
	return True


def _persist_invoice_row(
	*,
	batch_id: int,
	user_id: int,
	row_index: int,
	source: str,
	invoice_data: Dict[str, Any],
	warning_text: Optional[str],
	row_error: Optional[str],
) -> Dict[str, bool]:
	batch = db.session.get(InvoiceBatch, batch_id)
	user = db.session.get(User, user_id)
	if batch is None or user is None:
		return {
			"aborted": True,
			"row_persisted": False,
			"row_already_exists": False,
			"credits_exhausted": False,
		}
	existing_row = (
		db.session.query(InvoiceRow.id)
		.filter(InvoiceRow.batch_id == batch_id, InvoiceRow.row_index == row_index)
		.scalar()
	)
	if existing_row is not None:
		batch.last_heartbeat_at = _utcnow()
		return {
			"aborted": False,
			"row_persisted": False,
			"row_already_exists": True,
			"credits_exhausted": False,
		}

	local_invoice = dict(invoice_data or {})
	local_error = (row_error or "").strip() or None
	credits_exhausted = False

	if local_invoice:
		if int(user.credits or 0) <= 0:
			local_invoice = {}
			local_error = "Extrakce zastavena: došly kredity pro další faktury v dávce."
			credits_exhausted = True
		else:
			batch.success_count = int(batch.success_count or 0) + 1
			batch.credits_charged = int(batch.credits_charged or 0) + 1
			user.credits = max(0, int(user.credits or 0) - 1)

	if not local_invoice:
		if local_error is None:
			local_error = "Extrakce nevrátila použitelná data."
		batch.error_count = int(batch.error_count or 0) + 1

	batch.processed_invoices = int(batch.processed_invoices or 0) + 1
	batch.total_invoices_estimate = max(int(batch.total_invoices_estimate or 0), int(batch.processed_invoices or 0))
	batch.current_phase = "persisting"
	batch.last_heartbeat_at = _utcnow()

	db.session.add(
		InvoiceRow(
			batch=batch,
			row_index=row_index,
			source=source,
			invoice_data=local_invoice,
			warning=warning_text,
			error=local_error,
			marked_for_import=bool(local_invoice),
		)
	)
	return {
		"aborted": False,
		"row_persisted": True,
		"row_already_exists": False,
		"credits_exhausted": credits_exhausted,
	}


def _mark_file_processed(*, batch_id: int, processed_files_target: int, remaining_files: int) -> bool:
	batch = db.session.get(InvoiceBatch, batch_id)
	if batch is None:
		return False
	batch.processed_files = max(int(batch.processed_files or 0), int(processed_files_target or 0))
	minimum_estimate = int(batch.processed_invoices or 0) + max(0, int(remaining_files or 0))
	batch.total_invoices_estimate = max(int(batch.total_invoices_estimate or 0), minimum_estimate)
	batch.last_heartbeat_at = _utcnow()
	if not _is_terminal_batch_status(batch.processing_status):
		batch.current_phase = "extracting"
	batch.summary_message = (
		f"Průběžně uloženo {int(batch.processed_invoices or 0)} faktur, "
		f"zpracováno {batch.processed_files}/{batch.total_files} PDF."
	)
	return True


def _load_batch_snapshot(batch_id: int) -> Optional[Dict[str, int]]:
	batch = db.session.get(InvoiceBatch, batch_id)
	if batch is None:
		return None
	return {
		"total_files": int(batch.total_files or 0),
		"processed_files": int(batch.processed_files or 0),
		"success_count": int(batch.success_count or 0),
	}


def _finalize_batch_processing(
	*,
	batch_id: int,
	stop_reason: Optional[str],
	auto_import_note: Optional[str],
	extra_note: Optional[str],
) -> bool:
	batch = db.session.get(InvoiceBatch, batch_id)
	if batch is None:
		return False
	row_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch_id)
		.scalar()
		or 0
	)
	resolved_stop_reason = stop_reason
	if resolved_stop_reason is None and int(batch.processed_files or 0) < int(batch.total_files or 0):
		resolved_stop_reason = "interrupted"

	if resolved_stop_reason == "timeout":
		status = BATCH_STATUS_PARTIAL_TIMEOUT
	elif resolved_stop_reason == "credits":
		status = BATCH_STATUS_COMPLETED_WITH_ERRORS
	elif resolved_stop_reason == "interrupted":
		status = BATCH_STATUS_INTERRUPTED
	elif resolved_stop_reason == "db_error":
		status = BATCH_STATUS_COMPLETED_WITH_ERRORS if int(row_count) > 0 else BATCH_STATUS_FAILED
	elif int(batch.error_count or 0) > 0:
		status = BATCH_STATUS_COMPLETED_WITH_ERRORS
	else:
		status = BATCH_STATUS_COMPLETED

	if status == BATCH_STATUS_FAILED and resolved_stop_reason != "failed":
		resolved_stop_reason = "failed"
	elif resolved_stop_reason is None:
		resolved_stop_reason = "done"

	batch.processing_status = status
	batch.current_phase = "done"
	batch.finished_at = _utcnow()
	batch.last_heartbeat_at = _utcnow()
	batch.summary_message = _build_batch_summary(
		batch,
		stop_reason=resolved_stop_reason,
		auto_import_note=auto_import_note,
		extra_note=extra_note,
	)
	return True


def _finalize_batch_crash(*, batch_id: int) -> bool:
	batch = db.session.get(InvoiceBatch, batch_id)
	if batch is None:
		return False
	row_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch_id)
		.scalar()
		or 0
	)
	if int(row_count) > 0:
		batch.processing_status = BATCH_STATUS_COMPLETED_WITH_ERRORS
		stop_reason = "interrupted"
		extra = "Proces byl neočekávaně ukončen, ale částečné výsledky zůstaly dostupné."
	else:
		batch.processing_status = BATCH_STATUS_FAILED
		stop_reason = "failed"
		extra = "Proces byl neočekávaně ukončen."
	batch.current_phase = "done"
	batch.finished_at = _utcnow()
	batch.last_heartbeat_at = _utcnow()
	batch.summary_message = _build_batch_summary(batch, stop_reason=stop_reason, extra_note=extra)
	return True


def _run_pdf_batch_job(
	app: Flask,
	*,
	batch_id: int,
	user_id: int,
	saved_files: List[tuple[str, str]],
	job_dir: str,
	runtime_limit_s: int,
) -> None:
	"""Background worker that processes one uploaded PDF batch."""
	started_perf = time.perf_counter()
	stop_reason: Optional[str] = None
	auto_import_note: Optional[str] = None
	extra_summary_note: Optional[str] = None
	heartbeat_stop = threading.Event()
	heartbeat_thread = threading.Thread(
		target=_batch_heartbeat_worker,
		name=f"pdf-batch-heartbeat-{batch_id}",
		daemon=True,
		kwargs={"app": app, "batch_id": batch_id, "stop_event": heartbeat_stop},
	)
	heartbeat_thread.start()

	try:
		with app.app_context():
			started_ok = _run_db_write_with_retry(
				f"batch {batch_id} start",
				lambda: _start_batch_run(batch_id=batch_id, user_id=user_id, file_count=len(saved_files)),
			)
			if not started_ok:
				return

			cfg = _run_db_read_with_retry(
				f"batch {batch_id} load user config",
				lambda: _load_user_cfg(user_id),
			)
			if cfg is None:
				return
			extractor = InvoiceExtractor(config=cfg)

			last_row_index = _run_db_read_with_retry(
				f"batch {batch_id} load row index",
				lambda: db.session.query(db.func.max(InvoiceRow.row_index))
				.filter(InvoiceRow.batch_id == batch_id)
				.scalar(),
			)
			next_row_index = int(last_row_index) + 1 if last_row_index is not None else 0
			total_files = len(saved_files)

			for file_idx, (file_path_str, display_name) in enumerate(saved_files, start=1):
				elapsed = time.perf_counter() - started_perf
				if runtime_limit_s > 0 and elapsed >= runtime_limit_s:
					stop_reason = "timeout"
					break

				try:
					_run_db_write_with_retry(
						f"batch {batch_id} heartbeat before file",
						lambda: _update_batch_phase(
							batch_id=batch_id,
							phase="extracting",
							summary=(
								f"Zpracovávám PDF {file_idx}/{total_files}: {display_name}. "
								"Výsledky budou ukládány po jednotlivých fakturách."
							),
						),
					)
				except OperationalError as exc:
					stop_reason = "db_error"
					extra_summary_note = f"DB chyba při aktualizaci průběhu: {exc}"
					break

				credits_available = _run_db_read_with_retry(
					f"batch {batch_id} check credits",
					lambda: _load_user_credits(user_id),
				)
				if credits_available <= 0:
					stop_reason = "credits"
					break

				remaining_files_after = max(total_files - file_idx, 0)
				file_estimate_locked = False

				def _persist_stream_result(res: ExtractResult) -> None:
					nonlocal next_row_index, stop_reason, extra_summary_note, file_estimate_locked
					if stop_reason in {"db_error", "credits", "timeout", "interrupted"}:
						return
					elapsed_local = time.perf_counter() - started_perf
					if runtime_limit_s > 0 and elapsed_local >= runtime_limit_s:
						stop_reason = "timeout"
						return

					group_total_hint = int(getattr(res, "_invoice_group_total", 0) or 0)
					if group_total_hint > 0 and not file_estimate_locked:
						try:
							_run_db_write_with_retry(
								f"batch {batch_id} update streaming invoice estimate",
								lambda: _update_invoice_estimate_for_file(
									batch_id=batch_id,
									file_invoice_estimate=group_total_hint,
									remaining_files=remaining_files_after,
									source_label=display_name,
								),
							)
							file_estimate_locked = True
						except OperationalError as exc:
							stop_reason = "db_error"
							extra_summary_note = f"DB chyba při stream aktualizaci odhadu: {exc}"
							return

					res.file_path = display_name
					data_obj_local = getattr(res, "data", None)
					row_error_local = (getattr(res, "error", None) or "").strip() or None
					warning_text_local = _merge_warning_text(data_obj_local, getattr(res, "warnings", None))
					invoice_payload_local: Dict[str, Any] = {}
					if data_obj_local is not None:
						try:
							invoice_payload_local = data_obj_local.model_dump()
						except Exception:
							invoice_payload_local = {}
						if warning_text_local and invoice_payload_local:
							set_warning(invoice_payload_local, warning_text_local)

					row_index_for_insert = next_row_index
					try:
						persist_info = _run_db_write_with_retry(
							f"batch {batch_id} persist row {row_index_for_insert}",
							lambda: _persist_invoice_row(
								batch_id=batch_id,
								user_id=user_id,
								row_index=row_index_for_insert,
								source=display_name,
								invoice_data=invoice_payload_local,
								warning_text=warning_text_local,
								row_error=row_error_local,
							),
						)
					except OperationalError as exc:
						stop_reason = "db_error"
						extra_summary_note = f"DB chyba při ukládání výsledku: {exc}"
						return

					if persist_info.get("aborted"):
						stop_reason = "interrupted"
						return
					if persist_info.get("row_persisted") or persist_info.get("row_already_exists"):
						next_row_index += 1
					if persist_info.get("credits_exhausted"):
						stop_reason = "credits"

				db.session.remove()
				file_results = _extract_file_with_retry(
					extractor,
					Path(file_path_str),
					display_name,
					on_result=_persist_stream_result,
					max_attempts=2,
				)
				if not file_results:
					file_results = [
						ExtractResult(
							file_path=display_name,
							data=None,
							error="Extrakce nevrátila žádná data.",
						)
					]

				file_invoice_estimate = max(1, len(file_results))
				try:
					_run_db_write_with_retry(
						f"batch {batch_id} update invoice estimate",
						lambda: _update_invoice_estimate_for_file(
							batch_id=batch_id,
							file_invoice_estimate=file_invoice_estimate,
							remaining_files=remaining_files_after,
							source_label=display_name,
						),
					)
				except OperationalError as exc:
					stop_reason = "db_error"
					extra_summary_note = f"DB chyba při aktualizaci odhadu faktur: {exc}"
					break

				if stop_reason == "db_error":
					break

				target_processed_files = min(total_files, file_idx)
				try:
					_run_db_write_with_retry(
						f"batch {batch_id} mark file processed",
						lambda: _mark_file_processed(
							batch_id=batch_id,
							processed_files_target=target_processed_files,
							remaining_files=max(total_files - target_processed_files, 0),
						),
					)
				except OperationalError as exc:
					stop_reason = "db_error"
					extra_summary_note = f"DB chyba při ukládání průběhu souboru: {exc}"
					break

				if stop_reason in {"credits", "timeout", "interrupted"}:
					break

			batch_state = _run_db_read_with_retry(
				f"batch {batch_id} final state snapshot",
				lambda: _load_batch_snapshot(batch_id),
			)
			if batch_state is None:
				return

			if stop_reason is None and batch_state["processed_files"] < batch_state["total_files"]:
				stop_reason = "interrupted"

			if stop_reason != "db_error" and getattr(cfg, "auto_import", False) and batch_state["success_count"] > 0:
				try:
					_run_db_write_with_retry(
						f"batch {batch_id} set importing phase",
						lambda: _update_batch_phase(
							batch_id=batch_id,
							phase="importing",
							summary="Probíhá automatický import do ABRA.",
						),
					)
					batch_for_import = db.session.get(InvoiceBatch, batch_id)
					user_for_import = db.session.get(User, user_id)
					if batch_for_import is not None and user_for_import is not None:
						company_code, direction, doc_type_code = current_context(user_for_import.settings, base_cfg=cfg)
						apply_context_to_config(cfg, company_code, direction, doc_type_code)
						ok, err, skipped = _import_batch(batch_for_import, cfg, None)
						auto_import_note = f"Auto-import: {ok} OK, {err} chyb, {skipped} přeskočeno."
				finally:
					db.session.remove()

			_run_db_write_with_retry(
				f"batch {batch_id} finalize",
				lambda: _finalize_batch_processing(
					batch_id=batch_id,
					stop_reason=stop_reason,
					auto_import_note=auto_import_note,
					extra_note=extra_summary_note,
				),
			)
	except Exception as exc:  # noqa: BLE001
		logger.exception("PDF batch worker selhal (batch_id=%s): %s", batch_id, exc)
		with app.app_context():
			try:
				_run_db_write_with_retry(
					f"batch {batch_id} crash finalize",
					lambda: _finalize_batch_crash(batch_id=batch_id),
				)
			except Exception:  # noqa: BLE001
				db.session.rollback()
	finally:
		heartbeat_stop.set()
		heartbeat_thread.join(timeout=1.0)
		with _BATCH_THREADS_LOCK:
			_BATCH_THREADS.pop(batch_id, None)
		shutil.rmtree(job_dir, ignore_errors=True)


def _start_pdf_batch_job(
	app: Flask,
	*,
	batch_id: int,
	user_id: int,
	saved_files: List[tuple[str, str]],
	job_dir: str,
	runtime_limit_s: int,
) -> None:
	thread = threading.Thread(
		target=_run_pdf_batch_job,
		name=f"pdf-batch-{batch_id}",
		daemon=True,
		kwargs={
			"app": app,
			"batch_id": batch_id,
			"user_id": user_id,
			"saved_files": saved_files,
			"job_dir": job_dir,
			"runtime_limit_s": runtime_limit_s,
		},
	)
	with _BATCH_THREADS_LOCK:
		_BATCH_THREADS[batch_id] = thread
	thread.start()


def _import_batch(batch: InvoiceBatch, cfg: Any, selected_ids: Optional[List[int]]) -> tuple[int, int, int]:
	"""Import selected rows to ABRA using current config."""
	from EasyFlex.abra import import_to_abra

	if selected_ids is None:
		selected_ids = [row.id for row in batch.rows if row.marked_for_import]
	selected_set = {int(val) for val in selected_ids}
	success = 0
	error = 0
	skipped = 0
	processed = 0
	slowest_row = None
	slowest_s = 0.0
	for row in sorted(batch.rows, key=lambda r: r.row_index):
		row.marked_for_import = row.id in selected_set
		if not row.marked_for_import:
			continue
		inv_dict: Dict[str, Any] = dict(row.invoice_data or {})
		if not _has_minimal_invoice_data(inv_dict):
			skipped += 1
			continue
		try:
			payload = InvoiceData.model_validate(inv_dict)
		except Exception:
			payload = inv_dict

		# Pro import chceme používat stejný tvar jako desktop (plain dict) a mít jasno,
		# jaká data o odběrateli posíláme.
		try:
			if isinstance(payload, InvoiceData):
				payload_dict: Dict[str, Any] = payload.model_dump()
			else:
				payload_dict = dict(payload)
		except Exception:
			payload_dict = inv_dict

		partner_preview = {
			"odberatel_jmeno": payload_dict.get("odberatel_jmeno"),
			"odberatel_ic": payload_dict.get("odberatel_ic"),
			"odberatel_dic": payload_dict.get("odberatel_dic"),
		}
		logger.info("Import ABRA řádek %s – odběratel %s", row.row_index, partner_preview)
		start = time.perf_counter()
		try:
			resp = import_to_abra(payload_dict, cfg=cfg)
			if isinstance(resp, dict) and resp.get("__status") == "skipped-duplicate":
				row.status = "Přeskočeno (duplicitní kód)"
				row.error = None
				skipped += 1
				continue
			if isinstance(resp, dict) and resp.get("__status") == "skipped-duplicate-foreign":
				row.status = "Přeskočeno (kód patří jinému dokladu)"
				row.error = None
				skipped += 1
				continue
			if isinstance(resp, dict) and resp.get("__status") == "failed-duplicate-not-found":
				row.status = None
				row.error = "Duplicitní kód – ABRA nenašla doklad podle kódu."
				error += 1
				continue
			if isinstance(resp, dict) and resp.get("__status") == "failed-duplicate-unverifiable":
				row.status = None
				row.error = "Duplicitní kód – nelze ověřit ext-id, doklad neimportován."
				error += 1
				continue
			if isinstance(resp, dict) and resp.get("__status") == "failed-duplicate-update":
				row.status = None
				row.error = "Duplicitní kód – update selhal."
				error += 1
				continue
			if resp is None:
				raise RuntimeError("Import se nepodařil (zkontrolujte logy).")
			if isinstance(resp, dict) and resp.get("__status") == "updated":
				row.status = "Importováno (aktualizováno)"
			else:
				row.status = "Importováno"
			row.error = None
			success += 1
		except Exception as exc:  # noqa: BLE001
			logger.exception("Chyba importu do ABRA")
			row.error = str(exc)
			row.status = None
			error += 1
		finally:
			elapsed = time.perf_counter() - start
			processed += 1
			if elapsed > slowest_s:
				slowest_s = elapsed
				slowest_row = row.row_index
			logger.info("Import ABRA řádek %s dokončen za %.2fs", row.row_index, elapsed)
	db.session.commit()
	if processed:
		logger.info(
			"Import ABRA souhrn: zpracováno %s faktur (OK=%s, chyba=%s, přeskočeno=%s); "
			"nejdéle trvala řádek %s (%.2fs)",
			processed,
			success,
			error,
			skipped,
			slowest_row,
			slowest_s,
		)
	else:
		logger.info("Import ABRA souhrn: zpracováno 0 faktur.")
	return success, error, skipped


def create_app() -> Flask:
	load_dotenv()
	app = Flask(__name__)
	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
	base_dir = Path(__file__).resolve().parent
	db_path = base_dir / "easyflex_web.db"
	app_env = (os.getenv("EASYFLEX_ENV") or os.getenv("FLASK_ENV") or "").strip().lower()
	secret_key = os.getenv("EASYFLEX_SECRET_KEY")
	if secret_key:
		app.config["SECRET_KEY"] = secret_key
	elif app_env == "production":
		raise RuntimeError("EASYFLEX_SECRET_KEY must be set when EASYFLEX_ENV=production.")
	else:
		app.config["SECRET_KEY"] = "dev-secret-key"

	# Pokud je nastaveno DATABASE_URL (např. z Render Postgres), použij ho,
	# jinak fallback na lokální SQLite soubor easyflex_web.db
	db_url = os.getenv("DATABASE_URL")
	if db_url:
		# Render dává URL ve tvaru "postgres://", SQLAlchemy očekává "postgresql://"
		if db_url.startswith("postgres://"):
			db_url = db_url.replace("postgres://", "postgresql://", 1)
		app.config["SQLALCHEMY_DATABASE_URI"] = db_url
		if db_url.startswith("postgresql://"):
			app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
				"pool_pre_ping": True,
				"pool_recycle": 300,
			}
	else:
		app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
	app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
	app.config["MAX_CONTENT_LENGTH"] = _pdf_upload_limits()["max_request_bytes"]
	app.config["SESSION_COOKIE_HTTPONLY"] = True
	app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
	if app_env == "production":
		app.config["SESSION_COOKIE_SECURE"] = True

	# Load shared EasyFlex configuration (per-user overrides are applied later)
	app.config["EASYFLEX_BASE_CONFIG"] = load_config()

	init_db(app)
	init_auth(app)

	@app.context_processor
	def inject_credit_settings():
		return {
			"starting_credits": STARTING_CREDITS,
			"pdf_invoice_credit_cost": PDF_INVOICE_CREDIT_COST,
			"table_upload_credit_cost": TABLE_UPLOAD_CREDIT_COST,
			"price_per_credit": PRICE_PER_CREDIT,
			"min_credit_purchase": MIN_CREDIT_PURCHASE,
		}

	@app.route("/")
	@login_required
	def dashboard():
		return render_template("dashboard.html")

	@app.get("/healthz")
	def healthz():
		return jsonify({"status": "ok"}), 200

	@app.route("/napoveda")
	def help_page():
		return render_template(
			"help.html",
			contact_email="vvitovec27@gmail.com",
			contact_phone="+420 774 943 304",
		)

	@app.route("/credits")
	@login_required
	def credits():
		return render_template(
			"credits.html",
			account_number="254980360/0600",
			price_per_credit=PRICE_PER_CREDIT,
			min_purchase=MIN_CREDIT_PURCHASE,
		)

	@app.route("/assets/qr-payment")
	@login_required
	def qr_payment():
		base_dir = Path(__file__).resolve().parent
		image_path = base_dir.parent / "assets" / "qr_payment.jpeg"
		if not image_path.exists():
			abort(404)
		return send_file(image_path, mimetype="image/jpeg")

	@app.route("/upload-pdf", methods=["GET", "POST"])
	@login_required
	def upload_pdf():
		cfg = get_user_config()
		segmentation_enabled = bool(getattr(cfg, "enable_multi_invoice_segmentation", False))
		ensure_seed_data(current_user, cfg)
		if request.method == "POST":
			segmentation_enabled = bool(request.form.get("enable_multi_invoice_segmentation"))
			cfg.enable_multi_invoice_segmentation = segmentation_enabled
			settings = getattr(current_user, "settings", None)
			if settings is not None:
				overrides = dict(settings.config_overrides or {})
				if overrides.get("enable_multi_invoice_segmentation") != segmentation_enabled:
					overrides["enable_multi_invoice_segmentation"] = segmentation_enabled
					settings.config_overrides = overrides
					db.session.commit()
			if not cfg.openai_api_key:
				flash("Nejprve vyplňte svůj OpenAI API klíč v Nastavení.", "warning")
				return redirect(url_for("user_settings"))
			if current_user.credits <= 0:
				flash("Nemáte žádné kredity. Dokupte si je v sekci Kredity.", "danger")
				return redirect(url_for("credits"))
			uploaded_files = [f for f in request.files.getlist("pdfs") if f and f.filename]
			if not uploaded_files:
				fallback = request.files.get("pdf")
				if fallback and fallback.filename:
					uploaded_files = [fallback]
			if not uploaded_files:
				flash("Vyberte prosím alespoň jeden PDF soubor nebo složku.", "warning")
				return render_template("upload_pdf.html", cfg=cfg)
			pdf_files = []
			skipped_non_pdf: list[str] = []
			for item in uploaded_files:
				if (item.filename or "").lower().endswith(".pdf"):
					pdf_files.append(item)
				else:
					skipped_non_pdf.append(item.filename or "")
			if skipped_non_pdf:
				flash(
					"Následující soubory byly přeskočeny (nejsou PDF): "
					+ ", ".join(filter(None, skipped_non_pdf)),
					"warning",
				)
			if not pdf_files:
				flash("V nahraných souborech není žádné PDF.", "warning")
				return render_template("upload_pdf.html", cfg=cfg)
			limits = _pdf_upload_limits()
			if len(pdf_files) > limits["max_files"]:
				flash(
					f"V jedné dávce lze nahrát maximálně {limits['max_files']} PDF. "
					f"Vybráno: {len(pdf_files)}.",
					"danger",
				)
				return render_template("upload_pdf.html", cfg=cfg)
			display_names: list[str] = []
			used_names: set[str] = set()
			saved_files: list[dict[str, Any]] = []
			total_bytes = 0
			total_pages = 0
			source_label = _derive_batch_label([_normalize_upload_label(item.filename, idx) for idx, item in enumerate(pdf_files)])
			batch = InvoiceBatch(
				user=current_user,
				source_label=source_label,
				source_type="pdf",
				processing_status=BATCH_STATUS_QUEUED,
				total_files=len(pdf_files),
				processed_files=0,
				processed_invoices=0,
				total_invoices_estimate=max(1, len(pdf_files)),
				current_phase="queued",
				success_count=0,
				error_count=0,
				credits_charged=0,
				selected_count=0,
				active_job_type=JOB_TYPE_EXTRACT_PDF,
				summary_message=f"Dávka čeká na worker ({len(pdf_files)} PDF).",
				last_heartbeat_at=_utcnow(),
			)
			db.session.add(batch)
			db.session.commit()
			storage_root = batch_storage_root(batch.id)
			try:
				for idx, storage in enumerate(pdf_files):
					display_name = _normalize_upload_label(storage.filename, idx)
					display_names.append(display_name)
					save_name = _dedupe_filename(display_name, used_names, fallback=f"upload-{idx + 1}.pdf")
					pdf_path = storage_root / save_name
					storage.save(pdf_path)
					size_bytes = int(pdf_path.stat().st_size)
					total_bytes += size_bytes
					if size_bytes > limits["max_file_bytes"]:
						raise ValueError(
							f"Soubor {display_name} je příliš velký ({size_bytes} B). "
							f"Limit je {limits['max_file_bytes']} B."
						)
					if total_bytes > limits["max_total_bytes"]:
						raise ValueError(
							f"Celková velikost dávky překročila limit {limits['max_total_bytes']} B."
						)
					page_count = _read_pdf_page_count(pdf_path, poppler_path=getattr(cfg, "poppler_path", None))
					if page_count > 0:
						total_pages += page_count
						if total_pages > limits["max_total_pages"]:
							raise ValueError(
								f"Dávka překračuje limit {limits['max_total_pages']} stran PDF."
							)
					saved_files.append(
						{
							"relative_path": save_name,
							"display_name": display_name,
							"size_bytes": size_bytes,
							"page_count": page_count,
						}
					)
			except Exception as exc:  # noqa: BLE001
				logger.exception("Nepodařilo se uložit/validovat nahraná PDF.")
				shutil.rmtree(storage_root, ignore_errors=True)
				db.session.delete(batch)
				db.session.commit()
				flash(f"Nepodařilo se připravit PDF dávku: {exc}", "danger")
				return render_template("upload_pdf.html", cfg=cfg)
			persist_batch_payload(
				batch.id,
				{
					"batch_id": batch.id,
					"source_label": source_label,
					"files": saved_files,
					"runtime_limit_s": _load_batch_runtime_limit_s(),
				},
			)
			try:
				queue_batch_job(
					batch_id=batch.id,
					user_id=current_user.id,
					job_type=JOB_TYPE_EXTRACT_PDF,
					payload={"files": saved_files, "runtime_limit_s": _load_batch_runtime_limit_s()},
					priority=10,
					max_attempts=3,
				)
				db.session.commit()
			except Exception as exc:  # noqa: BLE001
				logger.exception("Nepodařilo se zařadit PDF batch do fronty.")
				batch.processing_status = BATCH_STATUS_FAILED
				batch.finished_at = _utcnow()
				batch.last_heartbeat_at = _utcnow()
				batch.active_job_type = None
				batch.summary_message = f"Zařazení dávky do fronty selhalo: {exc}"
				db.session.commit()
				shutil.rmtree(storage_root, ignore_errors=True)
				flash("Nepodařilo se spustit zpracování na pozadí. Zkuste to prosím znovu.", "danger")
				return redirect(url_for("view_results", batch_id=batch.id))

			flash(
				f"Extrakce byla zařazena do fronty pro {len(saved_files)} PDF. "
				"Worker bude výsledky průběžně doplňovat.",
				"info",
			)
			return redirect(url_for("view_results", batch_id=batch.id))
		cfg.enable_multi_invoice_segmentation = segmentation_enabled
		return render_template("upload_pdf.html", cfg=cfg)

	@app.route("/upload-table", methods=["GET", "POST"])
	@login_required
	def upload_table():
		cfg = get_user_config()
		ensure_seed_data(current_user, cfg)
		if request.method == "POST":
			if current_user.credits <= 0:
				flash("Nemáte žádné kredity. Dokupte si je v sekci Kredity.", "danger")
				return redirect(url_for("credits"))
			file = request.files.get("table")
			if file is None or file.filename == "":
				flash("Vyberte prosím CSV/XLSX/XML soubor.", "warning")
				return render_template("upload_table.html")
			if request.content_length and request.content_length > _max_table_file_bytes():
				flash("Nahraný tabulkový soubor je příliš velký.", "danger")
				return render_template("upload_table.html")
			filename = secure_filename(file.filename) or "tabulka"
			with tempfile.TemporaryDirectory() as tmpdir:
				table_path = Path(tmpdir) / filename
				file.save(table_path)
				if table_path.stat().st_size > _max_table_file_bytes():
					flash("Tabulkový soubor překračuje povolený limit velikosti.", "danger")
					return render_template("upload_table.html")
				processor = _build_csv_processor(cfg)
				try:
					invoices = processor.process_table_file(str(table_path))
				except Exception as exc:  # noqa: BLE001
					logger.exception("Chyba při zpracování tabulky")
					return render_template("upload_table.html", error=str(exc))
			invoice_count = len(invoices)
			credit_cost = TABLE_UPLOAD_CREDIT_COST if invoice_count > 0 else 0
			if invoice_count == 0:
				flash("V tabulce nebyly nalezeny žádné faktury.", "warning")
				return render_template("upload_table.html")
			if current_user.credits < credit_cost:
				flash(
					f"Pro zpracování tabulky potřebujete {credit_cost} kredity, "
					f"ale k dispozici máte {current_user.credits}. Dokupte kredity a zkuste to znovu.",
					"danger",
				)
				return redirect(url_for("credits"))
			batch = create_batch_from_invoices(current_user, invoices, filename, source_type="table")
			remaining = _deduct_credits(current_user, credit_cost)
			batch.credits_charged = credit_cost
			batch.summary_message = (
				f"Z tabulky načteno {invoice_count} faktur. "
				f"Odečteno {credit_cost} kreditů."
			)
			db.session.commit()
			flash(
				f"Z tabulky načteno {invoice_count} faktur. Odečteno {credit_cost} kreditů. "
				f"Aktuální zůstatek: {remaining}.",
				"info",
			)
			if getattr(cfg, "auto_import", False):
				company_code, direction, doc_type_code = current_context(current_user.settings, base_cfg=cfg)
				queue_batch_job(
					batch_id=batch.id,
					user_id=current_user.id,
					job_type=JOB_TYPE_IMPORT_ABRA,
					payload={
						"company_code": company_code,
						"direction": direction,
						"doc_type_code": doc_type_code,
						"auto_import": True,
					},
					priority=5,
					max_attempts=3,
				)
				batch.processing_status = BATCH_STATUS_WAITING_IMPORT
				batch.current_phase = "waiting_import"
				batch.active_job_type = JOB_TYPE_IMPORT_ABRA
				db.session.commit()
				flash("Auto-import byl zařazen do fronty.", "info")
			return redirect(url_for("view_results", batch_id=batch.id))
		return render_template("upload_table.html")

	@app.route("/results/<int:batch_id>")
	@login_required
	def view_results(batch_id: int):
		cfg = get_user_config()
		ensure_seed_data(current_user, cfg)
		batch = load_batch_for_user(batch_id, current_user)
		if batch is None:
			abort(404)
		page = max(1, request.args.get("page", default=1, type=int) or 1)
		page_size = min(MAX_PAGE_SIZE, max(1, request.args.get("page_size", default=DEFAULT_PAGE_SIZE, type=int) or DEFAULT_PAGE_SIZE))
		sort_key = (request.args.get("sort") or "row_index").strip() or "row_index"
		sort_direction = (request.args.get("sort_dir") or "asc").strip().lower()
		filter_name = (request.args.get("filter") or "all").strip().lower() or "all"
		row_items, total_rows = query_rows_for_batch(
			batch,
			page=page,
			page_size=page_size,
			sort_key=sort_key,
			sort_direction=sort_direction,
			filter_name=filter_name,
		)
		rows = rows_for_display(batch, row_items)
		settings = current_user.settings
		company_code, direction, doc_type_code = current_context(settings, base_cfg=cfg)
		company_override = request.args.get("company") or request.args.get("company_code") or None
		direction_override = request.args.get("direction") or request.args.get("direction_code") or None
		if company_override:
			company_code = company_override
		if direction_override:
			direction = direction_override or direction
		companies = list_companies(current_user)
		doc_types = list_doc_types(current_user, company_code, direction)
		if doc_type_code and not any(dt.code == doc_type_code for dt in doc_types):
			doc_type_code = None
		progress = _batch_progress_payload(batch, row_count=total_rows)
		page_count = max(1, (int(total_rows) + page_size - 1) // page_size) if total_rows else 1
		return render_template(
			"results.html",
			batch=batch,
			rows=rows,
			batch_progress=progress,
			allow_import=True,
			columns=DISPLAY_COLUMNS,
			column_labels=COLUMN_LABELS,
			companies=companies,
			doc_types=doc_types,
			selected_company=company_code,
			selected_direction=direction,
			selected_doc_type=doc_type_code,
			page=page,
			page_size=page_size,
			page_count=page_count,
			total_rows=total_rows,
			sort_key=sort_key,
			sort_direction=sort_direction,
			filter_name=filter_name,
		)

	@app.route("/results/<int:batch_id>/progress")
	@login_required
	def batch_progress(batch_id: int):
		batch = InvoiceBatch.query.filter_by(id=batch_id, user_id=current_user.id).first()
		if batch is None:
			abort(404)
		row_count = db.session.query(db.func.count(InvoiceRow.id)).filter(InvoiceRow.batch_id == batch.id).scalar() or 0
		return jsonify(_batch_progress_payload(batch, row_count=int(row_count)))

	@app.route("/results/<int:batch_id>/selection", methods=["POST"])
	@login_required
	def update_batch_selection(batch_id: int):
		batch = load_batch_for_user(batch_id, current_user)
		if batch is None:
			abort(404)
		payload = request.get_json(silent=True) or request.form
		try:
			row_id = int(payload.get("row_id", 0))
		except (TypeError, ValueError):
			abort(400)
		marked_raw = payload.get("marked", False)
		marked = marked_raw if isinstance(marked_raw, bool) else str(marked_raw).strip().lower() in {"1", "true", "yes", "on"}
		row = InvoiceRow.query.filter_by(id=row_id, batch_id=batch.id).first_or_404()
		if row.error:
			marked = False
		row.marked_for_import = bool(marked)
		batch.selected_count = count_selected_rows(batch.id)
		db.session.commit()
		return jsonify({"ok": True, "row_id": row.id, "marked": bool(row.marked_for_import), "selected_count": int(batch.selected_count or 0)})

	@app.route("/results/<int:batch_id>/selection/bulk", methods=["POST"])
	@login_required
	def bulk_update_batch_selection(batch_id: int):
		batch = load_batch_for_user(batch_id, current_user)
		if batch is None:
			abort(404)
		payload = request.get_json(silent=True) or request.form
		marked_raw = payload.get("marked", False)
		marked = marked_raw if isinstance(marked_raw, bool) else str(marked_raw).strip().lower() in {"1", "true", "yes", "on"}
		scope = (payload.get("scope") or "page").strip().lower()
		updated = 0
		query = InvoiceRow.query.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.error.is_(None))
		if scope == "filtered":
			filter_name = (payload.get("filter") or "all").strip().lower()
			query = _apply_row_filter(query, filter_name)
			rows = query.all()
		else:
			if isinstance(payload, dict):
				row_ids = payload.get("row_ids") or []
			else:
				row_ids = payload.get("row_ids") or payload.getlist("row_ids")
			normalized_ids: list[int] = []
			for row_id in row_ids:
				try:
					normalized_ids.append(int(row_id))
				except (TypeError, ValueError):
					continue
			rows = query.filter(InvoiceRow.id.in_(normalized_ids or [-1])).all()
		for row in rows:
			row.marked_for_import = bool(marked)
			updated += 1
		batch.selected_count = count_selected_rows(batch.id)
		db.session.commit()
		return jsonify({"ok": True, "updated": updated, "selected_count": int(batch.selected_count or 0)})

	@app.route("/invoice/<int:row_id>/edit", methods=["GET", "POST"])
	@login_required
	def edit_invoice(row_id: int):
		cfg = get_user_config()
		row = (
			InvoiceRow.query.join(InvoiceBatch)
			.filter(InvoiceRow.id == row_id, InvoiceBatch.user_id == current_user.id)
			.first_or_404()
		)
		invoice_data = dict(row.invoice_data or {})
		field_labels = dict(EDITABLE_FIELDS)
		warning_fields: list[str] = []
		if request.method == "POST":
			updates: Dict[str, Any] = {}
			day_first = getattr(cfg, "date_day_first", True)
			for field, _label in EDITABLE_FIELDS:
				raw = (request.form.get(field) or "").strip()
				if not raw:
					updates[field] = None
					continue
				if field in DATE_FIELDS:
					parsed = parse_invoice_date(raw, day_first=day_first)
					if parsed is None:
						updates[field] = None
						warning_fields.append(field_labels.get(field, field))
					else:
						updates[field] = parsed
				elif field in FLOAT_FIELDS:
					try:
						updates[field] = _parse_float_value(raw)
					except ValueError:
						updates[field] = None
						warning_fields.append(field_labels.get(field, field))
				else:
					updates[field] = raw
			apply_invoice_updates(row, updates, day_first=day_first)
			if warning_fields:
				flash(
					"Změny uloženy. Některé hodnoty se nepodařilo převést a byly vymazány: "
					+ ", ".join(warning_fields),
					"warning",
				)
			else:
				flash("Úprava faktury uložena.", "success")
			return redirect(url_for("view_results", batch_id=row.batch_id))
		return render_template(
			"edit_invoice.html",
			row=row,
			invoice=invoice_data,
			error=None,
			fields=EDITABLE_FIELDS,
		)

	@app.route("/settings", methods=["GET", "POST"])
	@login_required
	def user_settings():
		# Ensure settings row exists (also pre-fills defaults from base config)
		cfg = get_user_config()
		if not getattr(cfg, "openai_model", None):
			cfg.openai_model = "gpt-5"
		settings = current_user.settings
		error = None
		active_tab = request.form.get("active_tab") or request.args.get("tab") or "abra"
		if not current_user.is_admin and active_tab == "extractor":
			active_tab = "abra"
		# Handle quick actions for ABRA entities
		action = request.form.get("action") if request.method == "POST" else None
		if action and settings is not None:
			active_tab = request.form.get("active_tab") or active_tab
			if action == "add_company":
				name = (request.form.get("company_name") or "").strip()
				code = (request.form.get("company_code") or "").strip()
				if not code:
					error = "Vyplňte kód firmy."
				else:
					store_company(current_user, code, name or code)
					flash(f"Firma {code} přidána.", "success")
			elif action == "delete_company":
				try:
					cid = int(request.form.get("company_id", "0"))
				except ValueError:
					cid = 0
				if not delete_company(current_user, cid):
					error = "Nepodařilo se smazat firmu."
				else:
					flash("Firma smazána.", "success")
			elif action == "add_doc_type":
				company_code = (request.form.get("dt_company_code") or "").strip()
				direction = (request.form.get("dt_direction") or "").strip() or "faktura-prijata"
				name = (request.form.get("dt_name") or "").strip()
				code = (request.form.get("dt_code") or "").strip()
				if not company_code or not code or not name:
					error = "Vyplňte firmu, směr i kód a název typu."
				else:
					store_doc_type(current_user, company_code, direction, name, code)
					flash(f"Typ dokladu {code} přidán.", "success")
			elif action == "delete_doc_type":
				try:
					did = int(request.form.get("doc_type_id", "0"))
				except ValueError:
					did = 0
				if not delete_doc_type(current_user, did):
					error = "Nepodařilo se smazat typ dokladu."
				else:
					flash("Typ dokladu smazán.", "success")
		# Main settings save
		if request.method == "POST" and settings is not None and not action:
			active_tab = request.form.get("active_tab") or active_tab
			if current_user.is_admin:
				settings.openai_api_key = (request.form.get("openai_api_key") or "").strip() or None
			else:
				settings.openai_api_key = None
			settings.abra_server = (request.form.get("abra_server") or "").strip() or None
			port_raw = (request.form.get("abra_port") or "").strip()
			if port_raw and not port_raw.isdigit():
				error = "Port musí být číslo."
			else:
				settings.abra_port = int(port_raw) if port_raw else None
			settings.abra_username = (request.form.get("abra_username") or "").strip() or None
			settings.abra_password = (request.form.get("abra_password") or "").strip() or None
			settings.abra_verify_tls = bool(request.form.get("abra_verify_tls"))
			# Work on a fresh copy so SQLAlchemy detects JSON changes on every save
			overrides = dict(settings.config_overrides or {})

			def _set_override(key: str, value):
				if value is None or value == "":
					overrides.pop(key, None)
				else:
					overrides[key] = value

			if current_user.is_admin:
				_set_override("openai_model", (request.form.get("openai_model") or "").strip() or None)
				_set_override("openai_reasoning_effort", (request.form.get("reasoning_effort") or "").strip() or None)
				for int_field, form_key in (
					("concurrency", "concurrency"),
					("max_tokens", "max_tokens"),
					("dpi", "pdf_dpi"),
					("max_pages", "max_pages"),
					("openai_timeout_s", "timeout_s"),
					("openai_connection_timeout_s", "connection_timeout_s"),
					("openai_max_retries", "max_retries"),
					("image_max_width", "image_max_width"),
					("image_jpeg_quality", "image_jpeg_quality"),
				):
					raw_val = (request.form.get(form_key) or "").strip()
					if raw_val:
						try:
							_set_override(int_field, int(raw_val))
						except ValueError:
							error = f"Pole {form_key} musí být číslo."
							break
					else:
						_set_override(int_field, None)
			if error is None:
				if current_user.is_admin:
					for float_field, form_key in (("openai_request_delay", "request_delay"),):
						raw_val = (request.form.get(form_key) or "").strip()
						if raw_val:
							try:
								_set_override(float_field, float(raw_val))
							except ValueError:
								error = f"Pole {form_key} musí být číslo."
								break
						else:
							_set_override(float_field, None)
			if error is None:
				_set_override("use_doc_number_as_variable_symbol", bool(request.form.get("use_doc_number_as_variable_symbol")))
				_set_override("infer_missing_dates", bool(request.form.get("infer_missing_dates")))
				_set_override("csv_enable_llm_mapping", bool(request.form.get("csv_enable_llm_mapping")))
				_set_override("auto_import", bool(request.form.get("auto_import")))
				date_order = (request.form.get("date_order") or "dd-mm").strip().lower()
				_set_override("date_day_first", False if date_order == "mm-dd" else True)
				if "abra_doc_endpoint" in request.form:
					_set_override("abra_doc_endpoint", (request.form.get("abra_doc_endpoint") or "").strip() or None)
				if "abra_doc_type_code" in request.form:
					_set_override("abra_doc_type_code", (request.form.get("abra_doc_type_code") or "").strip() or None)
				if not current_user.is_admin:
					for key in EXTRACTOR_OVERRIDE_KEYS:
						overrides.pop(key, None)
				settings.config_overrides = overrides
				db.session.commit()
				flash("Nastavení uloženo.", "success")
				cfg = get_user_config()  # reload to reflect new overrides
			else:
				db.session.rollback()
		companies = list_companies(current_user)
		doc_types_all = list_all_doc_types(current_user)
		return render_template(
			"settings.html",
			settings=settings,
			cfg=cfg,
			error=error,
			companies=companies,
			doc_types_all=doc_types_all,
			active_tab=active_tab,
		)

	@app.route("/import-abra/<int:batch_id>", methods=["POST"])
	@login_required
	def import_abra_route(batch_id: int):
		cfg = get_user_config()
		batch = load_batch_for_user(batch_id, current_user)
		if batch is None:
			abort(404)
		if not (cfg.abra_server and cfg.abra_username and cfg.abra_password):
			flash("Pro import do ABRA vyplňte prosím server, uživatele a heslo v Nastavení.", "warning")
			return redirect(url_for("user_settings"))
		company_code = (request.form.get("company_code") or "").strip() or None
		direction = (request.form.get("direction") or "").strip() or "faktura-prijata"
		doc_type_code = (request.form.get("doc_type_code") or "").strip() or None
		persist_context(current_user.settings, company_code, direction, doc_type_code)
		if batch.processing_status in {BATCH_STATUS_RUNNING, BATCH_STATUS_QUEUED, BATCH_STATUS_IMPORTING}:
			flash("Dávka se ještě zpracovává nebo se právě importuje. Zkuste to znovu za chvíli.", "warning")
			return redirect(url_for("view_results", batch_id=batch.id))
		selected_count = count_selected_rows(batch.id)
		if selected_count <= 0:
			flash("Vyberte alespoň jednu fakturu k importu.", "warning")
			return redirect(url_for("view_results", batch_id=batch.id))
		active_job = active_job_for_batch(batch.id)
		if active_job is not None and active_job.job_type == JOB_TYPE_IMPORT_ABRA:
			flash("Import do ABRA už je zařazen nebo právě běží.", "warning")
			return redirect(url_for("view_results", batch_id=batch.id))
		queue_batch_job(
			batch_id=batch.id,
			user_id=current_user.id,
			job_type=JOB_TYPE_IMPORT_ABRA,
			payload={
				"company_code": company_code,
				"direction": direction,
				"doc_type_code": doc_type_code,
				"selected_count": selected_count,
			},
			priority=5,
			max_attempts=3,
		)
		batch.processing_status = BATCH_STATUS_WAITING_IMPORT
		batch.current_phase = "waiting_import"
		batch.active_job_type = JOB_TYPE_IMPORT_ABRA
		batch.selected_count = selected_count
		batch.summary_message = f"Import do ABRA byl zařazen do fronty pro {selected_count} označených faktur."
		batch.last_heartbeat_at = _utcnow()
		db.session.commit()
		flash(f"Import do ABRA byl zařazen do fronty pro {selected_count} faktur.", "info")
		return redirect(url_for("view_results", batch_id=batch.id))

	@app.route("/abra/company", methods=["POST"])
	@login_required
	def add_company_route():
		cfg = get_user_config()
		ensure_seed_data(current_user, cfg)
		name = (request.form.get("company_name") or "").strip()
		code = (request.form.get("company_code") or "").strip()
		direction = (request.form.get("direction") or "").strip() or None
		doc_type_code = (request.form.get("doc_type_code") or "").strip() or None
		next_url = request.form.get("next") or url_for("dashboard")
		if not code:
			flash("Kód firmy je povinný.", "danger")
			return redirect(next_url)
		company = store_company(current_user, code, name or code)
		persist_context(current_user.settings, company.code, direction, doc_type_code)
		flash(f"Firma {company.code} přidána.", "success")
		return redirect(next_url)

	@app.route("/abra/doc-type", methods=["POST"])
	@login_required
	def add_doc_type_route():
		cfg = get_user_config()
		ensure_seed_data(current_user, cfg)
		company_code = (request.form.get("company_code") or "").strip()
		direction = (request.form.get("direction") or "").strip()
		name = (request.form.get("doc_type_name") or "").strip()
		code = (request.form.get("doc_type_code") or "").strip()
		next_url = request.form.get("next") or url_for("dashboard")
		if not company_code or not code or not name or not direction:
			flash("Vyplňte firmu, směr a kód i název typu dokladu.", "danger")
			return redirect(next_url)
		store_doc_type(current_user, company_code, direction, name, code)
		persist_context(current_user.settings, company_code, direction, code)
		flash(f"Typ dokladu {code} přidán.", "success")
		return redirect(next_url)

	@app.route("/users", methods=["GET", "POST"])
	@login_required
	def manage_users():
		if not current_user.is_admin:
			abort(403)
		message = None
		error = None
		if request.method == "POST":
			action = request.form.get("action") or "create"
			if action == "delete":
				try:
					user_id = int(request.form.get("user_id", "0"))
				except ValueError:
					user_id = 0
				user_to_delete = User.query.get(user_id)
				if user_to_delete is None:
					error = "Uživatel nebyl nalezen."
				elif user_to_delete.id == current_user.id:
					error = "Nemůžete smazat sami sebe."
				else:
					db.session.delete(user_to_delete)
					db.session.commit()
					message = f"Uživatel {user_to_delete.username} smazán."
			elif action == "adjust_credits":
				try:
					user_id = int(request.form.get("user_id", "0"))
				except ValueError:
					user_id = 0
				try:
					amount = int(request.form.get("amount", "0"))
				except ValueError:
					amount = 0
				direction = request.form.get("direction") or "add"
				target_user = User.query.get(user_id)
				if target_user is None:
					error = "Uživatel nebyl nalezen."
				elif amount <= 0:
					error = "Zadejte částku větší než 0."
				else:
					before = target_user.credits or 0
					if direction == "subtract":
						target_user.credits = max(0, before - amount)
						diff = before - target_user.credits
						message = f"Uživateli {target_user.username} odebráno {diff} kreditů."
					else:
						target_user.credits = before + amount
						message = f"Uživateli {target_user.username} přidáno {amount} kreditů."
					db.session.commit()
			else:
				username = (request.form.get("username") or "").strip()
				password = request.form.get("password") or ""
				if not username or not password:
					error = "Vyplňte uživatelské jméno i heslo."
				elif User.query.filter_by(username=username).first():
					error = "Uživatel s tímto jménem již existuje."
				else:
					user = User(username=username)
					user.set_password(password)
					user.credits = STARTING_CREDITS
					db.session.add(user)
					db.session.commit()
					message = f"Uživatel {username} vytvořen se {STARTING_CREDITS} startovními kredity."
		users = User.query.order_by(User.username.asc()).all()
		return render_template("users.html", users=users, message=message, error=error)


	def ensure_admin_user():
		"""Create or update the admin user from EASYFLEX_ADMIN_PASSWORD."""
		password = os.environ.get("EASYFLEX_ADMIN_PASSWORD")

		# Logování – ať v Render logu vidíme, jestli heslo je / není
		app.logger.info(
			"ensure_admin_user: password env is %s",
			"SET" if password else "MISSING",
		)
		if not password:
			return True

		max_attempts = 2
		for attempt in range(max_attempts):
			try:
				admin = User.query.filter_by(username="admin").first()
				if admin is None:
					app.logger.info("ensure_admin_user: creating new admin user 'admin'")
					admin = User(username="admin", is_admin=True)
					admin.set_password(password)
					admin.credits = STARTING_CREDITS
					db.session.add(admin)
				else:
					app.logger.info(
						"ensure_admin_user: updating existing admin user id=%s", admin.id
					)
					admin.is_admin = True
					admin.set_password(password)
					if admin.credits is None:
						admin.credits = STARTING_CREDITS

				db.session.commit()
				app.logger.info("ensure_admin_user: admin user saved")
				return True
			except OperationalError as exc:
				db.session.rollback()
				db.session.remove()
				app.logger.warning(
					"ensure_admin_user: transient DB error on attempt %s/%s: %s",
					attempt + 1,
					max_attempts,
					exc,
				)
				if attempt + 1 < max_attempts:
					time.sleep(0.2)
				else:
					return False

	@app.before_request
	def _run_admin_init_once():
		"""Run ensure_admin_user() exactly once per process."""
		if not app.config.get("_ADMIN_INITIALIZED", False):
			if ensure_admin_user():
				app.config["_ADMIN_INITIALIZED"] = True
			else:
				app.logger.warning("ensure_admin_user: init failed, will retry on next request.")
			
	return app

app = create_app()


if __name__ == "__main__":
	app.run(debug=True)
