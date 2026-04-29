"""Shared helpers for DB-backed batch jobs."""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .constants import (
	BATCH_PHASE_LABELS,
	BATCH_STATUS_LABELS,
	BATCH_TERMINAL_STATUSES,
	JOB_STATUS_CANCELLED,
	JOB_STATUS_LABELS,
	JOB_STATUS_QUEUED,
	JOB_STATUS_RETRYABLE_FAILED,
	JOB_STATUS_RUNNING,
	JOB_TERMINAL_STATUSES,
)
from .models import BatchJob, InvoiceBatch, db

_DbResultT = TypeVar("_DbResultT")
DEFAULT_DB_RETRY_ATTEMPTS = 3
DEFAULT_STALE_JOB_SECONDS = 90


def utcnow() -> datetime:
	return datetime.now(UTC).replace(tzinfo=None)


def batch_status_label(status: Optional[str]) -> str:
	return BATCH_STATUS_LABELS.get((status or "").strip().lower(), "Neznámý stav")


def job_status_label(status: Optional[str]) -> str:
	return JOB_STATUS_LABELS.get((status or "").strip().lower(), "Neznámý stav")


def batch_phase_label(phase: Optional[str]) -> str:
	return BATCH_PHASE_LABELS.get((phase or "").strip().lower(), "Zpracování")


def is_terminal_batch_status(status: Optional[str]) -> bool:
	return (status or "").strip().lower() in BATCH_TERMINAL_STATUSES


def is_terminal_job_status(status: Optional[str]) -> bool:
	return (status or "").strip().lower() in JOB_TERMINAL_STATUSES


def db_retry_sleep(attempt_number: int) -> float:
	return min(2.0, 0.35 * attempt_number)


def run_db_read_with_retry(
	operation_name: str,
	reader: Callable[[], _DbResultT],
	*,
	max_attempts: int = DEFAULT_DB_RETRY_ATTEMPTS,
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
			time.sleep(db_retry_sleep(attempt))
		finally:
			db.session.remove()
	if last_exc is not None:
		raise last_exc
	raise RuntimeError(f"DB read selhal: {operation_name}")


def run_db_write_with_retry(
	operation_name: str,
	writer: Callable[[], _DbResultT],
	*,
	max_attempts: int = DEFAULT_DB_RETRY_ATTEMPTS,
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
			time.sleep(db_retry_sleep(attempt))
		except Exception:
			db.session.rollback()
			raise
		finally:
			db.session.remove()
	if last_exc is not None:
		raise last_exc
	raise RuntimeError(f"DB write selhal: {operation_name}")


def get_worker_id() -> str:
	return f"{socket.gethostname()}:{os.getpid()}"


def batch_storage_root(batch_id: int) -> Path:
	root = Path(current_app.instance_path) / "batches" / str(batch_id)
	root.mkdir(parents=True, exist_ok=True)
	return root


def batch_payload_path(batch_id: int) -> Path:
	return batch_storage_root(batch_id) / "payload.json"


def persist_batch_payload(batch_id: int, payload: dict[str, Any]) -> None:
	path = batch_payload_path(batch_id)
	path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_batch_payload(batch_id: int) -> dict[str, Any]:
	path = batch_payload_path(batch_id)
	if not path.exists():
		return {}
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except Exception:
		return {}


def queue_batch_job(
	*,
	batch_id: int,
	user_id: int,
	job_type: str,
	payload: Optional[dict[str, Any]] = None,
	priority: int = 0,
	max_attempts: int = 3,
) -> BatchJob:
	job = BatchJob(
		batch_id=batch_id,
		user_id=user_id,
		job_type=job_type,
		status=JOB_STATUS_QUEUED,
		priority=priority,
		attempt_count=0,
		max_attempts=max(1, int(max_attempts)),
		payload=payload or {},
		resume_cursor={},
	)
	db.session.add(job)
	return job


def active_job_for_batch(batch_id: int) -> BatchJob | None:
	return (
		BatchJob.query
		.filter(
			BatchJob.batch_id == batch_id,
			BatchJob.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_RETRYABLE_FAILED]),
		)
		.order_by(BatchJob.priority.desc(), BatchJob.id.asc())
		.first()
	)


def requeue_stale_running_jobs(*, stale_after_s: int = DEFAULT_STALE_JOB_SECONDS) -> int:
	cutoff = utcnow() - timedelta(seconds=max(15, int(stale_after_s)))
	jobs = (
		BatchJob.query
		.filter(
			BatchJob.status == JOB_STATUS_RUNNING,
			BatchJob.last_heartbeat_at.isnot(None),
			BatchJob.last_heartbeat_at < cutoff,
		)
		.all()
	)
	count = 0
	for job in jobs:
		job.status = JOB_STATUS_RETRYABLE_FAILED
		job.claimed_by = None
		job.claimed_at = None
		job.summary_message = (job.summary_message or "").strip() or "Job vyžaduje obnovení po restartu workeru."
		job.last_error = "Worker heartbeat vypršel."
		batch = db.session.get(InvoiceBatch, job.batch_id)
		if batch is not None and batch.active_job_type == job.job_type:
			batch.active_job_type = job.job_type
			if batch.processing_status == "running":
				batch.processing_status = "interrupted"
		count += 1
	if count:
		db.session.commit()
	return count


def claim_next_job(worker_id: str) -> BatchJob | None:
	engine = db.engine
	now = utcnow()
	if engine.dialect.name == "postgresql":
		with engine.begin() as conn:
			row = conn.execute(
				text(
					"""
					UPDATE batch_job
					SET status = :running,
					    claimed_by = :worker_id,
					    claimed_at = :now,
					    last_heartbeat_at = :now,
					    started_at = COALESCE(started_at, :now),
					    attempt_count = attempt_count + 1
					WHERE id = (
						SELECT id
						FROM batch_job
						WHERE status IN (:queued, :retryable_failed)
						ORDER BY priority DESC, id ASC
						FOR UPDATE SKIP LOCKED
						LIMIT 1
					)
					RETURNING id
					"""
				),
				{
					"running": JOB_STATUS_RUNNING,
					"worker_id": worker_id,
					"now": now,
					"queued": JOB_STATUS_QUEUED,
					"retryable_failed": JOB_STATUS_RETRYABLE_FAILED,
				},
			).first()
		if not row:
			return None
		return db.session.get(BatchJob, int(row.id))

	job = (
		BatchJob.query
		.filter(BatchJob.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_RETRYABLE_FAILED]))
		.order_by(BatchJob.priority.desc(), BatchJob.id.asc())
		.first()
	)
	if job is None:
		return None
	job.status = JOB_STATUS_RUNNING
	job.claimed_by = worker_id
	job.claimed_at = now
	job.last_heartbeat_at = now
	job.started_at = job.started_at or now
	job.attempt_count = int(job.attempt_count or 0) + 1
	db.session.commit()
	return job


def heartbeat_job(job_id: int, worker_id: str) -> bool:
	job = db.session.get(BatchJob, job_id)
	if job is None or job.claimed_by != worker_id or job.status != JOB_STATUS_RUNNING:
		return False
	job.last_heartbeat_at = utcnow()
	return True


def finalize_job(
	*,
	job_id: int,
	status: str,
	summary_message: Optional[str] = None,
	last_error: Optional[str] = None,
	resume_cursor: Optional[dict[str, Any]] = None,
) -> bool:
	job = db.session.get(BatchJob, job_id)
	if job is None:
		return False
	job.status = status
	job.finished_at = utcnow()
	job.last_heartbeat_at = utcnow()
	job.summary_message = summary_message
	job.last_error = last_error
	if resume_cursor is not None:
		job.resume_cursor = resume_cursor
	job.claimed_by = None if status != JOB_STATUS_RUNNING else job.claimed_by
	job.claimed_at = None if status != JOB_STATUS_RUNNING else job.claimed_at
	return True


def cancel_non_terminal_jobs_for_batch(batch_id: int, *, keep_job_id: Optional[int] = None) -> int:
	jobs = (
		BatchJob.query
		.filter(
			BatchJob.batch_id == batch_id,
			BatchJob.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_RETRYABLE_FAILED]),
		)
		.all()
	)
	count = 0
	for job in jobs:
		if keep_job_id is not None and job.id == keep_job_id:
			continue
		job.status = JOB_STATUS_CANCELLED
		job.finished_at = utcnow()
		job.summary_message = "Job byl nahrazen novějším požadavkem."
		count += 1
	return count
