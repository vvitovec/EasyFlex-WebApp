"""Helpers for building per-user EasyFlex configuration objects."""
from __future__ import annotations

from copy import deepcopy

from flask import current_app
from flask_login import current_user

from EasyFlex.config import load_config, AppConfig

from .models import UserSettings, db


def _ensure_base_config() -> AppConfig:
	"""Return cached base config or load it once."""
	cfg = current_app.config.get("EASYFLEX_BASE_CONFIG")
	if cfg is None:
		cfg = load_config()
		current_app.config["EASYFLEX_BASE_CONFIG"] = cfg
	return cfg


def get_user_config() -> AppConfig:
	"""Return a deep-copied AppConfig with overrides from current user's settings."""
	base_cfg = _ensure_base_config()
	# Ensure settings row exists for current user
	settings = getattr(current_user, "settings", None)
	if settings is None:
		settings = UserSettings(user=current_user)
		# Pre-fill with base values so the settings page is informative
		# Sensitive values must stay empty for new users
		settings.abra_company = None
		settings.abra_verify_tls = getattr(base_cfg, "abra_verify_tls", True)
		settings.config_overrides = {}
		db.session.add(settings)
		db.session.commit()
	elif settings.config_overrides is None:
		settings.config_overrides = {}
		db.session.commit()
	# Build per-user copy
	cfg = deepcopy(base_cfg)
	# Credentials are per-user only: do not inherit from base config
	cfg.openai_api_key = settings.openai_api_key or None
	cfg.abra_server = settings.abra_server or None
	cfg.abra_port = settings.abra_port if settings.abra_port is not None else None
	# Použij uloženou firmu, pokud ji má uživatel nastavenou (jinak ponecháme hodnotu ze základní konfigurace)
	if settings.abra_company:
		cfg.abra_company = settings.abra_company
	cfg.abra_username = settings.abra_username or None
	cfg.abra_password = settings.abra_password or None
	if settings.abra_verify_tls is not None:
		cfg.abra_verify_tls = bool(settings.abra_verify_tls)
	overrides = settings.config_overrides or {}

	def _as_bool(value):
		if isinstance(value, str):
			return value.strip().lower() in {"1", "true", "yes", "on", "ano"}
		return bool(value)

	def _apply_override(attr: str, caster=None):
		if attr not in overrides:
			return
		value = overrides.get(attr)
		if caster is not None and value is not None:
			try:
				value = caster(value)
			except Exception:
				return
		setattr(cfg, attr, value)

	_apply_override("openai_model", str)
	_apply_override("concurrency", int)
	_apply_override("max_tokens", int)
	_apply_override("dpi", int)
	_apply_override("max_pages", int)
	_apply_override("openai_request_delay", float)
	_apply_override("openai_max_retries", int)
	_apply_override("image_max_width", int)
	_apply_override("image_jpeg_quality", int)
	_apply_override("use_doc_number_as_variable_symbol", _as_bool)
	_apply_override("infer_missing_dates", _as_bool)
	_apply_override("enable_multi_invoice_segmentation", _as_bool)
	_apply_override("csv_enable_llm_mapping", _as_bool)
	_apply_override("date_day_first", _as_bool)
	_apply_override("abra_doc_endpoint", str)
	_apply_override("abra_doc_type_code", str)
	# Store optional convenience values that are not part of AppConfig
	if "auto_import" in overrides:
		try:
			setattr(cfg, "auto_import", _as_bool(overrides.get("auto_import")))
		except Exception:
			pass
	return cfg
