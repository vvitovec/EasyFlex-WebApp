"""Standalone ABRA company-work helpers.

This module intentionally stays separate from invoice and receipt workflows.
It only reuses the authenticated ABRA connection stored in the user's settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from requests.utils import quote

from EasyFlex.abra import (
	_build_base_url,
	_extract_winstrom_entries,
	_normalize_companies_payload,
	_request_with_retry,
)
from EasyFlex.config import AppConfig


class CompanyWorkError(RuntimeError):
	"""User-facing error raised for company-work ABRA operations."""


@dataclass(frozen=True)
class CompanyWorkAbraClient:
	"""Small ABRA client for the standalone company-work section."""

	cfg: AppConfig

	def __post_init__(self) -> None:
		if not (self.cfg.abra_server and self.cfg.abra_username and self.cfg.abra_password):
			raise CompanyWorkError("Pro práci s firmami vyplňte ABRA server, uživatele a heslo v Nastavení.")

	@property
	def base_url(self) -> str:
		return _build_base_url(str(self.cfg.abra_server), self.cfg.abra_port)

	@property
	def auth(self) -> tuple[str, str]:
		return str(self.cfg.abra_username), str(self.cfg.abra_password)

	@property
	def verify_tls(self) -> bool:
		return bool(getattr(self.cfg, "abra_verify_tls", True))

	@property
	def timeout_s(self) -> int:
		return int(getattr(self.cfg, "abra_timeout_s", 10) or 10)

	def _request_json(self, method: str, path: str, *, json_body: Optional[dict[str, Any]] = None) -> Any:
		resp = _request_with_retry(
			method,
			f"{self.base_url}{path}",
			auth=self.auth,
			timeout_s=self.timeout_s,
			json_body=json_body,
			verify=self.verify_tls,
		)
		if resp.status_code == 401:
			raise CompanyWorkError("ABRA odmítla přihlášení. Zkontrolujte uživatele a heslo v Nastavení.")
		if resp.status_code == 403:
			raise CompanyWorkError("ABRA nepovolila přístup k této evidenci pro aktuálního uživatele.")
		if resp.status_code >= 400:
			raise CompanyWorkError(_extract_abra_error(resp.text) or f"ABRA vrátila chybu HTTP {resp.status_code}.")
		try:
			return resp.json()
		except Exception as exc:  # noqa: BLE001
			raise CompanyWorkError("ABRA vrátila neplatnou JSON odpověď.") from exc

	def _get_entries(
		self,
		company_code: str,
		endpoint: str,
		keys: tuple[str, ...],
		*,
		query: str = "",
		fallback_query: str = "",
	) -> list[dict[str, Any]]:
		company = _quote_path(company_code)
		path = f"/c/{company}/{endpoint}.json{query}"
		try:
			payload = self._request_json("GET", path)
		except CompanyWorkError:
			if not fallback_query:
				raise
			payload = self._request_json("GET", f"/c/{company}/{endpoint}.json{fallback_query}")
		return _extract_winstrom_entries(payload, keys)

	def list_companies(self) -> list[dict[str, str]]:
		payload = self._request_json("GET", "/c.json?limit=0")
		companies = _normalize_companies_payload(payload)
		if not companies and isinstance(payload, dict):
			nested = payload.get("companies")
			if isinstance(nested, dict) and isinstance(nested.get("company"), list):
				companies = [item for item in nested["company"] if isinstance(item, dict)]
		result: list[dict[str, str]] = []
		for item in companies:
			code = _first_text(item, "dbNazev", "firma", "kod", "code", "id")
			if not code:
				continue
			name = _first_text(item, "nazev", "name", "showAs", "firma", "dbNazev") or code
			result.append({"code": code, "name": name})
		return sorted(result, key=lambda row: (row["name"].lower(), row["code"].lower()))

	def list_employees(self, company_code: str) -> list[dict[str, str]]:
		entries = self._get_entries(
			company_code,
			"osoba",
			("osoba", "items", "data", "result"),
			query="?limit=0&detail=custom:id,kod,osbCis,jmeno,prijmeni,osobaHlav",
			fallback_query="?limit=0",
		)
		employees: list[dict[str, str]] = []
		for entry in entries:
			employee_id = _first_text(entry, "id", "@id")
			if not employee_id:
				continue
			code = _first_text(entry, "kod", "code", "osbCis")
			first_name = _first_text(entry, "jmeno")
			last_name = _first_text(entry, "prijmeni")
			name = " ".join(part for part in (first_name, last_name) if part).strip()
			if not name:
				name = _first_text(entry, "showAs", "nazev") or code or employee_id
			# pracovni-pomer.osoba points to osoba-hlavicka, whose id is not
			# necessarily the same as the id of the employee record in osoba.
			# Filtering with the latter can silently return another person's jobs.
			employment_person_ref = _first_text(entry, "osobaHlav") or employee_id
			employees.append(
				{
					"id": employee_id,
					"code": code or "",
					"name": name,
					"ref": employment_person_ref,
					"label": f"{name} ({code})" if code and code not in name else name,
				}
			)
		return sorted(employees, key=lambda row: (row["name"].lower(), row["code"].lower()))

	def list_employments(self, company_code: str, employee_ref: str) -> list[dict[str, Any]]:
		company = _quote_path(company_code)
		filter_part = quote(f'(osoba="{employee_ref}")', safe='()=":')
		try:
			payload = self._request_json(
				"GET",
				f"/c/{company}/pracovni-pomer/{filter_part}.json?limit=0&detail=custom:id,kod,osoba,nazev,aktivniOd,aktivniDo,zacatek,skutecnyNastup,konecPomeru",
			)
		except CompanyWorkError:
			payload = self._request_json("GET", f"/c/{company}/pracovni-pomer/{filter_part}.json?limit=0")
		entries = _extract_winstrom_entries(payload, ("pracovni-pomer", "pracovniPomer", "items", "data", "result"))
		rows = [format_employment_summary(entry) for entry in entries if _first_text(entry, "id", "@id")]
		return sorted(rows, key=lambda row: (row["date_from"], row["id"]), reverse=True)

	def get_employment(self, company_code: str, employment_ref: str) -> dict[str, Any]:
		company = _quote_path(company_code)
		ref = _quote_path(employment_ref)
		payload = self._request_json("GET", f"/c/{company}/pracovni-pomer/{ref}.json?detail=full")
		entries = _extract_winstrom_entries(payload, ("pracovni-pomer", "pracovniPomer", "items", "data", "result"))
		if not entries:
			raise CompanyWorkError("ABRA nevrátila detail pracovního poměru.")
		return entries[0]

	def build_duplicate_payload(self, source: dict[str, Any], overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
		duplicate = sanitize_employment_copy(source)
		overrides = overrides or {}
		new_code = _clean_optional(overrides.get("kod"))
		if new_code:
			duplicate["kod"] = new_code
		else:
			duplicate.pop("kod", None)
		for target_key, override_key in (
			("nazev", "nazev"),
			("poznam", "poznam"),
		):
			value = _clean_optional(overrides.get(override_key))
			if value is not None:
				duplicate[target_key] = value
		date_from = _clean_optional(overrides.get("datumOd"))
		if date_from is not None:
			for target_key in ("aktivniOd", "zacatek", "skutecnyNastup"):
				duplicate[target_key] = date_from
		date_to = _clean_optional(overrides.get("datumDo"))
		if date_to is not None:
			for target_key in ("aktivniDo", "konecPomeru"):
				duplicate[target_key] = date_to
		return {
			"winstrom": {
				"@version": "1.0",
				"pracovni-pomer": [duplicate],
			}
		}

	def preview_duplicate(
		self,
		company_code: str,
		employment_ref: str,
		overrides: Optional[dict[str, Any]] = None,
	) -> dict[str, Any]:
		source = self.get_employment(company_code, employment_ref)
		payload = self.build_duplicate_payload(source, overrides)
		return {
			"source": format_employment_summary(source),
			"payload": payload,
			"field_count": len(payload["winstrom"]["pracovni-pomer"][0]),
		}

	def duplicate_employment(
		self,
		company_code: str,
		employment_ref: str,
		overrides: Optional[dict[str, Any]] = None,
	) -> dict[str, Any]:
		preview = self.preview_duplicate(company_code, employment_ref, overrides)
		company = _quote_path(company_code)
		response = self._request_json("POST", f"/c/{company}/pracovni-pomer.json", json_body=preview["payload"])
		return {
			"source": preview["source"],
			"payload": preview["payload"],
			"response": response,
			"created": _extract_created_summary(response),
		}


def format_employment_summary(entry: dict[str, Any]) -> dict[str, Any]:
	employment_id = _first_text(entry, "id", "@id")
	code = _first_text(entry, "kod", "code")
	name = _first_text(entry, "nazev", "showAs") or code or employment_id
	return {
		"id": employment_id,
		"code": code or "",
		"name": name or "",
		"ref": employment_id or code or "",
		"person": _relation_label(entry.get("osoba")),
		"date_from": _first_text(entry, "aktivniOd", "zacatek", "skutecnyNastup", "datumOd", "datOd", "platnostOd"),
		"date_to": _first_text(entry, "aktivniDo", "konecPomeru", "datumDo", "datDo", "platnostDo"),
		"raw": entry,
	}


def sanitize_employment_copy(source: dict[str, Any]) -> dict[str, Any]:
	"""Return a copy that is safe to POST as a new working relationship."""
	cleaned = _sanitize_value(source)
	if not isinstance(cleaned, dict):
		return {}
	for key in list(cleaned.keys()):
		if _is_readonly_key(key):
			cleaned.pop(key, None)
	cleaned.pop("id", None)
	cleaned.pop("kod", None)
	return cleaned


def _sanitize_value(value: Any) -> Any:
	if isinstance(value, dict):
		if "$" in value:
			non_attr_keys = [key for key in value if not str(key).startswith("@") and key != "$"]
			if not non_attr_keys:
				return _sanitize_value(value.get("$"))
		result: dict[str, Any] = {}
		for key, item in value.items():
			key_text = str(key)
			if key_text.startswith("@") or _is_readonly_key(key_text):
				continue
			cleaned = _sanitize_value(item)
			if cleaned in (None, "", [], {}):
				continue
			result[key_text] = cleaned
		return result
	if isinstance(value, list):
		result = [_sanitize_value(item) for item in value]
		return [item for item in result if item not in (None, "", [], {})]
	return value


def _is_readonly_key(key: str) -> bool:
	normalized = key.strip()
	lower = normalized.lower()
	if lower in {
		"id",
		"lastupdate",
		"lastupdateby",
		"url",
		"@id",
		"@version",
		"ineditaci",
		"pracpomhlav",
		"zamekk",
		"stavuzivk",
		"created",
		"createdat",
		"external-ids",
		"externalids",
		"extid",
		"updated",
		"updatedat",
	}:
		return True
	if lower.startswith("id"):
		return True
	return lower.endswith("@showas") or lower.endswith("@ref") or lower.endswith("@evidencepath")


def _extract_created_summary(response: Any) -> dict[str, str]:
	if not isinstance(response, dict):
		return {}
	results = response.get("winstrom", response).get("results") if isinstance(response.get("winstrom", response), dict) else None
	if isinstance(results, list) and results:
		first = results[0] if isinstance(results[0], dict) else {}
		return {
			"id": str(first.get("id") or first.get("ref") or ""),
			"status": str(first.get("status") or first.get("operation") or ""),
		}
	return {}


def _extract_abra_error(text: str) -> str:
	if not text:
		return ""
	return " ".join(text.strip().split())[:600]


def _first_text(entry: dict[str, Any], *keys: str) -> str:
	for key in keys:
		value = entry.get(key)
		text = _value_text(value)
		if text:
			return text
	return ""


def _value_text(value: Any) -> str:
	if value is None:
		return ""
	if isinstance(value, dict):
		for key in ("$", "value", "showAs", "@showAs", "id", "kod", "code"):
			if key in value:
				text = _value_text(value.get(key))
				if text:
					return text
		return ""
	text = str(value).strip()
	return text


def _relation_label(value: Any) -> str:
	if isinstance(value, dict):
		return _value_text(value.get("@showAs")) or _value_text(value)
	return _value_text(value)


def _clean_optional(value: Any) -> Optional[str]:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def _quote_path(value: str) -> str:
	return quote(str(value or "").strip(), safe=":-_")
