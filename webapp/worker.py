"""Persistent DB-backed worker for EasyFlex batch jobs."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Flask

from EasyFlex.invoice_warnings import get_warning, set_warning
from EasyFlex.models import InvoiceData

from .abra_context import apply_context_to_config, current_context
from .batch_jobs import (
	active_job_for_batch,
	batch_storage_root,
	cancel_non_terminal_jobs_for_batch,
	claim_next_job,
	finalize_job,
	get_worker_id,
	heartbeat_job,
	is_terminal_batch_status,
	job_status_label,
	load_batch_payload,
	persist_batch_payload,
	queue_batch_job,
	requeue_stale_running_jobs,
	run_db_read_with_retry,
	run_db_write_with_retry,
	utcnow,
)
from .config_utils import get_user_config_for_user
from .constants import (
	BATCH_STATUS_COMPLETED,
	BATCH_STATUS_COMPLETED_WITH_ERRORS,
	BATCH_STATUS_FAILED,
	BATCH_STATUS_IMPORTING,
	BATCH_STATUS_PARTIAL_TIMEOUT,
	BATCH_STATUS_QUEUED,
	BATCH_STATUS_RUNNING,
	BATCH_STATUS_WAITING_IMPORT,
	JOB_STATUS_COMPLETED,
	JOB_STATUS_COMPLETED_WITH_ERRORS,
	JOB_STATUS_FAILED,
	JOB_STATUS_RETRYABLE_FAILED,
	JOB_TYPE_EXTRACT_PDF,
	JOB_TYPE_IMPORT_ABRA,
	ROW_IMPORT_STATUS_FAILED,
	ROW_IMPORT_STATUS_IMPORTED,
	ROW_IMPORT_STATUS_PENDING,
	ROW_IMPORT_STATUS_RUNNING,
	ROW_IMPORT_STATUS_SKIPPED,
)
from .invoice_batches import count_selected_rows
from .models import BatchJob, InvoiceBatch, InvoiceRow, User, db

logger = logging.getLogger(__name__)

DEFAULT_PDF_BATCH_MAX_RUNTIME_S = 60 * 60
DEFAULT_JOB_POLL_INTERVAL_S = 2.0
DEFAULT_JOB_HEARTBEAT_INTERVAL_S = 5.0
DEFAULT_STALE_JOB_SECONDS = 90


def _load_batch_runtime_limit_s() -> int:
	raw = (os.getenv("PDF_BATCH_MAX_RUNTIME_S") or "").strip()
	if not raw:
		return DEFAULT_PDF_BATCH_MAX_RUNTIME_S
	try:
		value = int(raw)
	except ValueError:
		return DEFAULT_PDF_BATCH_MAX_RUNTIME_S
	return max(60, value)


def _build_extractor(cfg):
	from EasyFlex.extractor import InvoiceExtractor

	return InvoiceExtractor(config=cfg)


def _build_csv_processor(cfg):
	from EasyFlex.csv_processor import CSVProcessor

	return CSVProcessor(config=cfg)


def _import_to_abra(payload: dict[str, Any], cfg):
	from EasyFlex.abra import import_to_abra

	return import_to_abra(payload, cfg=cfg)


def _run_extraction(extractor, pdf_path: Path, *, on_result: Optional[Callable[[Any], None]] = None):
	try:
		return asyncio.run(extractor.extract_auto(str(pdf_path), on_result=on_result))
	except RuntimeError:
		loop = asyncio.new_event_loop()
		try:
			return loop.run_until_complete(extractor.extract_auto(str(pdf_path), on_result=on_result))
		finally:
			loop.close()


def _extract_file_with_retry(extractor, pdf_path: Path, display_name: str, *, on_result=None, max_attempts: int = 2):
	last_results: list[Any] = []
	for attempt in range(1, max_attempts + 1):
		attempt_results: list[Any] = []
		streaming_started = False

		def _collect_result(res: Any) -> None:
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


def _merge_warning_text(invoice_obj: Any, warnings: Optional[list[str]]) -> Optional[str]:
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


def _has_minimal_invoice_data(inv: dict[str, object]) -> bool:
	return bool(inv.get("cislo_dokladu") or inv.get("variabilni_symbol") or inv.get("odberatel_jmeno"))


def _set_batch_extract_running(batch: InvoiceBatch) -> bool:
	batch.processing_status = BATCH_STATUS_RUNNING
	batch.active_job_type = JOB_TYPE_EXTRACT_PDF
	batch.current_phase = "extracting"
	batch.started_at = batch.started_at or utcnow()
	batch.last_heartbeat_at = utcnow()
	batch.summary_message = batch.summary_message or "Extrakce dávky byla zařazena do fronty."
	return True


def _set_batch_import_running(batch: InvoiceBatch) -> bool:
	batch.processing_status = BATCH_STATUS_IMPORTING
	batch.active_job_type = JOB_TYPE_IMPORT_ABRA
	batch.current_phase = "importing"
	batch.last_heartbeat_at = utcnow()
	batch.summary_message = "Probíhá import označených faktur do ABRA."
	return True


def _update_batch_selection_count(batch: InvoiceBatch) -> None:
	batch.selected_count = count_selected_rows(batch.id)


def _persist_extract_result(
	*,
	job_id: int,
	batch_id: int,
	user_id: int,
	file_index: int,
	display_name: str,
	source_invoice_index: int,
	next_row_index: int,
	invoice_payload: dict[str, Any],
	warning_text: Optional[str],
	row_error: Optional[str],
) -> tuple[int, bool]:
	job = db.session.get(BatchJob, job_id)
	batch = db.session.get(InvoiceBatch, batch_id)
	user = db.session.get(User, user_id)
	if job is None or batch is None or user is None:
		return next_row_index, True
	existing_row = (
		InvoiceRow.query
		.filter_by(
			batch_id=batch_id,
			source_file_index=file_index,
			source_invoice_index=source_invoice_index,
		)
		.first()
	)
	if existing_row is not None:
		job.resume_cursor = {
			"file_index": file_index,
			"source_invoice_index": source_invoice_index,
			"next_row_index": next_row_index,
		}
		job.last_heartbeat_at = utcnow()
		batch.last_heartbeat_at = utcnow()
		return next_row_index, False

	local_invoice = dict(invoice_payload or {})
	local_error = (row_error or "").strip() or None
	credits_exhausted = False
	marked_for_import = False

	if local_invoice:
		if int(user.credits or 0) <= 0:
			local_invoice = {}
			local_error = "Extrakce zastavena: došly kredity pro další faktury v dávce."
			credits_exhausted = True
		else:
			batch.success_count = int(batch.success_count or 0) + 1
			batch.credits_charged = int(batch.credits_charged or 0) + 1
			user.credits = max(0, int(user.credits or 0) - 1)
			marked_for_import = True

	if not local_invoice:
		if local_error is None:
			local_error = "Extrakce nevrátila použitelná data."
		batch.error_count = int(batch.error_count or 0) + 1

	row = InvoiceRow(
		batch=batch,
		row_index=next_row_index,
		source_file_index=file_index,
		source_invoice_index=source_invoice_index,
		source=display_name,
		invoice_data=local_invoice,
		warning=warning_text,
		error=local_error,
		marked_for_import=marked_for_import,
		import_status=ROW_IMPORT_STATUS_PENDING,
	)
	db.session.add(row)
	batch.processed_invoices = int(batch.processed_invoices or 0) + 1
	batch.total_invoices_estimate = max(int(batch.total_invoices_estimate or 0), int(batch.processed_invoices or 0))
	if marked_for_import:
		batch.selected_count = int(batch.selected_count or 0) + 1
	batch.current_phase = "persisting"
	batch.last_heartbeat_at = utcnow()
	batch.summary_message = (
		f"Průběžně uloženo {int(batch.processed_invoices or 0)} faktur, "
		f"zpracovává se {display_name}."
	)
	job.resume_cursor = {
		"file_index": file_index,
		"source_invoice_index": source_invoice_index,
		"next_row_index": next_row_index + 1,
	}
	job.last_heartbeat_at = utcnow()
	return next_row_index + 1, credits_exhausted


def _mark_file_processed(*, job_id: int, batch_id: int, processed_files_target: int, total_files: int) -> bool:
	job = db.session.get(BatchJob, job_id)
	batch = db.session.get(InvoiceBatch, batch_id)
	if job is None or batch is None:
		return False
	batch.processed_files = max(int(batch.processed_files or 0), int(processed_files_target))
	batch.total_files = max(int(batch.total_files or 0), int(total_files))
	batch.last_heartbeat_at = utcnow()
	batch.current_phase = "extracting"
	batch.summary_message = (
		f"Hotovo {int(batch.processed_files or 0)}/{int(batch.total_files or 0)} PDF, "
		f"uloženo {int(batch.processed_invoices or 0)} faktur."
	)
	job.resume_cursor = {
		**dict(job.resume_cursor or {}),
		"file_index": processed_files_target,
	}
	job.last_heartbeat_at = utcnow()
	return True


def _finalize_extract_job(
	*,
	job_id: int,
	stop_reason: Optional[str],
	auto_import: bool,
	auto_import_payload: Optional[dict[str, Any]],
) -> tuple[str, str]:
	job = db.session.get(BatchJob, job_id)
	if job is None:
		return JOB_STATUS_FAILED, "Job nebyl nalezen."
	batch = db.session.get(InvoiceBatch, job.batch_id)
	if batch is None:
		return JOB_STATUS_FAILED, "Dávka nebyla nalezena."
	status = JOB_STATUS_COMPLETED
	if stop_reason == "timeout":
		batch.processing_status = BATCH_STATUS_PARTIAL_TIMEOUT
		status = JOB_STATUS_COMPLETED_WITH_ERRORS
	elif stop_reason == "credits":
		batch.processing_status = BATCH_STATUS_COMPLETED_WITH_ERRORS
		status = JOB_STATUS_COMPLETED_WITH_ERRORS
	elif stop_reason == "failed":
		batch.processing_status = BATCH_STATUS_FAILED
		status = JOB_STATUS_FAILED
	elif int(batch.error_count or 0) > 0:
		batch.processing_status = BATCH_STATUS_COMPLETED_WITH_ERRORS
		status = JOB_STATUS_COMPLETED_WITH_ERRORS
	else:
		batch.processing_status = BATCH_STATUS_COMPLETED
		status = JOB_STATUS_COMPLETED

	summary = (
		f"PDF dávka: {int(batch.processed_files or 0)}/{int(batch.total_files or 0)} PDF, "
		f"{int(batch.success_count or 0)} úspěšných faktur, {int(batch.error_count or 0)} chyb."
	)
	if stop_reason == "timeout":
		summary += " Zpracování bylo ukončeno časovým limitem."
	elif stop_reason == "credits":
		summary += " Zpracování se zastavilo kvůli vyčerpání kreditů."
	elif stop_reason == "failed":
		summary += " Dávka selhala."

	if auto_import and int(batch.selected_count or 0) > 0 and batch.processing_status != BATCH_STATUS_FAILED:
		queue_batch_job(
			batch_id=batch.id,
			user_id=batch.user_id,
			job_type=JOB_TYPE_IMPORT_ABRA,
			payload=auto_import_payload or {},
			priority=5,
			max_attempts=3,
		)
		batch.processing_status = BATCH_STATUS_WAITING_IMPORT
		batch.current_phase = "waiting_import"
		batch.active_job_type = JOB_TYPE_IMPORT_ABRA
		summary += " Auto-import byl zařazen do fronty."
	else:
		batch.current_phase = "done"
		batch.active_job_type = None

	batch.finished_at = utcnow()
	batch.last_heartbeat_at = utcnow()
	batch.summary_message = summary
	job.summary_message = summary
	return status, summary


def _resolve_import_outcome(resp: Any) -> tuple[str, Optional[str], str]:
	if isinstance(resp, dict) and resp.get("__status") == "skipped-duplicate":
		return ROW_IMPORT_STATUS_SKIPPED, None, "Přeskočeno (duplicitní kód)"
	if isinstance(resp, dict) and resp.get("__status") == "skipped-duplicate-foreign":
		return ROW_IMPORT_STATUS_SKIPPED, None, "Přeskočeno (kód patří jinému dokladu)"
	if isinstance(resp, dict) and resp.get("__status") == "failed-duplicate-not-found":
		return ROW_IMPORT_STATUS_FAILED, "Duplicitní kód – ABRA nenašla doklad podle kódu.", ""
	if isinstance(resp, dict) and resp.get("__status") == "failed-duplicate-unverifiable":
		return ROW_IMPORT_STATUS_FAILED, "Duplicitní kód – nelze ověřit ext-id, doklad neimportován.", ""
	if isinstance(resp, dict) and resp.get("__status") == "failed-duplicate-update":
		return ROW_IMPORT_STATUS_FAILED, "Duplicitní kód – update selhal.", ""
	if resp is None:
		return ROW_IMPORT_STATUS_FAILED, "Import se nepodařil (zkontrolujte logy).", ""
	if isinstance(resp, dict) and resp.get("__status") == "updated":
		return ROW_IMPORT_STATUS_IMPORTED, None, "Importováno (aktualizováno)"
	return ROW_IMPORT_STATUS_IMPORTED, None, "Importováno"


def _finalize_import_job(*, job_id: int) -> tuple[str, str]:
	job = db.session.get(BatchJob, job_id)
	if job is None:
		return JOB_STATUS_FAILED, "Job nebyl nalezen."
	batch = db.session.get(InvoiceBatch, job.batch_id)
	if batch is None:
		return JOB_STATUS_FAILED, "Dávka nebyla nalezena."
	imported_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.import_status == ROW_IMPORT_STATUS_IMPORTED)
		.scalar()
		or 0
	)
	failed_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.import_status == ROW_IMPORT_STATUS_FAILED)
		.scalar()
		or 0
	)
	skipped_count = (
		db.session.query(db.func.count(InvoiceRow.id))
		.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.import_status == ROW_IMPORT_STATUS_SKIPPED)
		.scalar()
		or 0
	)
	if failed_count:
		status = JOB_STATUS_COMPLETED_WITH_ERRORS
		batch.processing_status = BATCH_STATUS_COMPLETED_WITH_ERRORS
	else:
		status = JOB_STATUS_COMPLETED
		batch.processing_status = BATCH_STATUS_COMPLETED
	batch.current_phase = "done"
	batch.active_job_type = None
	batch.finished_at = utcnow()
	batch.last_heartbeat_at = utcnow()
	summary = f"Import ABRA: {imported_count} OK, {failed_count} chyb, {skipped_count} přeskočeno."
	batch.summary_message = summary
	job.summary_message = summary
	return status, summary


def _process_extract_job(job_id: int, worker_id: str) -> None:
	job = db.session.get(BatchJob, job_id)
	if job is None:
		return
	batch = db.session.get(InvoiceBatch, job.batch_id)
	user = db.session.get(User, job.user_id)
	if batch is None or user is None:
		finalize_job(job_id=job_id, status=JOB_STATUS_FAILED, summary_message="Chybí dávka nebo uživatel.", last_error="Batch/User missing")
		db.session.commit()
		return
	run_db_write_with_retry(f"job {job_id} start extract", lambda: _set_batch_extract_running(batch))
	cfg = get_user_config_for_user(user)
	extractor = _build_extractor(cfg)
	payload = dict(job.payload or load_batch_payload(batch.id))
	files = list(payload.get("files") or [])
	if not files:
		finalize_job(job_id=job_id, status=JOB_STATUS_FAILED, summary_message="Chybí seznam PDF souborů.", last_error="Missing files payload")
		db.session.commit()
		return
	cursor = dict(job.resume_cursor or {})
	start_file_index = int(cursor.get("file_index", 0) or 0)
	next_row_index = int(cursor.get("next_row_index", 0) or 0)
	if next_row_index <= 0:
		max_row = (
			db.session.query(db.func.max(InvoiceRow.row_index))
			.filter(InvoiceRow.batch_id == batch.id)
			.scalar()
		)
		next_row_index = int(max_row) + 1 if max_row is not None else 0

	started_perf = time.perf_counter()
	stop_reason: Optional[str] = None
	auto_import_payload: Optional[dict[str, Any]] = None

	for file_index in range(start_file_index, len(files)):
		if not heartbeat_job(job_id, worker_id):
			break
		db.session.commit()
		if time.perf_counter() - started_perf >= _load_batch_runtime_limit_s():
			stop_reason = "timeout"
			break
		file_meta = files[file_index]
		display_name = str(file_meta.get("display_name") or f"upload-{file_index + 1}.pdf")
		rel_path = str(file_meta.get("relative_path") or "")
		pdf_path = batch_storage_root(batch.id) / rel_path
		if not pdf_path.exists():
			stop_reason = "failed"
			job.last_error = f"Chybí soubor {display_name}."
			break
		credits = max(0, int(user.credits or 0))
		if credits <= 0:
			stop_reason = "credits"
			break
		seen_in_file: set[int] = set()

		def _persist_stream_result(res: Any) -> None:
			nonlocal next_row_index, stop_reason
			if stop_reason is not None:
				return
			group_index = int(getattr(res, "_invoice_group_index", 0) or 0)
			if group_index <= 0:
				group_index = len(seen_in_file) + 1
			if group_index in seen_in_file:
				return
			seen_in_file.add(group_index)
			data_obj = getattr(res, "data", None)
			row_error = (getattr(res, "error", None) or "").strip() or None
			warning_text = _merge_warning_text(data_obj, getattr(res, "warnings", None))
			invoice_payload: dict[str, Any] = {}
			if data_obj is not None:
				try:
					invoice_payload = data_obj.model_dump()
				except Exception:
					invoice_payload = {}
				if warning_text and invoice_payload:
					set_warning(invoice_payload, warning_text)
			def _writer():
				return _persist_extract_result(
					job_id=job_id,
					batch_id=batch.id,
					user_id=user.id,
					file_index=file_index,
					display_name=display_name,
					source_invoice_index=group_index,
					next_row_index=next_row_index,
					invoice_payload=invoice_payload,
					warning_text=warning_text,
					row_error=row_error,
				)
			next_row_index_local, credits_exhausted = run_db_write_with_retry(
				f"job {job_id} persist file {file_index} invoice {group_index}",
				_writer,
			)
			next_row_index = next_row_index_local
			if credits_exhausted:
				stop_reason = "credits"

		file_results = _extract_file_with_retry(extractor, pdf_path, display_name, on_result=_persist_stream_result, max_attempts=2)
		if not file_results:
			from EasyFlex.extractor import ExtractResult

			file_results = [ExtractResult(file_path=display_name, data=None, error="Extrakce nevrátila žádná data.")]
			for item in file_results:
				_persist_stream_result(item)
		run_db_write_with_retry(
			f"job {job_id} mark file processed",
			lambda: _mark_file_processed(
				job_id=job_id,
				batch_id=batch.id,
				processed_files_target=file_index + 1,
				total_files=len(files),
			),
		)
		if stop_reason in {"timeout", "credits", "failed"}:
			break

	if getattr(cfg, "auto_import", False):
		company_code, direction, doc_type_code = current_context(user.settings, base_cfg=cfg)
		auto_import_payload = {
			"company_code": company_code,
			"direction": direction,
			"doc_type_code": doc_type_code,
			"auto_import": True,
		}

	def _finalize():
		return _finalize_extract_job(
			job_id=job_id,
			stop_reason=stop_reason,
			auto_import=bool(getattr(cfg, "auto_import", False)),
			auto_import_payload=auto_import_payload,
		)

	job_status, summary = run_db_write_with_retry(f"job {job_id} finalize extract", _finalize)
	run_db_write_with_retry(
		f"job {job_id} finalize state",
		lambda: finalize_job(job_id=job_id, status=job_status, summary_message=summary, last_error=getattr(job, "last_error", None)),
	)


def _process_import_job(job_id: int, worker_id: str) -> None:
	job = db.session.get(BatchJob, job_id)
	if job is None:
		return
	batch = db.session.get(InvoiceBatch, job.batch_id)
	user = db.session.get(User, job.user_id)
	if batch is None or user is None:
		finalize_job(job_id=job_id, status=JOB_STATUS_FAILED, summary_message="Chybí dávka nebo uživatel.", last_error="Batch/User missing")
		db.session.commit()
		return
	run_db_write_with_retry(f"job {job_id} start import", lambda: _set_batch_import_running(batch))
	cfg = get_user_config_for_user(user)
	payload = dict(job.payload or {})
	apply_context_to_config(cfg, payload.get("company_code"), payload.get("direction"), payload.get("doc_type_code"))
	rows = (
		InvoiceRow.query
		.filter(InvoiceRow.batch_id == batch.id, InvoiceRow.marked_for_import.is_(True))
		.order_by(InvoiceRow.row_index.asc())
		.all()
	)
	for row in rows:
		if not heartbeat_job(job_id, worker_id):
			break
		db.session.commit()
		if row.import_status == ROW_IMPORT_STATUS_IMPORTED:
			continue
		inv_dict = dict(row.invoice_data or {})
		if not _has_minimal_invoice_data(inv_dict):
			def _skip_writer():
				row.import_status = ROW_IMPORT_STATUS_SKIPPED
				row.status = "Přeskočeno – chybí data"
				row.error = None
				row.last_import_error = None
				return True
			run_db_write_with_retry(f"job {job_id} skip row {row.id}", _skip_writer)
			continue
		def _mark_running():
			row.import_status = ROW_IMPORT_STATUS_RUNNING
			row.import_attempts = int(row.import_attempts or 0) + 1
			row.last_import_error = None
			batch.last_heartbeat_at = utcnow()
			batch.summary_message = f"Importuji řádek {row.row_index + 1} do ABRA."
			return True
		run_db_write_with_retry(f"job {job_id} mark running row {row.id}", _mark_running)
		try:
			try:
				payload_obj = InvoiceData.model_validate(inv_dict)
				payload_dict = payload_obj.model_dump()
			except Exception:
				payload_dict = inv_dict
			resp = _import_to_abra(payload_dict, cfg)
			import_status, error_text, status_text = _resolve_import_outcome(resp)
		except Exception as exc:  # noqa: BLE001
			logger.exception("Chyba importu do ABRA")
			import_status, error_text, status_text = ROW_IMPORT_STATUS_FAILED, str(exc), ""
		def _finish_row():
			row.import_status = import_status
			row.status = status_text or None
			row.error = error_text
			row.last_import_error = error_text
			if import_status == ROW_IMPORT_STATUS_IMPORTED:
				row.imported_at = utcnow()
			batch.last_heartbeat_at = utcnow()
			return True
		run_db_write_with_retry(f"job {job_id} finalize row {row.id}", _finish_row)

	job_status, summary = run_db_write_with_retry(f"job {job_id} finalize import", lambda: _finalize_import_job(job_id=job_id))
	run_db_write_with_retry(
		f"job {job_id} finalize import state",
		lambda: finalize_job(job_id=job_id, status=job_status, summary_message=summary),
	)


def process_job(job_id: int, worker_id: str) -> None:
	job = db.session.get(BatchJob, job_id)
	if job is None:
		return
	try:
		if job.job_type == JOB_TYPE_EXTRACT_PDF:
			_process_extract_job(job_id, worker_id)
		elif job.job_type == JOB_TYPE_IMPORT_ABRA:
			_process_import_job(job_id, worker_id)
		else:
			run_db_write_with_retry(
				f"job {job_id} unsupported type",
				lambda: finalize_job(
					job_id=job_id,
					status=JOB_STATUS_FAILED,
					summary_message=f"Nepodporovaný job type: {job.job_type}",
					last_error="Unsupported job type",
				),
			)
	except Exception as exc:  # noqa: BLE001
		logger.exception("Worker job %s selhal", job_id)
		run_db_write_with_retry(
			f"job {job_id} crash finalize",
			lambda: finalize_job(
				job_id=job_id,
				status=JOB_STATUS_RETRYABLE_FAILED,
				summary_message="Job selhal a čeká na obnovení workerem.",
				last_error=str(exc),
			),
		)


def run_forever(app: Flask, *, poll_interval_s: float = DEFAULT_JOB_POLL_INTERVAL_S) -> None:
	worker_id = get_worker_id()
	logger.info("EasyFlex worker start: %s", worker_id)
	with app.app_context():
		requeue_stale_running_jobs(stale_after_s=DEFAULT_STALE_JOB_SECONDS)
	while True:
		with app.app_context():
			requeue_stale_running_jobs(stale_after_s=DEFAULT_STALE_JOB_SECONDS)
			job = claim_next_job(worker_id)
			if job is None:
				time.sleep(poll_interval_s)
				continue
			logger.info("Worker %s claimed job %s (%s)", worker_id, job.id, job.job_type)
			process_job(job.id, worker_id)


def main() -> None:
	from .app import create_app

	app = create_app()
	run_forever(app)


if __name__ == "__main__":
	main()
