import importlib
from types import SimpleNamespace
from datetime import timedelta


def _load_app_module_with_db(monkeypatch, tmp_path):
	monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test_batch_jobs.db'}")
	monkeypatch.delenv("EASYFLEX_ADMIN_PASSWORD", raising=False)
	import webapp.app as app_module
	return importlib.reload(app_module)


def _create_user(app_module, app, *, username: str = "admin"):
	with app.app_context():
		user = app_module.User(username=username, is_admin=True, credits=20)
		user.set_password("secret")
		settings = app_module.UserSettings(user=user, openai_api_key="sk-test")
		app_module.db.session.add_all([user, settings])
		app_module.db.session.commit()
		return user.id


def test_job_retry_delay_caps() -> None:
	from webapp.batch_jobs import job_retry_delay_s

	assert job_retry_delay_s(1) == 30
	assert job_retry_delay_s(2) == 60
	assert job_retry_delay_s(10) == 900


def test_claim_next_job_skips_future_retry(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)
	import webapp.batch_jobs as batch_jobs

	with app.app_context():
		job_now = app_module.BatchJob(
			batch_id=1,
			user_id=user_id,
			job_type="extract_pdf",
			status="queued",
			priority=0,
		)
		job_later = app_module.BatchJob(
			batch_id=2,
			user_id=user_id,
			job_type="extract_pdf",
			status="retryable_failed",
			priority=10,
			next_attempt_at=app_module.utcnow() + timedelta(minutes=5),
		)
		app_module.db.session.add_all([job_now, job_later])
		app_module.db.session.commit()

		claimed = batch_jobs.claim_next_job("worker-test")
		assert claimed is not None
		assert claimed.id == job_now.id


def test_requeue_stale_running_job_marks_failed_after_max_attempts(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)
	import webapp.batch_jobs as batch_jobs

	with app.app_context():
		batch = app_module.InvoiceBatch(user_id=user_id, source_label="Test", processing_status="running", active_job_type="extract_pdf")
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		job = app_module.BatchJob(
			batch_id=batch.id,
			user_id=user_id,
			job_type="extract_pdf",
			status="running",
			attempt_count=3,
			max_attempts=3,
			last_heartbeat_at=app_module.utcnow() - timedelta(minutes=10),
			claimed_by="worker-a",
		)
		app_module.db.session.add(job)
		app_module.db.session.commit()

		count = batch_jobs.requeue_stale_running_jobs(stale_after_s=30)
		assert count == 1
		job = app_module.db.session.get(app_module.BatchJob, job.id)
		assert job is not None
		assert job.status == "failed"
		assert job.next_attempt_at is None


def test_cleanup_old_terminal_jobs_removes_only_old_jobs(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)
	import webapp.batch_jobs as batch_jobs

	with app.app_context():
		old_job = app_module.BatchJob(
			batch_id=1,
			user_id=user_id,
			job_type="extract_pdf",
			status="completed",
			finished_at=app_module.utcnow() - timedelta(days=30),
		)
		new_job = app_module.BatchJob(
			batch_id=2,
			user_id=user_id,
			job_type="extract_pdf",
			status="completed",
			finished_at=app_module.utcnow() - timedelta(hours=2),
		)
		app_module.db.session.add_all([old_job, new_job])
		app_module.db.session.commit()

		removed = batch_jobs.cleanup_old_terminal_jobs(retention_hours=24)
		assert removed == 1
		assert app_module.db.session.get(app_module.BatchJob, old_job.id) is None
		assert app_module.db.session.get(app_module.BatchJob, new_job.id) is not None


def test_cleanup_orphaned_batch_storage_respects_existing_batch(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)
	import webapp.batch_jobs as batch_jobs

	with app.app_context():
		batch = app_module.InvoiceBatch(user_id=user_id, source_label="Keep me", processing_status="completed")
		app_module.db.session.add(batch)
		app_module.db.session.commit()

		root = app_module.Path(app.instance_path) / "batches"
		root.mkdir(parents=True, exist_ok=True)
		keep_dir = root / str(batch.id)
		keep_dir.mkdir(exist_ok=True)
		old_orphan = root / "99999"
		old_orphan.mkdir(exist_ok=True)
		(old_orphan / "payload.json").write_text("{}", encoding="utf-8")
		old_time = (app_module.utcnow() - timedelta(days=10)).timestamp()
		import os
		os.utime(old_orphan, (old_time, old_time))

		removed = batch_jobs.cleanup_orphaned_batch_storage(retention_hours=24)
		assert removed == 1
		assert keep_dir.exists()
		assert not old_orphan.exists()


def test_process_extract_job_survives_session_retries(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)
	import webapp.worker as worker_module
	worker_module = importlib.reload(worker_module)

	class _DummyInvoice:
		def model_dump(self):
			return {
				"cislo_dokladu": "INV-001",
				"odberatel_jmeno": "Test odberatel",
			}

	dummy_result = SimpleNamespace(
		data=_DummyInvoice(),
		error=None,
		warnings=[],
		_invoice_group_index=1,
	)

	monkeypatch.setattr(worker_module, "_build_extractor", lambda cfg: object())

	def _fake_extract(_extractor, _pdf_path, _display_name, *, on_result=None, max_attempts=2):
		if on_result is not None:
			on_result(dummy_result)
		return [dummy_result]

	monkeypatch.setattr(worker_module, "_extract_file_with_retry", _fake_extract)

	with app.app_context():
		batch = app_module.InvoiceBatch(
			user_id=user_id,
			source_label="PDF batch",
			source_type="pdf",
			processing_status="queued",
			total_files=1,
			total_invoices_estimate=1,
			active_job_type="extract_pdf",
		)
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		job = app_module.BatchJob(
			batch_id=batch.id,
			user_id=user_id,
			job_type="extract_pdf",
			status="running",
			claimed_by="worker-test",
			payload={"files": [{"relative_path": "sample.pdf", "display_name": "sample.pdf"}]},
			resume_cursor={"file_index": 0, "next_row_index": 0},
		)
		app_module.db.session.add(job)
		app_module.db.session.commit()

		pdf_path = worker_module.batch_storage_root(batch.id) / "sample.pdf"
		pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

		worker_module._process_extract_job(job.id, "worker-test")

		job = app_module.db.session.get(app_module.BatchJob, job.id)
		batch = app_module.db.session.get(app_module.InvoiceBatch, batch.id)
		row = app_module.InvoiceRow.query.filter_by(batch_id=batch.id).one()

		assert job is not None
		assert job.status == "completed"
		assert batch is not None
		assert batch.processing_status == "completed"
		assert row.invoice_data["cislo_dokladu"] == "INV-001"
		assert row.marked_for_import is True


def test_process_import_job_survives_session_retries(monkeypatch, tmp_path) -> None:
	app_module = _load_app_module_with_db(monkeypatch, tmp_path)
	app = app_module.create_app()
	user_id = _create_user(app_module, app)
	import webapp.worker as worker_module
	worker_module = importlib.reload(worker_module)

	monkeypatch.setattr(worker_module, "_import_to_abra", lambda payload, cfg: {"id": 123})

	with app.app_context():
		batch = app_module.InvoiceBatch(
			user_id=user_id,
			source_label="Import batch",
			source_type="pdf",
			processing_status="waiting_import",
			active_job_type="import_abra",
		)
		app_module.db.session.add(batch)
		app_module.db.session.flush()
		row = app_module.InvoiceRow(
			batch_id=batch.id,
			row_index=0,
			source="sample.pdf",
			invoice_data={"cislo_dokladu": "INV-002"},
			marked_for_import=True,
			import_status="pending",
		)
		job = app_module.BatchJob(
			batch_id=batch.id,
			user_id=user_id,
			job_type="import_abra",
			status="running",
			claimed_by="worker-test",
			payload={},
		)
		app_module.db.session.add_all([row, job])
		app_module.db.session.commit()

		worker_module._process_import_job(job.id, "worker-test")

		job = app_module.db.session.get(app_module.BatchJob, job.id)
		batch = app_module.db.session.get(app_module.InvoiceBatch, batch.id)
		row = app_module.db.session.get(app_module.InvoiceRow, row.id)

		assert job is not None
		assert job.status == "completed"
		assert batch is not None
		assert batch.processing_status == "completed"
		assert row is not None
		assert row.import_status == "imported"
