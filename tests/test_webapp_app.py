import importlib
import io
import json
from datetime import timedelta

from sqlalchemy.exc import OperationalError


def _load_app_module(monkeypatch):
	import webapp.models as models

	monkeypatch.setattr(models, "init_db", lambda app: None)
	import webapp.app as app_module
	return importlib.reload(app_module)


def _load_app_module_with_db(monkeypatch, tmp_path):
	monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test_webapp.db'}")
	monkeypatch.delenv("EASYFLEX_ADMIN_PASSWORD", raising=False)
	import webapp.app as app_module
	return importlib.reload(app_module)


def _create_user(app_module, app, *, username: str = "admin", password: str = "secret", credits: int = 20):
	with app.app_context():
		user = app_module.User(username=username, is_admin=True, credits=credits)
		user.set_password(password)
		settings = app_module.UserSettings(user=user, openai_api_key="sk-test")
		app_module.db.session.add(user)
		app_module.db.session.add(settings)
		app_module.db.session.commit()
		return user.id


def _login(client, username: str = "admin", password: str = "secret"):
	resp = client.post(
		"/login",
		data={"username": username, "password": password},
		follow_redirects=False,
	)
	assert resp.status_code == 302


def test_engine_options_set_for_postgres(monkeypatch) -> None:
	monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/dbname")
	app_module = _load_app_module(monkeypatch)
	app = app_module.create_app()
	assert app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql://")
	assert app.config["SQLALCHEMY_ENGINE_OPTIONS"] == {
		"pool_pre_ping": True,
		"pool_recycle": 300,
	}


def test_readyz_returns_ready(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True

	client = app.test_client()
	resp = client.get("/readyz")
	assert resp.status_code == 200
	assert resp.get_json()["status"] == "ready"


def test_readyz_returns_503_on_db_error(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True

	def _boom(*_args, **_kwargs):
		raise RuntimeError("db down")

	monkeypatch.setattr(app_module.db.session, "execute", _boom)

	client = app.test_client()
	resp = client.get("/readyz")
	assert resp.status_code == 503
	payload = resp.get_json()
	assert payload["status"] == "degraded"
	assert payload["database"] == "error"


def test_admin_init_operational_error_is_handled(monkeypatch) -> None:
	app_module = _load_app_module(monkeypatch)
	app = app_module.create_app()
	monkeypatch.setenv("EASYFLEX_ADMIN_PASSWORD", "secret")

	rollback_called = {"value": False}
	remove_called = {"value": False}

	def _rollback():
		rollback_called["value"] = True

	def _remove():
		remove_called["value"] = True

	monkeypatch.setattr(app_module.db.session, "rollback", _rollback)
	monkeypatch.setattr(app_module.db.session, "remove", _remove)

	before_funcs = app.before_request_funcs.get(None, [])
	run_once = next(func for func in before_funcs if func.__name__ == "_run_admin_init_once")

	with app.test_request_context("/"):
		class _DummyQuery:
			def filter_by(self, **kwargs):
				return self

			def first(self):
				raise OperationalError("SELECT 1", {}, Exception("boom"))

		monkeypatch.setattr(app_module.User, "query", _DummyQuery(), raising=False)
		run_once()

	assert rollback_called["value"] is True
	assert remove_called["value"] is True
	assert app.config.get("_ADMIN_INITIALIZED") is not True


def test_upload_pdf_creates_queued_batch_and_redirects(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	_create_user(app_module, app)
	monkeypatch.setattr(app_module, "ensure_seed_data", lambda *_args, **_kwargs: None)

	client = app.test_client()
	_login(client)
	resp = client.post(
		"/upload-pdf",
		data={
			"pdfs": [
				(io.BytesIO(b"%PDF-1.4 fake a"), "faktura-a.pdf"),
				(io.BytesIO(b"%PDF-1.4 fake b"), "faktura-b.pdf"),
			],
		},
		content_type="multipart/form-data",
		follow_redirects=False,
	)
	assert resp.status_code == 302
	assert "/results/" in resp.headers["Location"]

	with app.app_context():
		job = app_module.BatchJob.query.order_by(app_module.BatchJob.id.desc()).first()
		assert job is not None
		assert job.job_type == "extract_pdf"
		assert job.status == "queued"
		batch = app_module.db.session.get(app_module.InvoiceBatch, job.batch_id)
		assert batch is not None
		assert batch.processing_status == "queued"
		assert batch.total_files == 2
		assert batch.processed_files == 0
		assert batch.success_count == 0
		assert batch.error_count == 0
		payload = json.loads(app_module.batch_payload_path(batch.id).read_text(encoding="utf-8"))
		assert len(payload["files"]) == 2


def test_batch_progress_endpoint_returns_json(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	user_id = _create_user(app_module, app)
	monkeypatch.setattr(app_module, "ensure_seed_data", lambda *_args, **_kwargs: None)

	with app.app_context():
		user = app_module.db.session.get(app_module.User, user_id)
		batch = app_module.InvoiceBatch(
			user=user,
			source_label="Test batch",
			source_type="pdf",
			processing_status="running",
			total_files=3,
			processed_files=1,
			processed_invoices=2,
			total_invoices_estimate=5,
			current_phase="persisting",
			success_count=1,
			error_count=0,
			credits_charged=1,
			summary_message="Zpracovává se…",
		)
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		row = app_module.InvoiceRow(
			batch=batch,
			row_index=0,
			source="faktura-a.pdf",
			invoice_data={"cislo_dokladu": "A1"},
			warning=None,
			error=None,
			marked_for_import=True,
		)
		app_module.db.session.add(row)
		job = app_module.BatchJob(
			batch_id=batch.id,
			user_id=user.id,
			job_type="extract_pdf",
			status="retryable_failed",
			attempt_count=2,
			max_attempts=3,
			next_attempt_at=app_module._utcnow() + timedelta(seconds=45),
			last_heartbeat_at=app_module._utcnow() - timedelta(seconds=75),
			last_error="Test retry",
		)
		app_module.db.session.add(job)
		app_module.db.session.commit()
		batch_id = batch.id

	client = app.test_client()
	_login(client)
	resp = client.get(f"/results/{batch_id}/progress")
	assert resp.status_code == 200
	payload = resp.get_json()
	assert payload["status"] == "running"
	assert payload["total_files"] == 3
	assert payload["processed_files"] == 1
	assert payload["processed_invoices"] == 2
	assert payload["total_invoices_estimate"] == 5
	assert payload["current_phase"] == "persisting"
	assert payload["row_count"] == 1
	assert payload["selected_count"] == 0
	assert payload["is_terminal"] is False
	assert payload["next_retry_in_s"] is not None
	assert payload["active_job_attempt_count"] == 2
	assert payload["active_job_last_error"] == "Test retry"


def test_current_context_for_user_works_without_user_relationship(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)

	with app.app_context():
		settings = app_module.UserSettings.query.filter_by(user_id=user_id).first()
		assert settings is not None
		settings.abra_company = "DEMO"
		settings.config_overrides = {
			"abra_doc_endpoint": "faktura-vydana",
			"abra_doc_type_code": "FAK",
		}
		app_module.db.session.commit()

		user = app_module.db.session.get(app_module.User, user_id)
		app_module.db.session.expunge(user)

		company_code, direction, doc_type_code = app_module.current_context_for_user(user_id)

		assert company_code == "DEMO"
		assert direction == "faktura-vydana"
		assert doc_type_code == "FAK"


def test_get_user_config_for_user_works_without_user_relationship(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)

	with app.app_context():
		settings = app_module.UserSettings.query.filter_by(user_id=user_id).first()
		assert settings is not None
		settings.abra_server = "abra.local"
		settings.abra_port = 443
		settings.abra_verify_tls = True
		settings.config_overrides = {
			"openai_model": "gpt-5",
			"enable_multi_invoice_segmentation": True,
		}
		app_module.db.session.commit()

		user = app_module.db.session.get(app_module.User, user_id)
		app_module.db.session.expunge(user)

		cfg = app_module.get_user_config_for_user(user)

		assert cfg.abra_server == "abra.local"
		assert cfg.abra_port == 443
		assert cfg.enable_multi_invoice_segmentation is True


def test_dashboard_renders_recent_batches_and_problem_jobs(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	user_id = _create_user(app_module, app)

	with app.app_context():
		user = app_module.db.session.get(app_module.User, user_id)
		batch = app_module.InvoiceBatch(
			user=user,
			source_label="Domaci batch",
			source_type="pdf",
			processing_status="running",
			total_files=4,
			processed_files=1,
			processed_invoices=2,
			total_invoices_estimate=6,
			current_phase="extracting",
			started_at=app_module._utcnow() - timedelta(seconds=120),
			last_heartbeat_at=app_module._utcnow() - timedelta(seconds=20),
			summary_message="Test",
		)
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		job = app_module.BatchJob(
			batch_id=batch.id,
			user_id=user.id,
			job_type="extract_pdf",
			status="retryable_failed",
			attempt_count=1,
			max_attempts=3,
			next_attempt_at=app_module._utcnow() + timedelta(seconds=30),
		)
		app_module.db.session.add(job)
		app_module.db.session.commit()

	client = app.test_client()
	_login(client)
	resp = client.get("/")
	assert resp.status_code == 200
	body = resp.get_data(as_text=True)
	assert "Domaci batch" in body
	assert "Čeká na retry" in body


def test_dashboard_limits_preview_and_shows_csv_label(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	user_id = _create_user(app_module, app)

	with app.app_context():
		user = app_module.db.session.get(app_module.User, user_id)
		for idx in range(4):
			batch = app_module.InvoiceBatch(
				user=user,
				source_label=f"batch-{idx}.pdf",
				source_type="pdf",
				processing_status="completed",
				total_files=1,
				processed_files=1,
				processed_invoices=1,
				total_invoices_estimate=1,
				current_phase="done",
			)
			app_module.db.session.add(batch)
		csv_batch = app_module.InvoiceBatch(
			user=user,
			source_label="faktury.csv",
			source_type="table",
			processing_status="completed",
			total_files=1,
			processed_files=1,
			processed_invoices=5,
			total_invoices_estimate=5,
			current_phase="done",
		)
		app_module.db.session.add(csv_batch)
		app_module.db.session.flush()
		for idx in range(4):
			job = app_module.BatchJob(
				batch_id=csv_batch.id,
				user_id=user.id,
				job_type="extract_pdf",
				status="failed" if idx < 3 else "retryable_failed",
				attempt_count=idx + 1,
				max_attempts=4,
			)
			app_module.db.session.add(job)
		app_module.db.session.commit()

	client = app.test_client()
	_login(client)
	resp = client.get("/")
	assert resp.status_code == 200
	body = resp.get_data(as_text=True)
	assert "1/1 CSV" in body
	assert body.count("Zobrazit více") == 2
	assert "Promazat" in body


def test_dismiss_problem_job_removes_failed_job(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	user_id = _create_user(app_module, app)

	with app.app_context():
		user = app_module.db.session.get(app_module.User, user_id)
		batch = app_module.InvoiceBatch(user=user, source_label="Test", source_type="pdf", processing_status="failed")
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		job = app_module.BatchJob(
			batch_id=batch.id,
			user_id=user.id,
			job_type="extract_pdf",
			status="failed",
		)
		app_module.db.session.add(job)
		app_module.db.session.commit()
		job_id = job.id

	client = app.test_client()
	_login(client)
	resp = client.post(f"/jobs/{job_id}/dismiss", data={"next": "/"}, follow_redirects=False)
	assert resp.status_code == 302

	with app.app_context():
		assert app_module.db.session.get(app_module.BatchJob, job_id) is None


def test_clear_problem_jobs_keeps_running_jobs(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	user_id = _create_user(app_module, app)

	with app.app_context():
		user = app_module.db.session.get(app_module.User, user_id)
		batch = app_module.InvoiceBatch(user=user, source_label="Test", source_type="pdf", processing_status="running")
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		app_module.db.session.add_all([
			app_module.BatchJob(batch_id=batch.id, user_id=user.id, job_type="extract_pdf", status="failed"),
			app_module.BatchJob(batch_id=batch.id, user_id=user.id, job_type="extract_pdf", status="retryable_failed"),
			app_module.BatchJob(batch_id=batch.id, user_id=user.id, job_type="extract_pdf", status="running"),
		])
		app_module.db.session.commit()

	client = app.test_client()
	_login(client)
	resp = client.post("/jobs/attention/clear", data={"next": "/"}, follow_redirects=False)
	assert resp.status_code == 302

	with app.app_context():
		statuses = [job.status for job in app_module.BatchJob.query.order_by(app_module.BatchJob.id.asc()).all()]
		assert statuses == ["running"]


def test_admin_jobs_requires_admin(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	with app.app_context():
		user = app_module.User(username="user", is_admin=False, credits=20)
		user.set_password("secret")
		settings = app_module.UserSettings(user=user, openai_api_key="sk-test")
		app_module.db.session.add_all([user, settings])
		app_module.db.session.commit()

	client = app.test_client()
	_login(client, username="user", password="secret")
	resp = client.get("/admin/jobs")
	assert resp.status_code == 403


def test_selection_endpoint_persists_checkbox_state(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	user_id = _create_user(app_module, app)
	monkeypatch.setattr(app_module, "ensure_seed_data", lambda *_args, **_kwargs: None)

	with app.app_context():
		user = app_module.db.session.get(app_module.User, user_id)
		batch = app_module.InvoiceBatch(user=user, source_label="Test", source_type="table", processing_status="completed")
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		row = app_module.InvoiceRow(
			batch=batch,
			row_index=0,
			source="row-1",
			invoice_data={"cislo_dokladu": "A1"},
			marked_for_import=True,
		)
		app_module.db.session.add(row)
		app_module.db.session.commit()
		batch_id = batch.id
		row_id = row.id

	client = app.test_client()
	_login(client)
	resp = client.post(
		f"/results/{batch_id}/selection",
		json={"row_id": row_id, "marked": False},
	)
	assert resp.status_code == 200
	payload = resp.get_json()
	assert payload["marked"] is False

	with app.app_context():
		row = app_module.db.session.get(app_module.InvoiceRow, row_id)
		assert row is not None
		assert row.marked_for_import is False


def test_db_write_retry_retries_operational_error(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	call_state = {"calls": 0}

	def _writer():
		call_state["calls"] += 1
		if call_state["calls"] == 1:
			raise OperationalError("SELECT 1", {}, Exception("boom"))
		return "ok"

	with app.app_context():
		result = app_module._run_db_write_with_retry("test writer", _writer, max_attempts=2)
	assert result == "ok"
	assert call_state["calls"] == 2
