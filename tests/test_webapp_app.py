import importlib
import io

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


def test_admin_init_operational_error_is_handled(monkeypatch) -> None:
	app_module = _load_app_module(monkeypatch)
	app = app_module.create_app()
	monkeypatch.setenv("EASYFLEX_ADMIN_PASSWORD", "secret")

	class _DummyQuery:
		def filter_by(self, **kwargs):
			return self

		def first(self):
			raise OperationalError("SELECT 1", {}, Exception("boom"))

	monkeypatch.setattr(app_module.User, "query", _DummyQuery())

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
		run_once()

	assert rollback_called["value"] is True
	assert remove_called["value"] is True
	assert app.config.get("_ADMIN_INITIALIZED") is False


def test_upload_pdf_creates_queued_batch_and_redirects(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	app.config["TESTING"] = True
	_create_user(app_module, app)
	monkeypatch.setattr(app_module, "ensure_seed_data", lambda *_args, **_kwargs: None)

	captured = {}

	def _fake_start(app_obj, *, batch_id, user_id, saved_files, job_dir, runtime_limit_s):
		captured["app_obj"] = app_obj
		captured["batch_id"] = batch_id
		captured["user_id"] = user_id
		captured["saved_files"] = saved_files
		captured["job_dir"] = job_dir
		captured["runtime_limit_s"] = runtime_limit_s

	monkeypatch.setattr(app_module, "_start_pdf_batch_job", _fake_start)

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
	assert "batch_id" in captured
	assert len(captured["saved_files"]) == 2

	with app.app_context():
		batch = app_module.db.session.get(app_module.InvoiceBatch, captured["batch_id"])
		assert batch is not None
		assert batch.processing_status == "queued"
		assert batch.total_files == 2
		assert batch.processed_files == 0
		assert batch.success_count == 0
		assert batch.error_count == 0


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
	assert payload["row_count"] == 1
	assert payload["is_terminal"] is False
