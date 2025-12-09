"""Utility helpers for working with invoice warning texts.

Centralises how the transient `upozorneni` field is stored on invoice
structures so UI code can rely on a consistent behaviour even when the
underlying implementation uses Pydantic models or plain dictionaries.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


FIELD_NAME = "upozorneni"
_UI_DEFAULT = "bez problému"


def _normalize(text: Optional[str]) -> Optional[str]:
	if text is None:
		return None
	clean = str(text).strip()
	if not clean or clean.lower() == _UI_DEFAULT:
		return None
	return clean


def _set_on_object(obj: Any, value: Optional[str]) -> bool:
	try:
		setattr(obj, FIELD_NAME, value)
		return True
	except AttributeError:
		return False
	except Exception:
		return False


def _set_via_object_setattr(obj: Any, value: Optional[str]) -> bool:
	try:
		object.__setattr__(obj, FIELD_NAME, value)
		return True
	except Exception:
		return False


def _set_in_dict(obj: Any, value: Optional[str]) -> bool:
	if not hasattr(obj, "__dict__"):
		return False
	if value is None:
		obj.__dict__.pop(FIELD_NAME, None)
	else:
		obj.__dict__[FIELD_NAME] = value
	return True


def set_warning(invoice: Any, value: Optional[str]) -> None:
	"""Set or clear the warning string on an invoice-like object."""
	normalized = _normalize(value)
	if isinstance(invoice, dict):
		if normalized is None:
			invoice.pop(FIELD_NAME, None)
		else:
			invoice[FIELD_NAME] = normalized
		return
	if normalized is None:
		if _set_on_object(invoice, None):
			return
		if _set_via_object_setattr(invoice, None):
			return
		_set_in_dict(invoice, None)
		return
	if _set_on_object(invoice, normalized):
		return
	if _set_via_object_setattr(invoice, normalized):
		return
	_set_in_dict(invoice, normalized)


def clear_warning(invoice: Any) -> None:
	"""Remove any warning from the invoice-like object."""
	set_warning(invoice, None)


def get_warning(invoice: Any, default: Optional[str] = None) -> Optional[str]:
	"""Fetch the stored warning text, if any."""
	value: Optional[str]
	if isinstance(invoice, dict):
		value = invoice.get(FIELD_NAME)
	else:
		value = getattr(invoice, FIELD_NAME, None)
		if value is None and hasattr(invoice, "__dict__"):
			value = invoice.__dict__.get(FIELD_NAME)
	return value if value is not None else default


def append_warnings(invoice: Any, warnings: Iterable[str]) -> None:
	"""Merge additional warnings into the invoice warning string."""
	new_entries = []
	for warn in warnings:
		norm = _normalize(warn)
		if norm:
			new_entries.append(norm)
	if not new_entries:
		return
	existing_raw = get_warning(invoice)
	ordered: list[str] = []
	if existing_raw:
		existing_parts = [part.strip() for part in str(existing_raw).split("|")]
		for part in existing_parts:
			norm = _normalize(part)
			if norm and norm not in ordered:
				ordered.append(norm)
	for entry in new_entries:
		if entry not in ordered:
			ordered.append(entry)
	if ordered:
		set_warning(invoice, " | ".join(ordered))
	else:
		clear_warning(invoice)

