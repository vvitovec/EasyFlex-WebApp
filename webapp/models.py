"""Database models for the EasyFlex web application."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import inspect, text

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


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
		db.create_all()
		# Lightweight migration: add config_overrides column if absent (SQLite only)
		engine = db.engine
		insp = inspect(engine)
		columns = {col["name"] for col in insp.get_columns("user_settings")}
		if "config_overrides" not in columns:
			with engine.begin() as conn:
				conn.execute(text("ALTER TABLE user_settings ADD COLUMN config_overrides TEXT"))
		user_columns = {col["name"] for col in insp.get_columns("user")}
		if "credits" not in user_columns:
			table_name = '"user"' if engine.dialect.name == "postgresql" else "user"
			with engine.begin() as conn:
				conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN credits INTEGER DEFAULT 100 NOT NULL"))
		batch_columns = {col["name"] for col in insp.get_columns("invoice_batch")}
		with engine.begin() as conn:
			if "processing_status" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN processing_status VARCHAR(32) DEFAULT 'completed' NOT NULL"))
			if "total_files" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN total_files INTEGER DEFAULT 0 NOT NULL"))
			if "processed_files" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN processed_files INTEGER DEFAULT 0 NOT NULL"))
			if "processed_invoices" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN processed_invoices INTEGER DEFAULT 0 NOT NULL"))
			if "total_invoices_estimate" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN total_invoices_estimate INTEGER DEFAULT 0 NOT NULL"))
			if "current_phase" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN current_phase VARCHAR(64)"))
			if "success_count" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN success_count INTEGER DEFAULT 0 NOT NULL"))
			if "error_count" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN error_count INTEGER DEFAULT 0 NOT NULL"))
			if "credits_charged" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN credits_charged INTEGER DEFAULT 0 NOT NULL"))
			if "started_at" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN started_at TIMESTAMP"))
			if "finished_at" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN finished_at TIMESTAMP"))
			if "last_heartbeat_at" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN last_heartbeat_at TIMESTAMP"))
			if "summary_message" not in batch_columns:
				conn.execute(text("ALTER TABLE invoice_batch ADD COLUMN summary_message TEXT"))
			# Normalize nulls in legacy rows and ensure defaults are usable in UI/progress.
			conn.execute(text(
				"UPDATE invoice_batch "
				"SET processing_status = COALESCE(NULLIF(processing_status, ''), 'completed'), "
				"total_files = COALESCE(total_files, 0), "
				"processed_files = COALESCE(processed_files, 0), "
				"processed_invoices = COALESCE(processed_invoices, 0), "
				"total_invoices_estimate = COALESCE(total_invoices_estimate, total_files, 0), "
				"success_count = COALESCE(success_count, 0), "
				"error_count = COALESCE(error_count, 0), "
				"credits_charged = COALESCE(credits_charged, 0)"
			))
			# If the process restarted while batch processing was in-flight, expose partial results as interrupted.
			conn.execute(text(
				"UPDATE invoice_batch "
				"SET processing_status = 'interrupted', "
				"finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP), "
				"summary_message = COALESCE(summary_message, 'Zpracování bylo přerušeno restartem serveru.') "
				"WHERE processing_status IN ('queued', 'running')"
			))
