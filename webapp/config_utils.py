"""Helpers for building per-user EasyFlex configuration objects."""
from __future__ import annotations

from copy import deepcopy

from flask import current_app
from flask_login import current_user

from EasyFlex.config import load_config, AppConfig

from .models import UserSettings, User, db

EXTRACTOR_OVERRIDE_KEYS = {
	"openai_model",
	"openai_reasoning_effort",
	"concurrency",
	"max_tokens",
	"dpi",
	"max_pages",
	"openai_request_delay",
	"openai_timeout_s",
	"openai_connection_timeout_s",
	"openai_max_retries",
	"image_max_width",
	"image_jpeg_quality",
}

OVERRIDE_CASTERS = {
	"openai_model": str,
	"openai_reasoning_effort": str,
	"concurrency": int,
	"max_tokens": int,
	"dpi": int,
	"max_pages": int,
	"openai_request_delay": float,
	"openai_timeout_s": int,
	"openai_connection_timeout_s": int,
	"openai_max_retries": int,
	"image_max_width": int,
	"image_jpeg_quality": int,
	"use_doc_number_as_variable_symbol": bool,
	"infer_missing_dates": bool,
	"enable_multi_invoice_segmentation": bool,
	"csv_enable_llm_mapping": bool,
	"date_day_first": bool,
	"abra_doc_endpoint": str,
	"abra_doc_type_code": str,
	"abra_use_kod": bool,
	"abra_duplicate_kod_strategy": str,
}

NON_APPCONFIG_OVERRIDES = {"auto_import"}


def _as_bool(value):
	if isinstance(value, str):
		return value.strip().lower() in {"1", "true", "yes", "on", "ano"}
	return bool(value)


def _ensure_settings_row(user, base_cfg: AppConfig) -> UserSettings:
	settings = getattr(user, "settings", None)
	if settings is None:
		settings = UserSettings(user=user)
		settings.abra_company = None
		settings.abra_verify_tls = getattr(base_cfg, "abra_verify_tls", True)
		settings.config_overrides = {}
		db.session.add(settings)
		db.session.commit()
	elif settings.config_overrides is None:
		settings.config_overrides = {}
		db.session.commit()
	return settings


def _get_admin_settings(base_cfg: AppConfig) -> UserSettings | None:
	admin = User.query.filter_by(is_admin=True).order_by(User.id.asc()).first()
	if admin is None:
		return None
	return _ensure_settings_row(admin, base_cfg)


def _apply_overrides(cfg: AppConfig, overrides: dict) -> None:
	for attr, caster in OVERRIDE_CASTERS.items():
		if attr not in overrides:
			continue
		value = overrides.get(attr)
		if caster is bool:
			value = _as_bool(value)
		elif value is not None:
			try:
				value = caster(value)
			except Exception:
				continue
		setattr(cfg, attr, value)
	# Store optional convenience values that are not part of AppConfig
	if "auto_import" in overrides:
		try:
			setattr(cfg, "auto_import", _as_bool(overrides.get("auto_import")))
		except Exception:
			pass


def _ensure_base_config() -> AppConfig:
	"""Return cached base config or load it once."""
	cfg = current_app.config.get("EASYFLEX_BASE_CONFIG")
	if cfg is None:
		cfg = load_config()
		current_app.config["EASYFLEX_BASE_CONFIG"] = cfg
	return cfg


def _build_user_config(user: User, base_cfg: AppConfig) -> AppConfig:
	"""Return a deep-copied AppConfig with overrides from the provided user settings."""
	settings = _ensure_settings_row(user, base_cfg)
	admin_settings = _get_admin_settings(base_cfg)
	# Build per-user copy
	cfg = deepcopy(base_cfg)
	# Credentials are per-user only: do not inherit from base config (except extractor for non-admins)
	if user.is_admin:
		cfg.openai_api_key = settings.openai_api_key or None
	elif admin_settings is not None:
		cfg.openai_api_key = admin_settings.openai_api_key or None
	else:
		cfg.openai_api_key = None
	cfg.abra_server = settings.abra_server or None
	cfg.abra_port = settings.abra_port if settings.abra_port is not None else None
	# Použij uloženou firmu, pokud ji má uživatel nastavenou (jinak ponecháme hodnotu ze základní konfigurace)
	if settings.abra_company:
		cfg.abra_company = settings.abra_company
	cfg.abra_username = settings.abra_username or None
	cfg.abra_password = settings.abra_password or None
	if settings.abra_verify_tls is not None:
		cfg.abra_verify_tls = bool(settings.abra_verify_tls)
	user_overrides = dict(settings.config_overrides or {})
	admin_overrides = dict(admin_settings.config_overrides or {}) if admin_settings else {}
	# For non-admin users, copy extractor overrides from admin and ignore personal overrides
	if not user.is_admin:
		for key in EXTRACTOR_OVERRIDE_KEYS:
			if key in admin_overrides:
				user_overrides[key] = admin_overrides[key]
			elif key in user_overrides:
				user_overrides.pop(key, None)
	_apply_overrides(cfg, user_overrides)
	return cfg


def get_user_config() -> AppConfig:
	"""Return a deep-copied AppConfig with overrides from current user's settings."""
	base_cfg = _ensure_base_config()
	return _build_user_config(current_user, base_cfg)


def get_user_config_for_user(user: User) -> AppConfig:
	"""Return AppConfig for an explicit user (safe to use in background jobs)."""
	base_cfg = _ensure_base_config()
	return _build_user_config(user, base_cfg)
