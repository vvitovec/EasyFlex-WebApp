"""Pomocné funkce pro práci s daty faktur."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config


DATE_FIELDS: Tuple[str, str, str] = ("datum_vystaveni", "datum_splatnosti", "datum_duzp")

DATE_FRIENDLY: Dict[str, str] = {
	"datum_vystaveni": "Datum vystavení",
	"datum_splatnosti": "Datum splatnosti",
	"datum_duzp": "Datum DUZP",
}


def _active_day_first() -> bool:
	"""Return current preference whether day comes before month."""
	try:
		cfg = load_config()
		return getattr(cfg, "date_day_first", True)
	except Exception:
		return True


def _parse_date(value: Any, day_first: Optional[bool] = None) -> Optional[date]:
	"""Vrátí `date`, pokud je hodnota rozpoznatelné datum."""
	if value is None:
		return None
	if isinstance(value, date) and not isinstance(value, datetime):
		return value
	if isinstance(value, datetime):
		return value.date()
	text = str(value).strip()
	if not text:
		return None
	# Normalizuj oddělovače, aby prošly i běžné formáty
	normalized = text.replace("/", "-").replace(".", "-")
	day_first_flag = _active_day_first() if day_first is None else day_first
	ambiguous_with_sep: Tuple[str, ...]
	ambiguous_compact: Tuple[str, ...]
	if day_first_flag:
		ambiguous_with_sep = ("%d-%m-%Y", "%m-%d-%Y")
		ambiguous_compact = ("%d%m%Y", "%m%d%Y")
	else:
		ambiguous_with_sep = ("%m-%d-%Y", "%d-%m-%Y")
		ambiguous_compact = ("%m%d%Y", "%d%m%Y")
	formats: Tuple[str, ...] = ("%Y-%m-%d",) + ambiguous_with_sep + ("%Y%m%d",) + ambiguous_compact
	for fmt in formats:
		try:
			return datetime.strptime(normalized, fmt).date()
		except ValueError:
			continue
	try:
		return datetime.fromisoformat(text).date()
	except ValueError:
		return None


def _format_date(value: date) -> str:
	"""Formátování na ISO yyyy-mm-dd."""
	return value.strftime("%Y-%m-%d")


def parse_invoice_date(value: Any, day_first: Optional[bool] = None) -> Optional[str]:
	"""Veřejná obálka pro parsování data na formát YYYY-MM-DD."""
	active_flag = _active_day_first() if day_first is None else day_first
	parsed = _parse_date(value, day_first=active_flag)
	return _format_date(parsed) if parsed else None


def domysleni_chybejicich_datumu(payload: Dict[str, Any], day_first: Optional[bool] = None) -> Tuple[Dict[str, Any], List[str]]:
	"""Doplní chybějící datumy podle dostupných hodnot.

	Funkce pracuje pouze s trojicí datumů. Pokud chybí všechny nebo
	nelze použít žádné z nich, vrací vstup beze změny.
	"""
	local_payload = dict(payload)
	day_first_flag = _active_day_first() if day_first is None else day_first
	parsed = {field: _parse_date(local_payload.get(field), day_first=day_first_flag) for field in DATE_FIELDS}
	missing_fields = [field for field, value in parsed.items() if value is None]
	available_fields = [field for field, value in parsed.items() if value is not None]
	if not missing_fields or not available_fields:
		return local_payload, []

	filled = dict(parsed)
	updated: Dict[str, date] = {}
	sources: Dict[str, str] = {}
	warnings: List[str] = []

	def _set_field(target: str, source: str, value: Optional[date]) -> None:
		if value is None or parsed.get(target) is not None:
			return
		filled[target] = value
		updated[target] = value
		sources[target] = source

	def _earliest(candidates: List[str]) -> Tuple[Optional[str], Optional[date]]:
		usable = [(field, filled[field]) for field in candidates if filled.get(field) is not None]
		if not usable:
			return None, None
		field, value = min(usable, key=lambda item: item[1])
		return field, value

	def _latest(candidates: List[str]) -> Tuple[Optional[str], Optional[date]]:
		usable = [(field, filled[field]) for field in candidates if filled.get(field) is not None]
		if not usable:
			return None, None
		field, value = max(usable, key=lambda item: item[1])
		return field, value

	# 1) DUZP ← nejbližší dostupné dřívější datum
	source_field, chosen = _earliest(["datum_vystaveni", "datum_splatnosti"])
	if filled.get("datum_duzp") is None and chosen is not None and source_field is not None:
		_set_field("datum_duzp", source_field, chosen)

	# 2) Datum splatnosti ← nejpozdější z ostatních
	source_field, chosen = _latest(["datum_vystaveni", "datum_duzp"])
	if filled.get("datum_splatnosti") is None and chosen is not None and source_field is not None:
		_set_field("datum_splatnosti", source_field, chosen)

	# 3) Datum vystavení ← ideálně mezi DUZP a splatností
	if filled.get("datum_vystaveni") is None:
		before = filled.get("datum_duzp")
		after = filled.get("datum_splatnosti")
		chosen_field: Optional[str] = None
		chosen_value: Optional[date] = None
		if before and after:
			if before <= after:
				chosen_field, chosen_value = "datum_duzp", before
			else:
				# Nekonzistence – držme se splatnosti, aby vystavení nebylo později než splatnost
				chosen_field, chosen_value = "datum_splatnosti", after
		elif before:
			chosen_field, chosen_value = "datum_duzp", before
		elif after:
			chosen_field, chosen_value = "datum_splatnosti", after
		if chosen_field and chosen_value:
			_set_field("datum_vystaveni", chosen_field, chosen_value)

	if not updated:
		return local_payload, []

	for field, value in updated.items():
		local_payload[field] = _format_date(value)
		source = sources.get(field)
		friendly_field = DATE_FRIENDLY.get(field, field)
		if not source or source == field:
			warnings.append(f"{friendly_field} bylo dopočítáno z dostupných údajů.")
		else:
			source_name = DATE_FRIENDLY.get(source, source)
			warnings.append(f"{friendly_field} doplněno podle hodnoty {source_name}.")

	return local_payload, warnings
