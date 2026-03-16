"""Database models for the EasyFlex web application."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import logging
import re
import time
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()
logger = logging.getLogger(__name__)
_MIGRATION_ADVISORY_LOCK_KEY = 724019631
_MIGRATION_MIN_XACT_AGE_FOR_TERMINATE_S = 60
_ALTER_TABLE_RE = re.compile(
	r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?)(?=\s|$)",
	re.IGNORECASE,
)
_ADD_COLUMN_IF_NOT_EXISTS_RE = re.compile(
	r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(?P<column>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))(?=\s|$)",
	re.IGNORECASE,
)


def _unquote_identifier(name: str) -> str:
	name = (name or "").strip()
	if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
		return name[1:-1].replace('""', '"')
	return name


def _split_table_ref(table_ref: str) -> tuple[str | None, str]:
	raw = (table_ref or "").strip()
	parts = [part.strip() for part in raw.split(".") if part.strip()]
	if len(parts) >= 2:
		return _unquote_identifier(parts[-2]), _unquote_identifier(parts[-1])
	return None, _unquote_identifier(parts[-1] if parts else raw)


def _extract_alter_table_target(sql: str) -> str | None:
	match = _ALTER_TABLE_RE.match(sql or "")
	return match.group("table") if match else None


def _extract_add_column_if_not_exists_target(sql: str) -> tuple[str, str] | None:
	match = _ADD_COLUMN_IF_NOT_EXISTS_RE.match(sql or "")
	if not match:
		return None
	return match.group("table"), match.group("column")


def _column_exists(engine, *, table_ref: str, column_name: str) -> bool:
	schema, table = _split_table_ref(table_ref)
	if not table:
		return False
	normalized_column = _unquote_identifier(column_name)
	columns = {col["name"] for col in inspect(engine).get_columns(table, schema=schema)}
	return normalized_column in columns


def _terminate_stale_table_lock_holders(engine, *, table_ref: str, min_xact_age_s: int) -> int:
	"""Terminate stale same-user transactions that still hold locks on the target table."""
	if engine.dialect.name != "postgresql":
		return 0
	schema, table = _split_table_ref(table_ref)
	if not table:
		return 0
	with engine.begin() as conn:
		rows = conn.execute(
			text(
				"""
				SELECT DISTINCT
					a.pid,
					a.state,
					EXTRACT(EPOCH FROM (NOW() - a.xact_start))::INTEGER AS xact_age_s
				FROM pg_locks l
				JOIN pg_class c ON c.oid = l.relation
				JOIN pg_namespace n ON n.oid = c.relnamespace
				JOIN pg_stat_activity a ON a.pid = l.pid
				WHERE l.granted
				  AND a.pid <> pg_backend_pid()
				  AND a.datname = current_database()
				  AND a.usename = current_user
				  AND a.xact_start IS NOT NULL
				  AND NOW() - a.xact_start > make_interval(secs => :min_age_s)
				  AND c.relname = :table_name
				  AND (:schema_name IS NULL OR n.nspname = :schema_name)
				ORDER BY a.xact_start
				"""
			),
			{
				"table_name": table,
				"schema_name": schema,
				"min_age_s": int(min_xact_age_s),
			},
		).fetchall()
		terminated = 0
		for row in rows:
			was_terminated = bool(conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": int(row.pid)}).scalar())
			if was_terminated:
				terminated += 1
				logger.warning(
					"Terminated stale DB session pid=%s (state=%s, xact_age=%ss) blocking migration on %s",
					row.pid,
					row.state,
					row.xact_age_s,
					table_ref,
				)
		return terminated


def _is_retryable_migration_error(exc: Exception) -> bool:
	message = str(exc).lower()
	return (
		"statement timeout" in message
		or "lock timeout" in message
		or "deadlock detected" in message
		or "canceling statement due to statement timeout" in message
	)


@contextmanager
def _schema_migration_lock(engine):
	"""Serialise startup schema changes across processes on PostgreSQL."""
	if engine.dialect.name != "postgresql":
		yield
		return
	with engine.connect() as conn:
		conn.execute(text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": _MIGRATION_ADVISORY_LOCK_KEY})
		try:
			yield
		finally:
			conn.execute(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": _MIGRATION_ADVISORY_LOCK_KEY})


def _execute_migration_sql(engine, sql: str, *, attempts: int = 20, delay_s: float = 2.0) -> None:
	"""Execute a migration statement with retries for transient DB lock/timeout errors."""
	last_exc: Exception | None = None
	alter_table_target = _extract_alter_table_target(sql) if engine.dialect.name == "postgresql" else None
	for attempt in range(1, attempts + 1):
		try:
			with engine.begin() as conn:
				if engine.dialect.name == "postgresql":
					# Keep DDL from being killed by low per-role statement timeout during deploy.
					conn.execute(text("SET LOCAL statement_timeout = 0"))
					conn.execute(text("SET LOCAL lock_timeout = '15s'"))
				conn.execute(text(sql))
			return
		except OperationalError as exc:
			last_exc = exc
			retryable = _is_retryable_migration_error(exc)
			if retryable and engine.dialect.name == "postgresql" and alter_table_target and attempt >= 3:
				try:
					_terminate_stale_table_lock_holders(
						engine,
						table_ref=alter_table_target,
						min_xact_age_s=_MIGRATION_MIN_XACT_AGE_FOR_TERMINATE_S,
					)
				except Exception:
					logger.warning(
						"Unable to terminate stale lock holders for migration target %s",
						alter_table_target,
						exc_info=True,
					)
			if not retryable:
				raise
			if attempt >= attempts:
				add_col_target = _extract_add_column_if_not_exists_target(sql)
				if add_col_target is not None:
					table_ref, column_name = add_col_target
					try:
						if _column_exists(engine, table_ref=table_ref, column_name=column_name):
							logger.warning(
								"Migration statement hit lock timeouts, but target column already exists: %s",
								sql,
							)
							return
					except Exception:
						logger.warning(
							"Unable to verify target column existence after migration timeout: %s",
							sql,
							exc_info=True,
						)
				raise
			logger.warning(
				"DB migration statement retry %s/%s due to lock/timeout: %s",
				attempt,
				attempts,
				sql,
			)
			time.sleep(delay_s)
	if last_exc is not None:
		raise last_exc


def _ensure_counter_column(engine, *, table_name: str, column_name: str) -> None:
	"""Ensure integer counter column exists with default 0, using lock-safe steps on PostgreSQL."""
	if engine.dialect.name == "postgresql":
		_execute_migration_sql(engine, f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} INTEGER")
		_execute_migration_sql(engine, f"ALTER TABLE {table_name} ALTER COLUMN {column_name} SET DEFAULT 0")
		_execute_migration_sql(engine, f"UPDATE {table_name} SET {column_name} = 0 WHERE {column_name} IS NULL")
		return
	_execute_migration_sql(engine, f"ALTER TABLE {table_name} ADD COLUMN {column_name} INTEGER DEFAULT 0 NOT NULL")


class User(db.Model, UserMixin):
	"""Simple user model for web authentication."""

	id = db.Column(db.Integer, primary_key=True)
	username = db.Column(db.String(80), unique=True, nullable=False)
	password_hash = db.Column(db.String(255), nullable=False)
	is_admin = db.Column(db.Boolean, default=False, nullable=False)
	credits = db.Column(db.Integer, default=100, nullable=False, server_default=text("100"))
	settings = db.relationship(
		"UserSettings",
		uselist=False,
		back_populates="user",
		cascade="all, delete-orphan",
	)
	companies = db.relationship(
		"Company",
		back_populates="user",
		cascade="all, delete-orphan",
	)
	doc_types = db.relationship(
		"DocType",
		back_populates="user",
		cascade="all, delete-orphan",
	)
	batches = db.relationship(
		"InvoiceBatch",
		back_populates="user",
		cascade="all, delete-orphan",
	)

	def set_password(self, password: str) -> None:
		self.password_hash = generate_password_hash(password)

	def check_password(self, password: str) -> bool:
		return check_password_hash(self.password_hash, password)


class UserSettings(db.Model):
	"""Per-user connection/API settings stored in the web DB."""

	id = db.Column(db.Integer, primary_key=True)
	user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
	openai_api_key = db.Column(db.String(255), nullable=True)
	abra_server = db.Column(db.String(255), nullable=True)
	abra_port = db.Column(db.Integer, nullable=True)
	abra_company = db.Column(db.String(255), nullable=True)
	abra_username = db.Column(db.String(255), nullable=True)
	abra_password = db.Column(db.String(255), nullable=True)
	abra_verify_tls = db.Column(db.Boolean, default=True, nullable=False)
	# Flexible holder for per-user overrides (extractor/extraction/CSV, ABRA context, auto-import)
	config_overrides = db.Column(db.JSON, nullable=True)

	user = db.relationship("User", back_populates="settings")


class Company(db.Model):
	"""Per-user ABRA company reference."""

	id = db.Column(db.Integer, primary_key=True)
	user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
	code = db.Column(db.String(120), nullable=False)
	name = db.Column(db.String(255), nullable=False)

	user = db.relationship("User", back_populates="companies")
	doc_types = db.relationship("DocType", back_populates="company", cascade="all, delete-orphan")

	__table_args__ = (db.UniqueConstraint("user_id", "code", name="uq_company_user_code"),)


class DocType(db.Model):
	"""Per-user ABRA document type scoped to company + direction."""

	id = db.Column(db.Integer, primary_key=True)
	user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
	company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
	company_code = db.Column(db.String(120), nullable=False)
	direction = db.Column(db.String(64), nullable=False)
	name = db.Column(db.String(255), nullable=False)
	code = db.Column(db.String(120), nullable=False)

	user = db.relationship("User", back_populates="doc_types")
	company = db.relationship("Company", back_populates="doc_types")

	__table_args__ = (
		db.UniqueConstraint("user_id", "company_code", "direction", "code", name="uq_doc_type_user_company_dir_code"),
	)


class InvoiceBatch(db.Model):
	"""A batch of invoices extracted or imported from a table for a specific user."""

	id = db.Column(db.Integer, primary_key=True)
	user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
	source_label = db.Column(db.String(255), nullable=True)
	source_type = db.Column(db.String(32), nullable=True)  # e.g., pdf/table
	created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
	processing_status = db.Column(db.String(32), nullable=False, default="completed", server_default=text("'completed'"))
	total_files = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
	processed_files = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
	processed_invoices = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
	total_invoices_estimate = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
	current_phase = db.Column(db.String(64), nullable=True)
	success_count = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
	error_count = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
	credits_charged = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
	started_at = db.Column(db.DateTime, nullable=True)
	finished_at = db.Column(db.DateTime, nullable=True)
	last_heartbeat_at = db.Column(db.DateTime, nullable=True)
	summary_message = db.Column(db.Text, nullable=True)

	user = db.relationship("User", back_populates="batches")
	rows = db.relationship("InvoiceRow", back_populates="batch", cascade="all, delete-orphan")


class InvoiceRow(db.Model):
	"""Single invoice row stored for later editing/import."""

	id = db.Column(db.Integer, primary_key=True)
	batch_id = db.Column(db.Integer, db.ForeignKey("invoice_batch.id"), nullable=False)
	row_index = db.Column(db.Integer, nullable=False)
	source = db.Column(db.String(255), nullable=True)
	invoice_data = db.Column(db.JSON, nullable=True)
	warning = db.Column(db.Text, nullable=True)
	error = db.Column(db.Text, nullable=True)
	status = db.Column(db.String(120), nullable=True)
	marked_for_import = db.Column(db.Boolean, default=True, nullable=False)

	batch = db.relationship("InvoiceBatch", back_populates="rows")


def init_db(app) -> None:
	"""Initialise SQLAlchemy and create tables."""
	db.init_app(app)
	with app.app_context():
		engine = db.engine
		with _schema_migration_lock(engine):
			db.create_all()
			# Lightweight migration: add config_overrides column if absent (SQLite only)
			insp = inspect(engine)
			columns = {col["name"] for col in insp.get_columns("user_settings")}
			if "config_overrides" not in columns:
				_execute_migration_sql(engine, "ALTER TABLE user_settings ADD COLUMN config_overrides TEXT")
			user_columns = {col["name"] for col in insp.get_columns("user")}
			if "credits" not in user_columns:
				table_name = '"user"' if engine.dialect.name == "postgresql" else "user"
				_execute_migration_sql(engine, f"ALTER TABLE {table_name} ADD COLUMN credits INTEGER DEFAULT 100 NOT NULL")
			batch_columns = {col["name"] for col in insp.get_columns("invoice_batch")}
			if "processing_status" not in batch_columns:
				_execute_migration_sql(engine, "ALTER TABLE invoice_batch ADD COLUMN processing_status VARCHAR(32) DEFAULT 'completed' NOT NULL")
			if "total_files" not in batch_columns:
				_ensure_counter_column(engine, table_name="invoice_batch", column_name="total_files")
			if "processed_files" not in batch_columns:
				_ensure_counter_column(engine, table_name="invoice_batch", column_name="processed_files")
			if "processed_invoices" not in batch_columns:
				_ensure_counter_column(engine, table_name="invoice_batch", column_name="processed_invoices")
			if "total_invoices_estimate" not in batch_columns:
				_ensure_counter_column(engine, table_name="invoice_batch", column_name="total_invoices_estimate")
			if "current_phase" not in batch_columns:
				_execute_migration_sql(engine, "ALTER TABLE invoice_batch ADD COLUMN current_phase VARCHAR(64)")
			if "success_count" not in batch_columns:
				_ensure_counter_column(engine, table_name="invoice_batch", column_name="success_count")
			if "error_count" not in batch_columns:
				_ensure_counter_column(engine, table_name="invoice_batch", column_name="error_count")
			if "credits_charged" not in batch_columns:
				_ensure_counter_column(engine, table_name="invoice_batch", column_name="credits_charged")
			if "started_at" not in batch_columns:
				_execute_migration_sql(engine, "ALTER TABLE invoice_batch ADD COLUMN started_at TIMESTAMP")
			if "finished_at" not in batch_columns:
				_execute_migration_sql(engine, "ALTER TABLE invoice_batch ADD COLUMN finished_at TIMESTAMP")
			if "last_heartbeat_at" not in batch_columns:
				_execute_migration_sql(engine, "ALTER TABLE invoice_batch ADD COLUMN last_heartbeat_at TIMESTAMP")
			if "summary_message" not in batch_columns:
				_execute_migration_sql(engine, "ALTER TABLE invoice_batch ADD COLUMN summary_message TEXT")
			# If the process restarted while batch processing was in-flight, expose partial results as interrupted.
			# Keep this update narrowly scoped to avoid heavy startup scans.
			with engine.begin() as conn:
				conn.execute(text(
					"UPDATE invoice_batch "
					"SET processing_status = 'interrupted', "
					"finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP), "
					"summary_message = COALESCE(summary_message, 'Zpracování bylo přerušeno restartem serveru.') "
					"WHERE processing_status IN ('queued', 'running')"
				))
