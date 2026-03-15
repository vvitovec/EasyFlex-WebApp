"""Helpers for robust CZK/EUR currency handling across extract/import flows."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Optional, Set, Tuple


SUPPORTED_CURRENCIES: tuple[str, str] = ("CZK", "EUR")

_RE_EUR_CODE = re.compile(r"(?<![A-Z0-9])(?:EUR|EURO|EUROS)(?![A-Z0-9])")
_RE_CZK_CODE = re.compile(r"(?<![A-Z0-9])CZK(?![A-Z0-9])")
_RE_KC_CODE = re.compile(r"(?<![A-Z0-9])K(?:\s|[.,-])?C(?![A-Z0-9])")
_RE_STRIP_MARKERS = re.compile(r"(?i)\b(?:CZK|EUR|EURO|EUROS|KČ|KC)\b|€")


def _to_text(value: Any) -> str:
	if value is None:
		return ""
	return str(value).strip()


def _fold_ascii_upper(value: Any) -> str:
	text = _to_text(value)
	if not text:
		return ""
	normalized = unicodedata.normalize("NFKD", text)
	ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
	return ascii_text.upper()


def detect_currency_tokens(value: Any) -> Set[str]:
	"""Return detected supported currencies from an arbitrary string-like value."""
	raw = _to_text(value)
	if not raw:
		return set()
	folded = _fold_ascii_upper(raw)
	detected: Set[str] = set()
	if "€" in raw or _RE_EUR_CODE.search(folded):
		detected.add("EUR")
	if _RE_CZK_CODE.search(folded) or _RE_KC_CODE.search(folded):
		detected.add("CZK")
	return detected


def normalize_currency(raw: Any) -> Optional[str]:
	"""Normalize raw currency value to canonical CZK/EUR; return None if unknown/ambiguous."""
	text = _to_text(raw)
	if not text:
		return None
	direct = _fold_ascii_upper(text)
	if direct in SUPPORTED_CURRENCIES:
		return direct
	detected = detect_currency_tokens(text)
	if len(detected) == 1:
		return next(iter(detected))
	return None


def infer_currency_from_texts(values: Iterable[Any]) -> Tuple[Optional[str], bool]:
	"""Infer currency from multiple texts. Returns (currency, has_conflict)."""
	seen: Set[str] = set()
	for value in values:
		seen.update(detect_currency_tokens(value))
	if len(seen) == 1:
		return next(iter(seen)), False
	if len(seen) > 1:
		return None, True
	return None, False


def resolve_currency(
	explicit_raw: Any = None,
	inferred_raw: Any = None,
	*,
	fallback: str = "CZK",
) -> str:
	"""Apply policy explicit > inferred > fallback, always returning supported canonical code."""
	explicit = normalize_currency(explicit_raw)
	if explicit:
		return explicit
	inferred = normalize_currency(inferred_raw)
	if inferred:
		return inferred
	fallback_norm = normalize_currency(fallback)
	return fallback_norm or "CZK"


def strip_known_currency_markers(value: Any) -> str:
	"""Remove known CZK/EUR symbols/labels from amount text."""
	text = _to_text(value)
	if not text:
		return ""
	return _RE_STRIP_MARKERS.sub("", text)


def currency_reference(raw: Any, *, fallback: str = "CZK") -> str:
	"""Return ABRA relation reference for currency, e.g. code:CZK."""
	return f"code:{resolve_currency(raw, fallback=fallback)}"

