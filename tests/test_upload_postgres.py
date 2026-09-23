"""Run with EASYFLEX_TEST_POSTGRES_URL pointing to an isolated easyflex_regression DB."""
import io
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


@pytest.fixture
def postgres_app(monkeypatch):
	url = os.environ.get("EASYFLEX_TEST_POSTGRES_URL")
	if not url:
		pytest.skip("Requires an isolated PostgreSQL regression database")
	parsed = make_url(url)
	assert parsed.get_backend_name() == "postgresql"
	assert parsed.database == "easyflex_regression", "Never run against an application database"
	engine = create_engine(parsed)
	schema = "regression_" + uuid4().hex
	with engine.begin() as conn:
		conn.execute(text(f'CREATE SCHEMA "{schema}"'))
	monkeypatch.setenv("DATABASE_URL", parsed.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False))
	monkeypatch.setenv("EASYFLEX_ENV", "testing")
	monkeypatch.delenv("EASYFLEX_ADMIN_PASSWORD", raising=False)
	monkeypatch.delenv("OPENAI_API_KEY", raising=False)
	import webapp.app as module
	app = module.create_app()
	app.config["TESTING"] = True
	try:
		yield module, app
	finally:
		with app.app_context():
			module.db.session.remove()
			module.db.engine.dispose()
		with engine.begin() as conn:
			conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
		engine.dispose()


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_long_filename_upload_preserves_sources_and_charges_once(postgres_app, legacy_schema):
	module, app = postgres_app
	from webapp.invoice_batches import create_batch_from_invoices
	with app.app_context():
		user = module.User(username="upload-regression", credits=20)
		user.set_password("regression-only")
		settings = module.UserSettings(user=user, config_overrides={"auto_import": False, "csv_enable_llm_mapping": False})
		module.db.session.add_all([user, settings])
		module.db.session.commit()
		original = create_batch_from_invoices(user, [{"cislo_dokladu": "existing"}], "existing.csv")
		original_id = original.id
		if legacy_schema:
			module.db.session.remove()
			with module.db.engine.begin() as conn:
				conn.execute(text("ALTER TABLE invoice_row ALTER COLUMN source TYPE VARCHAR(255)"))
	# Exercise the real startup migration twice, including its no-op second run.
	app = module.create_app()
	app = module.create_app()
	app.config["TESTING"] = True
	client = app.test_client()
	assert client.post("/login", data={"username": "upload-regression", "password": "regression-only"}).status_code == 302
	filename = "x" * 251 + ".csv"
	csv = "Číslo faktury,Datum vystavení,Datum splatnosti,Jméno,Částka celkem bez DPH v sazbě 21%\n"
	csv += "".join(f"REG-{i},1.9.2026,15.9.2026,Regression,100\n" for i in range(1, 13))
	response = client.post("/upload-table", data={"table": (io.BytesIO(csv.encode()), filename)})
	assert response.status_code == 302
	assert "/results/" in response.location
	assert client.get(response.location).status_code == 200
	with app.app_context():
		batch = module.InvoiceBatch.query.filter_by(source_label=filename).one()
		rows = module.InvoiceRow.query.filter_by(batch_id=batch.id).order_by(module.InvoiceRow.row_index).all()
		assert len(rows) == 12
		assert [row.source for row in rows] == [f"{filename} #{i}" for i in range(1, 13)]
		assert rows[-1].invoice_data["cislo_dokladu"] == "REG-12"
		assert batch.credits_charged == module.TABLE_UPLOAD_CREDIT_COST
		assert module.User.query.filter_by(username="upload-regression").one().credits == 20 - module.TABLE_UPLOAD_CREDIT_COST
		assert module.BatchJob.query.count() == 0
		assert module.InvoiceRow.query.filter_by(batch_id=original_id).one().source == "existing.csv #1"
		source = next(col for col in inspect(module.db.engine).get_columns("invoice_row") if col["name"] == "source")
		assert str(source["type"]) == "TEXT"
