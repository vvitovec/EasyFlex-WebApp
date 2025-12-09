"""Flask web layer for EasyFlex."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from flask import (
	Flask,
	render_template,
	request,
	redirect,
	url_for,
	flash,
	abort,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from EasyFlex.config import load_config
from EasyFlex.extractor import InvoiceExtractor, ExtractResult
from EasyFlex.csv_processor import CSVProcessor
from EasyFlex.abra import import_to_abra
from EasyFlex.date_helpers import DATE_FIELDS, parse_invoice_date
from EasyFlex.models import InvoiceData

from .models import init_db, db, User, InvoiceBatch, InvoiceRow
from .auth import init_auth
from .config_utils import get_user_config
from .invoice_batches import (
	DISPLAY_COLUMNS,
	COLUMN_LABELS,
	apply_invoice_updates,
	create_batch_from_invoices,
	create_batch_from_results,
	load_batch_for_user,
	rows_for_display,
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

logger = logging.getLogger(__name__)


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


def _run_extraction(extractor: InvoiceExtractor, pdf_path: Path) -> List[ExtractResult]:
	"""Run async extractor in a blocking context."""
	try:
		return asyncio.run(extractor.extract_auto(str(pdf_path)))
	except RuntimeError:
		loop = asyncio.new_event_loop()
		try:
			return loop.run_until_complete(extractor.extract_auto(str(pdf_path)))
		finally:
			loop.close()


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


def _import_batch(batch: InvoiceBatch, cfg: Any, selected_ids: Optional[List[int]]) -> tuple[int, int, int]:
	"""Import selected rows to ABRA using current config."""
	if selected_ids is None:
		selected_ids = [row.id for row in batch.rows if row.marked_for_import]
	selected_set = {int(val) for val in selected_ids}
	success = 0
	error = 0
	skipped = 0
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
		try:
			resp = import_to_abra(payload, cfg=cfg)
			if resp is None:
				raise RuntimeError("Import se nepodařil (zkontrolujte logy).")
			row.status = "Importováno"
			row.error = None
			success += 1
		except Exception as exc:  # noqa: BLE001
			logger.exception("Chyba importu do ABRA")
			row.error = str(exc)
			row.status = None
			error += 1
	db.session.commit()
	return success, error, skipped


def create_app() -> Flask:
	load_dotenv()
	app = Flask(__name__)
	base_dir = Path(__file__).resolve().parent
	db_path = base_dir / "easyflex_web.db"

	app.config["SECRET_KEY"] = os.getenv("EASYFLEX_SECRET_KEY", "dev-secret-key")
	app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
	app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

	# Load shared EasyFlex configuration (per-user overrides are applied later)
	app.config["EASYFLEX_BASE_CONFIG"] = load_config()

	init_db(app)
	init_auth(app)

	@app.route("/")
	@login_required
	def dashboard():
		return render_template("dashboard.html")

	@app.route("/upload-pdf", methods=["GET", "POST"])
	@login_required
	def upload_pdf():
		cfg = get_user_config()
		ensure_seed_data(current_user, cfg)
		if request.method == "POST" and not cfg.openai_api_key:
			flash("Nejprve vyplňte svůj OpenAI API klíč v Nastavení.", "warning")
			return redirect(url_for("user_settings"))
		if request.method == "POST":
			file = request.files.get("pdf")
			if file is None or file.filename == "":
				flash("Vyberte prosím PDF soubor.", "warning")
				return render_template("upload_pdf.html")
			filename = secure_filename(file.filename) or "upload.pdf"
			with tempfile.TemporaryDirectory() as tmpdir:
				pdf_path = Path(tmpdir) / filename
				file.save(pdf_path)
				extractor = InvoiceExtractor(config=cfg)
				try:
					results = _run_extraction(extractor, pdf_path)
				except Exception as exc:  # noqa: BLE001
					logger.exception("Chyba při extrakci PDF")
					return render_template("upload_pdf.html", error=str(exc))
			batch = create_batch_from_results(current_user, results, filename, source_type="pdf")
			if any(getattr(res, "error", None) for res in results):
				flash("Některé faktury obsahují chybu extrakce.", "warning")
			if getattr(cfg, "auto_import", False):
				company_code, direction, doc_type_code = current_context(current_user.settings, base_cfg=cfg)
				apply_context_to_config(cfg, company_code, direction, doc_type_code)
				_import_batch(batch, cfg, None)
				flash("Auto-import dokončen (viz statusy níže).", "info")
			return redirect(url_for("view_results", batch_id=batch.id))
		return render_template("upload_pdf.html")

	@app.route("/upload-table", methods=["GET", "POST"])
	@login_required
	def upload_table():
		cfg = get_user_config()
		ensure_seed_data(current_user, cfg)
		if request.method == "POST":
			file = request.files.get("table")
			if file is None or file.filename == "":
				flash("Vyberte prosím CSV/XLSX/XML soubor.", "warning")
				return render_template("upload_table.html")
			filename = secure_filename(file.filename) or "tabulka"
			with tempfile.TemporaryDirectory() as tmpdir:
				table_path = Path(tmpdir) / filename
				file.save(table_path)
				processor = CSVProcessor(config=cfg)
				try:
					invoices = processor.process_table_file(str(table_path))
				except Exception as exc:  # noqa: BLE001
					logger.exception("Chyba při zpracování tabulky")
					return render_template("upload_table.html", error=str(exc))
			batch = create_batch_from_invoices(current_user, invoices, filename, source_type="table")
			if getattr(cfg, "auto_import", False):
				company_code, direction, doc_type_code = current_context(current_user.settings, base_cfg=cfg)
				apply_context_to_config(cfg, company_code, direction, doc_type_code)
				_import_batch(batch, cfg, None)
				flash("Auto-import dokončen (viz statusy níže).", "info")
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
		rows = rows_for_display(batch)
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
		return render_template(
			"results.html",
			batch=batch,
			rows=rows,
			allow_import=True,
			columns=DISPLAY_COLUMNS,
			column_labels=COLUMN_LABELS,
			companies=companies,
			doc_types=doc_types,
			selected_company=company_code,
			selected_direction=direction,
			selected_doc_type=doc_type_code,
		)

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
		error = None
		if request.method == "POST":
			updates: Dict[str, Any] = {}
			for field, _label in EDITABLE_FIELDS:
				raw = (request.form.get(field) or "").strip()
				if not raw:
					updates[field] = None
					continue
				if field in DATE_FIELDS:
					parsed = parse_invoice_date(raw, day_first=getattr(cfg, "date_day_first", True))
					if parsed is None:
						error = f"Pole '{field}' obsahuje neplatné datum."
						break
					updates[field] = parsed
				elif field in FLOAT_FIELDS:
					try:
						updates[field] = _parse_float_value(raw)
					except ValueError:
						error = f"Pole '{field}' musí být číslo."
						break
				else:
					updates[field] = raw
			if error is None:
				apply_invoice_updates(row, updates, day_first=getattr(cfg, "date_day_first", True))
				flash("Úprava faktury uložena.", "success")
				return redirect(url_for("view_results", batch_id=row.batch_id))
		return render_template(
			"edit_invoice.html",
			row=row,
			invoice=invoice_data,
			error=error,
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
			settings.openai_api_key = (request.form.get("openai_api_key") or "").strip() or None
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

			_set_override("openai_model", (request.form.get("openai_model") or "").strip() or None)
			for int_field, form_key in (
				("concurrency", "concurrency"),
				("max_tokens", "max_tokens"),
				("dpi", "pdf_dpi"),
				("max_pages", "max_pages"),
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
				_set_override("enable_multi_invoice_segmentation", bool(request.form.get("enable_multi_invoice_segmentation")))
				_set_override("csv_enable_llm_mapping", bool(request.form.get("csv_enable_llm_mapping")))
				_set_override("auto_import", bool(request.form.get("auto_import")))
				date_order = (request.form.get("date_order") or "dd-mm").strip().lower()
				_set_override("date_day_first", False if date_order == "mm-dd" else True)
				if "abra_doc_endpoint" in request.form:
					_set_override("abra_doc_endpoint", (request.form.get("abra_doc_endpoint") or "").strip() or None)
				if "abra_doc_type_code" in request.form:
					_set_override("abra_doc_type_code", (request.form.get("abra_doc_type_code") or "").strip() or None)
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
		apply_context_to_config(cfg, company_code, direction, doc_type_code)
		selected_ids = [int(val) for val in request.form.getlist("import_row_ids")]
		if not selected_ids:
			flash("Vyberte alespoň jednu fakturu k importu.", "warning")
			return redirect(url_for("view_results", batch_id=batch.id))
		success_count, error_count, skipped_count = _import_batch(batch, cfg, selected_ids)
		message_parts = []
		if success_count:
			message_parts.append(f"Úspěšně importováno: {success_count}")
		if error_count:
			message_parts.append(f"Chyby: {error_count}")
		if skipped_count:
			message_parts.append(f"Přeskočeno: {skipped_count}")
		if message_parts:
			flash("; ".join(message_parts), "info")
		rows = rows_for_display(batch)
		companies = list_companies(current_user)
		doc_types = list_doc_types(current_user, company_code, direction)
		return render_template(
			"results.html",
			batch=batch,
			rows=rows,
			allow_import=True,
			columns=DISPLAY_COLUMNS,
			column_labels=COLUMN_LABELS,
			companies=companies,
			doc_types=doc_types,
			selected_company=company_code,
			selected_direction=direction,
			selected_doc_type=doc_type_code,
		)

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
			if request.form.get("action") == "delete":
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
					db.session.add(user)
					db.session.commit()
					message = f"Uživatel {username} vytvořen."
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
			return

		admin = User.query.filter_by(username="admin").first()
		if admin is None:
			app.logger.info("ensure_admin_user: creating new admin user 'admin'")
			admin = User(username="admin", is_admin=True)
			admin.set_password(password)
			db.session.add(admin)
		else:
			app.logger.info(
				"ensure_admin_user: updating existing admin user id=%s", admin.id
			)
			admin.is_admin = True
			admin.set_password(password)

		db.session.commit()
		app.logger.info("ensure_admin_user: admin user saved")

	@app.before_request
	def _run_admin_init_once():
		"""Run ensure_admin_user() exactly once per process."""
		if not app.config.get("_ADMIN_INITIALIZED", False):
			ensure_admin_user()
			app.config["_ADMIN_INITIALIZED"] = True
			
	return app

app = create_app()


if __name__ == "__main__":
	app.run(debug=True)
