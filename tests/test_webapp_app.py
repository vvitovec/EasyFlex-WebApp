import importlib

from sqlalchemy.exc import OperationalError


def _load_app_module(monkeypatch):
	import webapp.models as models

	monkeypatch.setattr(models, "init_db", lambda app: None)
	import webapp.app as app_module
	return importlib.reload(app_module)


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
