"""Helpers for managing per-user ABRA context (companies, doc types, selections)."""
from __future__ import annotations

from typing import Optional, Tuple

from EasyFlex.config import get_companies_from_settings, get_doc_types, load_config

from .models import Company, DocType, User, UserSettings, db


def _ensure_overrides(settings: UserSettings) -> dict:
	if settings.config_overrides is None:
		settings.config_overrides = {}
	return dict(settings.config_overrides)


def ensure_seed_data(user: User, base_cfg=None) -> None:
	"""Populate per-user companies/doc types from shared settings if missing."""
	if base_cfg is None:
		base_cfg = load_config()
	existing_codes = {c.code for c in user.companies}
	# Seed companies from shared INI
	for code, name in get_companies_from_settings():
		if code not in existing_codes:
			db.session.add(Company(user=user, code=code, name=name))
			existing_codes.add(code)
	# Ensure company from base config is present
	if getattr(base_cfg, "abra_company", None):
		code = base_cfg.abra_company
		if code not in existing_codes:
			db.session.add(Company(user=user, code=code, name=code))
			existing_codes.add(code)
	db.session.flush()
	# Seed doc types for known companies from shared INI
	for company_code in list(existing_codes):
		for direction in ("faktura-prijata", "faktura-vydana"):
			for dt in get_doc_types(company_code, direction):
				_add_doc_type_internal(user, company_code, direction, dt.get("name") or dt.get("nazev") or "", dt.get("code") or dt.get("kod") or "")
	db.session.commit()


def list_companies(user: User) -> list[Company]:
	"""Return companies sorted by name/code."""
	return Company.query.filter_by(user_id=user.id).order_by(Company.name.asc(), Company.code.asc()).all()


def add_company(user: User, code: str, name: str) -> Company:
	"""Create or update a company entry for the user."""
	code = (code or "").strip()
	name = (name or code or "").strip()
	company = Company.query.filter_by(user_id=user.id, code=code).first()
	if company:
		company.name = name or company.name
	else:
		company = Company(user=user, code=code, name=name or code)
		db.session.add(company)
	db.session.commit()
	return company


def list_doc_types(user: User, company_code: Optional[str], direction: str) -> list[DocType]:
	if not company_code:
		return []
	return (
		DocType.query.filter_by(user_id=user.id, company_code=company_code, direction=direction)
		.order_by(DocType.name.asc(), DocType.code.asc())
		.all()
	)


def list_all_doc_types(user: User) -> list[DocType]:
	"""Return all doc types for a user."""
	return (
		DocType.query.filter_by(user_id=user.id)
		.order_by(DocType.company_code.asc(), DocType.direction.asc(), DocType.name.asc())
		.all()
	)


def add_doc_type(user: User, company_code: str, direction: str, name: str, code: str) -> DocType:
	return _add_doc_type_internal(user, company_code, direction, name, code, commit=True)


def _add_doc_type_internal(user: User, company_code: str, direction: str, name: str, code: str, commit: bool = False) -> DocType:
	company = Company.query.filter_by(user_id=user.id, code=company_code).first()
	if company is None:
		company = Company(user=user, code=company_code, name=company_code)
		db.session.add(company)
	doc_type = (
		DocType.query.filter_by(user_id=user.id, company_code=company_code, direction=direction, code=code)
		.first()
	)
	if doc_type:
		doc_type.name = name or doc_type.name
	else:
		doc_type = DocType(
			user=user,
			company=company,
			company_code=company_code,
			direction=direction,
			name=name or code,
			code=code,
		)
		db.session.add(doc_type)
	if commit:
		db.session.commit()
	return doc_type


def persist_context(settings: UserSettings, company_code: Optional[str], direction: Optional[str], doc_type_code: Optional[str]) -> None:
	"""Store last-used ABRA context into settings overrides."""
	overrides = _ensure_overrides(settings)
	if company_code is not None:
		settings.abra_company = company_code or None
	if direction is not None:
		if direction:
			overrides["abra_doc_endpoint"] = direction
		else:
			overrides.pop("abra_doc_endpoint", None)
	if doc_type_code is not None:
		if doc_type_code:
			overrides["abra_doc_type_code"] = doc_type_code
		else:
			overrides.pop("abra_doc_type_code", None)
	# Assign a fresh dict so SQLAlchemy registers the JSON change
	settings.config_overrides = dict(overrides)
	db.session.commit()


def current_context(settings: UserSettings, base_cfg=None) -> Tuple[Optional[str], str, Optional[str]]:
	"""Return (company_code, direction, doc_type_code) with sensible defaults."""
	if base_cfg is None:
		base_cfg = load_config()
	overrides = settings.config_overrides or {}
	company = settings.abra_company or getattr(base_cfg, "abra_company", None)
	direction = overrides.get("abra_doc_endpoint") or getattr(base_cfg, "abra_doc_endpoint", "faktura-prijata") or "faktura-prijata"
	doc_type = overrides.get("abra_doc_type_code") or getattr(base_cfg, "abra_doc_type_code", None)
	return company, direction, doc_type


def apply_context_to_config(cfg, company_code: Optional[str], direction: Optional[str], doc_type_code: Optional[str]) -> None:
	"""Mutate AppConfig copy with the selected ABRA context."""
	if company_code:
		cfg.abra_company = company_code
	if direction:
		cfg.abra_doc_endpoint = direction
	if doc_type_code:
		cfg.abra_doc_type_code = doc_type_code


def delete_company(user: User, company_id: int) -> bool:
	company = Company.query.filter_by(id=company_id, user_id=user.id).first()
	if not company:
		return False
	db.session.delete(company)
	db.session.commit()
	return True


def delete_doc_type(user: User, doc_type_id: int) -> bool:
	doc_type = DocType.query.filter_by(id=doc_type_id, user_id=user.id).first()
	if not doc_type:
		return False
	db.session.delete(doc_type)
	db.session.commit()
	return True
