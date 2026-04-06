import json
import logging
import os
import time
from typing import Any, Dict, Optional, Union, Tuple
import re
import unicodedata
import hashlib

import requests

from .config import load_config, get_errors_dir, AppConfig
from .models import InvoiceData, InvoiceItem, VATRate
from .invoice_warnings import get_warning
from .invoice_processor import should_use_items_logic
from .date_helpers import domysleni_chybejicich_datumu
from .currency_utils import currency_reference, normalize_currency


logger = logging.getLogger(__name__)


def _build_base_url(server: str, port: Optional[int]) -> str:
	if port is None:
		return f"https://{server}"
	return f"https://{server}:{port}"


def _normalize_companies_payload(payload: Any) -> list[dict]:
	"""Try to normalize various server responses to a list of company dicts."""
	# Already a list of dicts
	if isinstance(payload, list):
		return [c for c in payload if isinstance(c, dict)]
	# Common wrappers
	if isinstance(payload, dict):
		for key in ("companies", "items", "c", "data", "result"):
			val = payload.get(key)
			if isinstance(val, list):
				return [c for c in val if isinstance(c, dict)]
		# Maybe single company object
		if any(k in payload for k in ("firma", "nazev", "ico")):
			return [payload]
	return []


def _extract_winstrom_entries(payload: Any, keys: tuple[str, ...]) -> list[dict]:
	"""Return list of entries from common winstrom JSON structures.

	Accepts payloads either as already unwrapped dicts/lists or wrapped in
	{"winstrom": {"<key>": [ ... ]}}. Only dictionaries are returned.
	"""
	if isinstance(payload, dict) and isinstance(payload.get("winstrom"), dict):
		payload = payload["winstrom"]
	if isinstance(payload, dict):
		for key in keys:
			val = payload.get(key)
			if isinstance(val, list):
				return [c for c in val if isinstance(c, dict)]
	if isinstance(payload, list):
		return [c for c in payload if isinstance(c, dict)]
	return []


def _wrap_winstrom(doc_endpoint: str, entry: Dict[str, Any]) -> Dict[str, Any]:
	"""Wrap a single entry into FlexiBee winstrom JSON envelope."""
	return {
		"winstrom": {
			"@version": "1.0",
			doc_endpoint: [entry],
		}
	}


def _normalize_country_reference(value: Optional[str]) -> Optional[str]:
	"""Return FlexiBee reference for 'stat' as code:XX when possible.

	Accepts names like "Česká republika", "Slovensko", or two-letter codes like "CZ".
	"""
	if not value:
		return None
	val = str(value).strip()
	if val.lower().startswith(("code:", "id:", "ext:")):
		return val
	# Common name → code mapping (can be extended)
	name_to_code = {
		"česká republika": "CZ",
		"ceska republika": "CZ",
        "čr": "CZ",
		"czech republic": "CZ",
		"slovensko": "SK",
        "svk": "SK",
		"slovenská republika": "SK",
		"slovenska republika": "SK",
		"polsko": "PL",
		"poland": "PL",
		"německo": "DE",
		"nemecko": "DE",
		"germany": "DE",
		"austria": "AT",
		"rakousko": "AT",
		"hungary": "HU",
		"maďarsko": "HU",
		"madarsko": "HU",
		"united states": "US",
		"usa": "US",
	}
	key = val.lower()
	code = None
	if len(val) == 2 and val.isalpha():
		code = val.upper()
	elif len(val) == 3 and val.isalpha():
		# Basic ISO-3 acceptance for common ones
		iso3_map = {"cze": "CZ", "svk": "SK", "deu": "DE", "aut": "AT", "hun": "HU", "usa": "US", "pol": "PL"}
		code = iso3_map.get(key)
	elif key in name_to_code:
		code = name_to_code[key]
	if code:
		return f"code:{code}"
	return None


def _normalize_ico(value: Optional[str]) -> Optional[str]:
	"""Return normalized IČO as digits-only string when possible.

	Removes all non-digits. Returns None if input is falsy or has no digits.
	"""
	if not value:
		return None
	digits = "".join(ch for ch in str(value) if ch.isdigit())
	return digits or None


def _normalize_dic(value: Optional[str]) -> Optional[str]:
	"""Normalize DIČ: uppercase, remove spaces and common labels.

	Does not validate country code; leaves value as-is if not string-like.
	"""
	if not value:
		return None
	val = str(value).strip().upper()
	# Remove common prefixes like "DIČ:", "DIC:", "VAT:" and spaces
	val = re.sub(r"^(DIČ|DIC|VAT|USt-IdNr\.)\s*:\s*", "", val, flags=re.IGNORECASE)
	val = val.replace(" ", "")
	return val or None


def _split_address_components(address: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
	"""Attempt to split a full address into (ulice, psc, mesto).

	Heuristics for typical CZ format like: "Ulice 12, 110 00 Praha" or "Ulice 12, Praha 1, 110 00".
	Returns None components if parsing fails.
	"""
	if not address:
		return None, None, None
	text = str(address).replace("\n", ",").strip()
	parts = [p.strip(" ,;\t") for p in text.split(",") if p.strip()]
	street: Optional[str] = None
	psc: Optional[str] = None
	city: Optional[str] = None

	if parts:
		street = parts[0] or None
		rest = " ".join(parts[1:]).strip()
	else:
		rest = text

	# Find PSČ anywhere in the rest
	m_psc = re.search(r"\b(\d{3})\s?(\d{2})\b", rest)
	if not m_psc and street:
		m_psc = re.search(r"\b(\d{3})\s?(\d{2})\b", street)
	if m_psc:
		psc = f"{m_psc.group(1)}{m_psc.group(2)}"
		# Remove PSČ and optional CZ- prefix to isolate city
		rest_wo_psc = re.sub(r"\b(\d{3}\s?\d{2})\b", "", rest).strip()
		rest_wo_psc = re.sub(r"^CZ[-\s]?", "", rest_wo_psc, flags=re.IGNORECASE).strip(",; ")
		city = rest_wo_psc or (parts[-1] if len(parts) > 1 else None)
	else:
		# No PSČ found; try pattern "... PSČ Město"
		city = (parts[-1] if len(parts) > 1 else None)

	# Clean potential labels
	if city:
		city = re.sub(r"^(MĚSTO|MESTO|OBEC)\s*:\s*", "", city, flags=re.IGNORECASE).strip()
	if street:
		street = re.sub(r"^(ULICE)\s*:\s*", "", street, flags=re.IGNORECASE).strip()

	return street or None, psc or None, city or None


def _normalize_company_name(value: Optional[str]) -> Optional[str]:
	"""Normalize company name for matching (lowercase, stripped, remove diacritics and legal suffixes)."""
	if not value:
		return None
	text = unicodedata.normalize("NFKD", str(value))
	text = "".join(ch for ch in text if not unicodedata.combining(ch))
	text = re.sub(r"[.,;/]", " ", text)
	text = re.sub(
		r"\b(spol\.?\s*s\.?\s*r\.?\s*o\.?|s\.?\s*r\.?\s*o\.?|sro|s r o|a\.?\s*s\.?|as|a\.?\s*s\.?)\b",
		"",
		text,
		flags=re.IGNORECASE,
	)
	text = re.sub(r"\b(ltd|llc|gmbh)\b", "", text, flags=re.IGNORECASE)
	text = re.sub(r"\s+", " ", text).strip().lower()
	return text or None


def _normalize_city(value: Optional[str]) -> Optional[str]:
	if not value:
		return None
	text = unicodedata.normalize("NFKD", str(value))
	text = "".join(ch for ch in text if not unicodedata.combining(ch))
	text = re.sub(r"\s+", " ", text).strip().lower()
	return text or None


def _normalize_psc(value: Optional[str]) -> Optional[str]:
	if not value:
		return None
	digits = "".join(ch for ch in str(value) if ch.isdigit())
	if len(digits) >= 5:
		return digits[:5]
	if len(digits) == 4:  # tolerate missing leading zero in matching only
		return digits
	return None


def _extract_partner_details(faktura: Dict[str, Any], doc_endpoint: str) -> Dict[str, Optional[str]]:
	"""Extract and normalize partner details (dodavatel or odběratel) from invoice data based on doc endpoint."""
	if doc_endpoint == "faktura-prijata":
		primary = "dodavatel"
		secondary = "odberatel"
	else:
		primary = "odberatel"
		secondary = "dodavatel"

	def get_val(key: str, fallback: bool = True) -> Optional[str]:
		val = faktura.get(f"{primary}_{key}")
		if not val and fallback:
			val = faktura.get(f"{secondary}_{key}")
		return val

	partner_name = get_val("jmeno", fallback=False)
	match_name = get_val("jmeno", fallback=True)

	partner_address = get_val("adresa", fallback=False)
	match_address = get_val("adresa", fallback=True)

	partner_psc_field = get_val("psc", fallback=False)
	partner_city_field = get_val("mesto", fallback=False)
	match_psc_field = get_val("psc", fallback=True)
	match_city_field = get_val("mesto", fallback=True)

	partner_state_raw = get_val("stat", fallback=False)
	match_state_raw = get_val("stat", fallback=True)

	partner_ico_raw = get_val("ic", fallback=False)
	match_ico_raw = get_val("ic", fallback=True)

	partner_dic_raw = get_val("dic", fallback=False)
	match_dic_raw = get_val("dic", fallback=True)

	match_street, match_psc, match_city = _split_address_components(match_address)
	partner_street, partner_psc, partner_city = _split_address_components(partner_address)
	
	partner_street = partner_street or get_val("ulice", fallback=False)
	if partner_psc_field:
		partner_psc = partner_psc_field
	if partner_city_field:
		partner_city = partner_city_field
	if match_psc_field:
		match_psc = match_psc or match_psc_field
	if match_city_field:
		match_city = match_city or match_city_field

	return {
		"name_raw": str(match_name).strip() if match_name else None,
		"name_norm": _normalize_company_name(match_name),
		"street": match_street or (get_val("ulice", fallback=True) or None),
		"psc_raw": match_psc or None,
		"psc_norm": _normalize_psc(match_psc),
		"city_raw": match_city or None,
		"city_norm": _normalize_city(match_city),
		"state": _normalize_country_reference(match_state_raw),
		"ico": _normalize_ico(match_ico_raw),
		"dic": _normalize_dic(match_dic_raw),
		"partner_name": str(partner_name).strip() if partner_name else None,
		"partner_street": partner_street or None,
		"partner_psc": _normalize_psc(partner_psc),
		"partner_city": partner_city or None,
		"partner_city_norm": _normalize_city(partner_city),
		"partner_state": _normalize_country_reference(partner_state_raw),
		"partner_ico": _normalize_ico(partner_ico_raw),
		"partner_dic": _normalize_dic(partner_dic_raw),
	}


def _infer_vat_code(faktura: Dict[str, Any]) -> str:
	"""Infer ABRA 'sazbaDphK' from totals.

	Returns one of: 'none', 'low', 'high'.
	"""
	vyse_dph = faktura.get("vyse_dph")
	zaklad = faktura.get("zaklad_dane")
	celkem = faktura.get("celkova_cena")
	if not vyse_dph or float(vyse_dph) <= 0.0001:
		return "none"
	# Prefer ratio from zaklad if available
	if zaklad and float(zaklad) > 0:
		ratio = float(vyse_dph) / float(zaklad)
	else:
		# Fallback: compute net from gross if possible
		if celkem and float(celkem) > float(vyse_dph):
			net = float(celkem) - float(vyse_dph)
			ratio = float(vyse_dph) / net if net > 0 else 0.0
		else:
			ratio = 0.0
	# Map to CZ VAT codes (approx.)
	if 0.20 <= ratio <= 0.22:
		return "high"  # 21%
	if 0.10 <= ratio <= 0.14:
		return "low"   # 12% (approx)
	return "none"


def _build_positions_from_totals(faktura: Dict[str, Any]) -> list[Dict[str, Any]]:
	"""Create item positions strictly from explicitly extracted per-rate bases.

	No computations are performed. Only the following fields are used if present:
	- zaklad_dane_0 → typSzbDph.dphOsv
	- zaklad_dane_12 → typSzbDph.dphSniz
	- zaklad_dane_21 → typSzbDph.dphZakl
	"""
	zaklad_0 = faktura.get("zaklad_dane_0")
	zaklad_12 = faktura.get("zaklad_dane_12")
	zaklad_21 = faktura.get("zaklad_dane_21")

	positions: list[Dict[str, Any]] = []
	try:
		if zaklad_0 is not None and float(zaklad_0) > 0:
			positions.append({
				"nazev": "Služby/zboží 0% DPH",
				"mnozMj": 1,
				"cenaMj": float(zaklad_0),
				"typSzbDphK": "typSzbDph.dphOsv",
			})
		if zaklad_12 is not None and float(zaklad_12) > 0:
			positions.append({
				"nazev": "Služby/zboží 12% DPH",
				"mnozMj": 1,
				"cenaMj": float(zaklad_12),
				"typSzbDphK": "typSzbDph.dphSniz",
			})
		if zaklad_21 is not None and float(zaklad_21) > 0:
			positions.append({
				"nazev": "Služby/zboží 21% DPH",
				"mnozMj": 1,
				"cenaMj": float(zaklad_21),
				"typSzbDphK": "typSzbDph.dphZakl",
			})
	except Exception:
		# On parsing error, return no positions rather than computing
		return []

	return positions


def _build_positions_from_items(invoice_data: InvoiceData) -> list[Dict[str, Any]]:
	"""Create item positions from detailed invoice items with different VAT rates.
	
	According to instructions: export by rows with correctly assigned VAT rate.
	"""
	if not invoice_data.položky:
		return []
	
	positions = []
	for item in invoice_data.položky:
		if not item.nazev:
			continue
			
		position: Dict[str, Any] = {
			"nazev": item.nazev,
			"mnozMj": item.mnozstvi or 1.0,
		}
		
		# Set price without computations:
		# Prefer explicit base amount per item; set quantity to 1 to avoid division
		if item.zaklad_dane is not None:
			position["mnozMj"] = 1
			position["cenaMj"] = float(item.zaklad_dane)
		elif item.cena_mj is not None:
			# Use provided unit price and quantity as-is
			position["cenaMj"] = float(item.cena_mj)
			position["mnozMj"] = item.mnozstvi or 1.0
		elif item.celkova_cena is not None and (item.sazba_dph or VATRate.ZERO) == VATRate.ZERO:
			# For 0% VAT lines, gross equals net; safe to pass as-is
			position["mnozMj"] = 1
			position["cenaMj"] = float(item.celkova_cena)
		else:
			# Skip items without explicit usable amounts
			continue
		
		# Set VAT rate: prefer item, otherwise infer from invoice totals
		if item.sazba_dph is not None:
			vat_code = _map_vat_rate_to_code(item.sazba_dph)
			position["typSzbDphK"] = vat_code
		else:
			# Try infer from invoice-level explicit bases first
			vat_guess: Optional[str] = None
			try:
				if (invoice_data.zaklad_dane_21 or 0) > 0 and (invoice_data.zaklad_dane_12 or 0) == 0 and (invoice_data.zaklad_dane_0 or 0) == 0:
					vat_guess = "typSzbDph.dphZakl"
				elif (invoice_data.zaklad_dane_12 or 0) > 0 and (invoice_data.zaklad_dane_21 or 0) == 0 and (invoice_data.zaklad_dane_0 or 0) == 0:
					vat_guess = "typSzbDph.dphSniz"
				elif (invoice_data.zaklad_dane_0 or 0) > 0 and (invoice_data.zaklad_dane_21 or 0) == 0 and (invoice_data.zaklad_dane_12 or 0) == 0:
					vat_guess = "typSzbDph.dphOsv"
				else:
					# Fall back to ratio-based inference if only one non-zero rate can be deduced
					faktura_dict = invoice_data.model_dump()
					inferred = _infer_vat_code(faktura_dict)
					vat_guess = {"high": "typSzbDph.dphZakl", "low": "typSzbDph.dphSniz", "none": "typSzbDph.dphOsv"}.get(inferred)
			except Exception:
				vat_guess = None
			position["typSzbDphK"] = vat_guess or "typSzbDph.dphOsv"
		
		positions.append(position)
	
	return positions


def _map_vat_rate_to_code(vat_rate: VATRate) -> str:
    """Map VAT rate enum to ABRA Flexi typSzbDphK code."""
    if vat_rate == VATRate.HIGH:
        return "typSzbDph.dphZakl"    # 21%
    elif vat_rate == VATRate.LOW:
        return "typSzbDph.dphSniz"    # 12%
    else:
        return "typSzbDph.dphOsv"     # 0%


def _request_with_retry(method: str, url: str, *, auth: tuple[str, str], timeout_s: int, json_body: Optional[Dict] = None, verify: bool = True) -> requests.Response:
	if "/adresar" in url and method.upper() in {"POST", "PUT", "PATCH"}:
		raise RuntimeError(f"ABRA guard: blokován {method} na /adresar ({url})")
	retries = [0.5, 1.0, 2.0, 4.0]
	last_exc: Optional[Exception] = None
	connect_timeout = min(5.0, float(timeout_s))
	read_timeout = float(timeout_s)
	timeout = (connect_timeout, read_timeout)
	for attempt, backoff in enumerate([0.0] + retries, start=1):
		if backoff > 0:
			time.sleep(backoff)
		start = time.perf_counter()
		try:
			resp = requests.request(method, url, auth=auth, timeout=timeout, json=json_body, verify=verify)
			elapsed = time.perf_counter() - start
		except requests.exceptions.ConnectionError as exc:
			elapsed = time.perf_counter() - start
			if "getaddrinfo failed" in str(exc) or "Failed to resolve" in str(exc):
				logger.error("ABRA DNS resolution failed for %s after %.2fs: %s", url, elapsed, exc)
				raise RuntimeError(f"ABRA server not reachable: {url}") from exc
			last_exc = exc
			logger.warning(
				"ABRA %s %s connection failed on attempt %s after %.2fs: %s",
				method,
				url,
				attempt,
				elapsed,
				exc,
			)
			continue
		except requests.exceptions.Timeout as exc:
			elapsed = time.perf_counter() - start
			last_exc = exc
			logger.warning(
				"ABRA %s %s timeout on attempt %s after %.2fs (connect=%ss read=%ss): %s",
				method,
				url,
				attempt,
				elapsed,
				connect_timeout,
				read_timeout,
				exc,
			)
			continue
		except Exception as exc:  # noqa: BLE001
			elapsed = time.perf_counter() - start
			last_exc = exc
			logger.warning(
				"ABRA %s %s failed on attempt %s after %.2fs: %s",
				method,
				url,
				attempt,
				elapsed,
				exc,
			)
			continue

		if 500 <= resp.status_code < 600:
			last_exc = RuntimeError(f"ABRA HTTP {resp.status_code}")
			logger.warning(
				"ABRA %s %s → %s in %.2fs, retrying...",
				method,
				url,
				resp.status_code,
				elapsed,
			)
			continue

		if resp.status_code in (429, 408, 425, 423):
			retry_after_hdr = resp.headers.get('Retry-After')
			last_exc = RuntimeError(f"ABRA HTTP {resp.status_code}")
			logger.warning(
				"ABRA %s %s → %s in %.2fs, retrying...",
				method,
				url,
				resp.status_code,
				elapsed,
			)
			if retry_after_hdr:
				try:
					delay = float(retry_after_hdr)
					logger.warning("ABRA %s %s → %s, respecting Retry-After: %ss", method, url, resp.status_code, delay)
					time.sleep(delay)
				except Exception:
					pass
			continue

		logger.info("ABRA %s %s → %s in %.2fs", method, url, resp.status_code, elapsed)
		return resp

	raise RuntimeError(f"ABRA request failed after retries: {method} {url}") from last_exc


def _is_duplicate_kod_error(resp: requests.Response) -> bool:
	if resp.status_code != 400:
		return False
	try:
		data = resp.json()
	except Exception:
		return False
	payload = data.get("winstrom") if isinstance(data, dict) else None
	if not isinstance(payload, dict):
		return False
	results = payload.get("results")
	if not isinstance(results, list):
		return False
	for res in results:
		errors = res.get("errors") if isinstance(res, dict) else None
		if not isinstance(errors, list):
			continue
		for err in errors:
			if not isinstance(err, dict):
				continue
			if err.get("messageCode") == "dokladNeniUnikatniKod":
				return True
	return False


def _find_invoice_id_by_kod(base_url: str, auth: tuple[str, str], timeout_s: int, company_code: str, doc_endpoint: str, kod: str, verify: bool) -> tuple[Optional[str], int, str]:
	if not kod:
		return None, 0, ""
	filter_param = requests.utils.quote(f"kod eq '{kod}'", safe="'")
	url = f"{base_url}/c/{company_code}/{doc_endpoint}.json?limit=5&detail=custom:id,kod&filter={filter_param}"
	try:
		resp = _request_with_retry("GET", url, auth=auth, timeout_s=timeout_s, verify=verify)
	except Exception as exc:
		logger.warning("ABRA: nepodařilo se načíst kandidáty pro update (kod=%s): %s", kod, exc)
		return None, 0, url
	if resp.status_code >= 400:
		logger.warning("ABRA hledání dokladu podle kódu selhalo (%s): %s", resp.status_code, resp.text)
		return None, 0, url
	try:
		raw = resp.json()
		items = _extract_winstrom_entries(raw, (doc_endpoint, "items", "data", "result"))
		if not items:
			return None, 0, url
		entry = items[0] if isinstance(items[0], dict) else None
		if isinstance(entry, dict):
			return entry.get("id") or entry.get("@id"), len(items), url
	except Exception as exc:
		logger.warning("ABRA: nelze parsovat odpověď při hledání dokladu: %s", exc)
	return None, 0, url



def _ensure_company_id(base_url: str, auth: tuple[str, str], timeout_s: int, verify: bool, company_hint: Optional[str], faktura: Dict[str, Any]) -> str:
	if company_hint:
		return company_hint

	resp = _request_with_retry("GET", f"{base_url}/c.json", auth=auth, timeout_s=timeout_s, verify=verify)
	if resp.status_code == 401:
		logger.error("ABRA authentication failed (401): check username/password")
		raise RuntimeError("ABRA: authentication failed - zkontrolujte přihlašovací údaje")
	if resp.status_code == 403:
		logger.error("ABRA access forbidden (403): insufficient permissions")
		raise RuntimeError("ABRA: přístup byl odepřen (403)")
	if resp.status_code == 404:
		logger.error("ABRA endpoint not found (404): check server URL and port")
		raise RuntimeError("ABRA: endpoint nebyl nalezen (404)")
	if resp.status_code >= 500:
		logger.error("ABRA server error (%s): %s", resp.status_code, resp.text)
		raise RuntimeError(f"ABRA: serverová chyba (HTTP {resp.status_code})")

	try:
		raw = resp.json()
		companies = _normalize_companies_payload(raw)
	except Exception as exc:
		logger.error("ABRA failed to parse JSON response: %s", exc)
		raise RuntimeError("ABRA: neplatná JSON odpověď serveru")

	if not companies:
		raise RuntimeError("ABRA: server nevrátil žádné firmy")

	ico = faktura.get("dodavatel_ic") or faktura.get("odberatel_ic")
	nazev = faktura.get("dodavatel_jmeno") or faktura.get("odberatel_jmeno")
	for c in companies:
		if not isinstance(c, dict):
			continue
		if (ico and c.get("ico") == ico) or (nazev and c.get("nazev") == nazev):
			firma_code = c.get("firma") or c.get("code") or c.get("id")
			if firma_code:
				return str(firma_code)

	context_firma = None
	if companies and isinstance(companies[0], dict):
		context_firma = companies[0].get("firma") or companies[0].get("code") or companies[0].get("id")
	if not context_firma:
		raise RuntimeError("ABRA: nepodařilo se určit kód firmy. Nastavte firmu v Nastavení.")

	return str(context_firma)



def _ensure_partner_ext_id(base_url: str, auth: tuple[str, str], timeout_s: int, company_code: str, faktura: Dict[str, Any], verify: bool, partner_rel_code: Optional[str], doc_endpoint: str) -> Optional[str]:
	"""Najdi existující záznam v Adresáři dle IČO/DIČ/názvu a vrať jeho referenci.

	Nikdy nevytváří nové adresy – pokud se nenajde shoda nebo adresář nelze načíst,
	vrací None a import pokračuje bez přiřazené firmy.
	"""
	# typVztahuK (partner_rel_code) se zde nepoužívá, protože adresář pouze čteme
	_ = partner_rel_code

	def _extract_partner_reference(entry: Dict[str, Any]) -> Optional[str]:
		"""Pick usable reference (kod/zkratka/id) and prefix as code:/id: when needed."""
		for key in ("kod", "zkratka", "code"):
			raw = entry.get(key)
			if raw is None:
				continue
			ref = str(raw).strip()
			if ref:
				return ref if ref.startswith(("code:", "id:", "ext:")) else f"code:{ref}"
		raw_id = entry.get("id") or entry.get("@id")
		if raw_id is not None:
			ref = str(raw_id).strip()
			if ref:
				return ref if ref.startswith(("code:", "id:", "ext:")) else f"id:{ref}"
		return None

	partner = _extract_partner_details(faktura, doc_endpoint)
	nazev_raw = partner["name_raw"]
	ico = partner["ico"]
	dic = partner["dic"]
	nazev_norm = partner["name_norm"]
	psc_norm = partner["psc_norm"]
	city_norm = partner["city_norm"]

	if not any((ico, dic, nazev_norm)):
		logger.info("ABRA: faktura postrádá IČO/DIČ/název pro přiřazení firmy, import pokračuje bez vazby.")
		return None

	# Načti celý adresář s omezeným detailem a hledej lokálně shody
	detail_fields = "id,kod,zkratka,nazev,ic,ico,dic,psc,obec,mesto,ulice,stat,statK"
	url = f"{base_url}/c/{company_code}/adresar.json?limit=0&detail=custom:{detail_fields}"
	try:
		resp = _request_with_retry("GET", url, auth=auth, timeout_s=timeout_s, verify=verify)
	except Exception as exc:
		logger.warning("ABRA: nepodařilo se načíst adresář, import pokračuje bez přiřazení firmy: %s", exc)
		return None
	if resp.status_code == 401:
		logger.error("ABRA authentication failed when reading adresar (401)")
		return None
	if resp.status_code == 403:
		logger.error("ABRA access forbidden when reading adresar (403)")
		return None
	if resp.status_code == 404:
		logger.error("ABRA adresar endpoint not found (404): %s", resp.text)
		return None
	if resp.status_code >= 500:
		logger.error("ABRA adresar server error (%s): %s", resp.status_code, resp.text)
		return None
	if resp.status_code >= 400:
		logger.error("ABRA adresar request failed (%s): %s", resp.status_code, resp.text)
		return None

	try:
		raw = resp.json()
		adresar_entries = _extract_winstrom_entries(raw, ("adresar", "adresy", "items", "data", "result"))
	except Exception as exc:
		logger.error("ABRA failed to parse adresar JSON response: %s", exc)
		logger.info("ABRA: import pokračuje bez přiřazení firmy (adresář nelze přečíst).")
		return None

	if not adresar_entries:
		logger.info("ABRA: adresář je prázdný nebo se nepodařilo načíst záznamy, import pokračuje bez vazby.")
		return None

	candidates = []
	for entry in adresar_entries:
		ref = _extract_partner_reference(entry)
		if not ref:
			continue
		entry_ico = _normalize_ico(entry.get("ic") or entry.get("ico"))
		entry_dic = _normalize_dic(entry.get("dic"))
		entry_name = _normalize_company_name(entry.get("nazev") or entry.get("firma") or entry.get("obchNazev") or entry.get("jmeno"))
		entry_psc = _normalize_psc(entry.get("psc"))
		entry_city = _normalize_city(entry.get("obec") or entry.get("mesto") or entry.get("city"))
		candidates.append({"ref": ref, "ico": entry_ico, "dic": entry_dic, "name": entry_name, "psc": entry_psc, "city": entry_city})

	def _score_candidate(cand: Dict[str, Optional[str]]) -> int:
		score = 0
		if ico and cand.get("ico") == ico:
			score += 5
		if dic and cand.get("dic") == dic:
			score += 3
		if nazev_norm and cand.get("name") == nazev_norm:
			score += 2
		if psc_norm and cand.get("psc") == psc_norm:
			score += 2
		if city_norm and cand.get("city") == city_norm:
			score += 1
		return score

	def _choose_best(cands: list[Dict[str, Optional[str]]], reason: str) -> Optional[str]:
		if not cands:
			return None
		if len(cands) == 1:
			return cands[0]["ref"]
		scored = sorted(((_score_candidate(c), c) for c in cands), key=lambda t: t[0], reverse=True)
		best_score, best = scored[0]
		if len(scored) > 1 and best_score == scored[1][0]:
			logger.warning("ABRA: více shod v adresáři pro %s, vybírám první: %s", reason, best["ref"])
		else:
			logger.info("ABRA: více shod v adresáři pro %s, vybrána nejlepší shoda: %s", reason, best["ref"])
		return best["ref"]

	# Priorita: shoda IČO/DIČ, pak IČO, DIČ, název s PSČ/městem
	if ico and dic:
		ic_dic_matches = [c for c in candidates if c["ico"] == ico and c["dic"] == dic]
		if ic_dic_matches:
			ref = _choose_best(ic_dic_matches, "IČO+DIČ")
			if ref:
				logger.info("ABRA: nalezena firma v adresáři dle IČO a DIČ: %s", ref)
				return ref
	if ico:
		ico_matches = [c for c in candidates if c["ico"] == ico]
		if ico_matches:
			ref = _choose_best(ico_matches, "IČO")
			if ref:
				logger.info("ABRA: nalezena firma v adresáři dle IČO: %s", ref)
				return ref
	if dic:
		dic_matches = [c for c in candidates if c["dic"] == dic]
		if dic_matches:
			ref = _choose_best(dic_matches, "DIČ")
			if ref:
				logger.info("ABRA: nalezena firma v adresáři dle DIČ: %s", ref)
				return ref
	if nazev_norm:
		name_matches = [c for c in candidates if c["name"] == nazev_norm]
		if psc_norm:
			psc_matches = [c for c in name_matches if c["psc"] == psc_norm]
			if psc_matches:
				name_matches = psc_matches
		if city_norm and name_matches:
			city_matches = [c for c in name_matches if c["city"] == city_norm]
			if city_matches:
				name_matches = city_matches
		if name_matches:
			ref = _choose_best(name_matches, "název/PSČ")
			if ref:
				logger.info("ABRA: nalezena firma v adresáři dle názvu%s: %s", " a PSČ" if psc_norm else "", ref)
				return ref

	logger.info(
		"ABRA: žádná shoda v adresáři (ico=%s, dic=%s, nazev=%s, psc=%s, mesto=%s). Import pokračuje bez vazby na firmu.",
		ico,
		dic,
		nazev_raw,
		psc_norm,
		city_norm,
	)
	return None


def _prepare_partner_section(faktura: Dict[str, Any], partner_ref: Optional[str], doc_endpoint: str) -> Dict[str, Any]:
	"""Return ABRA payload fields for partner snapshot and optional firma reference."""
	partner = _extract_partner_details(faktura, doc_endpoint)
	body: Dict[str, Any] = {}

	name_for_payload = partner.get("partner_name") or partner.get("name_raw")
	street = partner.get("partner_street") or partner.get("street")
	psc_value = partner.get("partner_psc") or partner.get("psc_norm")
	city_value = partner.get("partner_city") or partner.get("city_raw")
	state_value = partner.get("partner_state") or partner.get("state")
	ico_value = partner.get("partner_ico") or partner.get("ico")
	dic_value = partner.get("partner_dic") or partner.get("dic")

	def _set_if_present(key: str, value: Optional[str]) -> None:
		if value:
			body[key] = value

	if partner_ref:
		body["firma"] = partner_ref
		logger.info("ABRA: faktura bude navázána na existující adresář: %s", partner_ref)
	else:
		logger.info("ABRA: faktura bude importována bez vazby na adresář (firma).")

	_set_if_present("nazFirmy", name_for_payload)
	_set_if_present("ulice", street)
	_set_if_present("psc", psc_value)
	_set_if_present("mesto", city_value)
	_set_if_present("stat", state_value)
	_set_if_present("ic", ico_value)
	_set_if_present("dic", dic_value)
	return body


def _validate_winstrom_payload(payload: Dict[str, Any], *, partner_ref: Optional[str]) -> None:
	"""Fail fast when payload violates safety invariants."""
	def _walk(obj: Any, path: str = "") -> None:
		if isinstance(obj, dict):
			for k, v in obj.items():
				key_path = f"{path}.{k}" if path else k
				if k.lower().endswith("if-not-found") and str(v).lower() == "create":
					raise ValueError(f"Zakázaná kombinace if-not-found (create) v payloadu ({key_path})")
				_walk(v, key_path)
		elif isinstance(obj, list):
			for idx, item in enumerate(obj):
				_walk(item, f"{path}[{idx}]")

	_walk(payload)

	winstrom = payload.get("winstrom") if isinstance(payload, dict) else None
	if not isinstance(winstrom, dict):
		return
	doc_entries = None
	for val in winstrom.values():
		if isinstance(val, list):
			doc_entries = val
			break
	if not doc_entries:
		return

	for entry in doc_entries:
		if not isinstance(entry, dict):
			continue
		has_firma = "firma" in entry
		if partner_ref:
			if not has_firma:
				raise ValueError("Očekáváme vazbu na existující firmu, ale payload neobsahuje klíč 'firma'.")
		else:
			if has_firma:
				raise ValueError("Bez shody v adresáři nesmí být v payloadu klíč 'firma'.")
			for key in ("nazFirmy", "ulice", "mesto", "psc", "stat", "ic", "dic"):
				if key in entry and (entry[key] is None or entry[key] == ""):
					raise ValueError(f"Payload obsahuje prázdnou hodnotu pro {key}.")


def _extract_ext_identifier(payload: Dict[str, Any], doc_endpoint: str) -> Optional[str]:
	try:
		winstrom = payload.get("winstrom", {})
		entries = winstrom.get(doc_endpoint)
		if not isinstance(entries, list) or not entries:
			return None
		first = entries[0] if isinstance(entries[0], dict) else None
		if not isinstance(first, dict):
			return None
		# Prefer stable external id if present; fall back to request identifiers
		return first.get("id") or first.get("cisDosle") or first.get("kod") or first.get("varSym")
	except Exception:
		return None


def _extract_entry_value(payload: Dict[str, Any], doc_endpoint: str, key: str) -> Optional[Any]:
	try:
		winstrom = payload.get("winstrom", {})
		entries = winstrom.get(doc_endpoint)
		if not isinstance(entries, list) or not entries:
			return None
		first = entries[0] if isinstance(entries[0], dict) else None
		if not isinstance(first, dict):
			return None
		return first.get(key)
	except Exception:
		return None


def _extract_ext_id_from_invoice_detail(detail: Dict[str, Any]) -> Optional[str]:
	"""Try to read ext-id from invoice detail payload."""
	if not isinstance(detail, dict):
		return None
	# Common keys
	for key in ("id", "@id", "externalId", "externalID"):
		val = detail.get(key)
		if isinstance(val, str) and val.startswith("ext:"):
			return val
	# externalIds array/object
	for key in ("externalIds", "external-ids", "ids"):
		val = detail.get(key)
		if isinstance(val, list):
			for item in val:
				if isinstance(item, str) and item.startswith("ext:"):
					return item
				if isinstance(item, dict):
					ref = item.get("id") or item.get("@id") or item.get("value")
					if isinstance(ref, str) and ref.startswith("ext:"):
						return ref
		if isinstance(val, dict):
			for ref in val.values():
				if isinstance(ref, str) and ref.startswith("ext:"):
					return ref
	# Try to scan string fields for ext:
	for key, val in detail.items():
		if isinstance(val, str) and "ext:" in val:
			m = re.search(r"(ext:[A-Za-z0-9_.:-]+)", val)
			if m:
				return m.group(1)
	return None


def _get_invoice_detail(base_url: str, auth: tuple[str, str], timeout_s: int, company_code: str, doc_endpoint: str, invoice_id: str, verify: bool) -> Optional[Dict[str, Any]]:
	url = f"{base_url}/c/{company_code}/{doc_endpoint}/{invoice_id}.json?detail=full"
	try:
		resp = _request_with_retry("GET", url, auth=auth, timeout_s=timeout_s, verify=verify)
	except Exception as exc:
		logger.warning("ABRA: načtení detailu dokladu selhalo: %s", exc)
		return None
	if resp.status_code >= 400:
		logger.warning("ABRA detail dokladu HTTP %s: %s", resp.status_code, resp.text)
		return None
	try:
		raw = resp.json()
		entries = _extract_winstrom_entries(raw, (doc_endpoint,))
		if entries and isinstance(entries[0], dict):
			return entries[0]
	except Exception as exc:
		logger.warning("ABRA: parsování detailu dokladu selhalo: %s", exc)
	return None


def _get_invoice_by_ext(base_url: str, auth: tuple[str, str], timeout_s: int, company_code: str, doc_endpoint: str, ext_id: str, verify: bool) -> Optional[Dict[str, Any]]:
	if not ext_id:
		return None
	ext_encoded = requests.utils.quote(ext_id, safe="")
	url = f"{base_url}/c/{company_code}/{doc_endpoint}/{ext_encoded}.json?detail=full"
	try:
		resp = _request_with_retry("GET", url, auth=auth, timeout_s=timeout_s, verify=verify)
	except Exception as exc:
		logger.warning("ABRA: dotaz na doklad podle ext-id selhal: %s", exc)
		return None
	if resp.status_code >= 400:
		return None
	try:
		raw = resp.json()
		entries = _extract_winstrom_entries(raw, (doc_endpoint,))
		if entries and isinstance(entries[0], dict):
			return entries[0]
	except Exception:
		return None
	return None


def _compute_ext_id(faktura: Dict[str, Any], doc_endpoint: str) -> str:
	"""Return stable ext identifier for invoice to make import idempotent."""
	candidates = [
		faktura.get("external_id"),
		faktura.get("cislo_dokladu"),
		faktura.get("variabilni_symbol"),
		faktura.get("cisDokl"),
		faktura.get("cisDosle"),
		faktura.get("kod"),
	]

	def _sanitize(value: str) -> str:
		text = unicodedata.normalize("NFKD", str(value))
		text = "".join(ch for ch in text if not unicodedata.combining(ch))
		text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text)
		text = re.sub(r"-{2,}", "-", text).strip("-")
		return text or "unknown"

	for cand in candidates:
		if cand:
			return f"ext:easyflex:{doc_endpoint}:{_sanitize(cand)}"

	# Fallback: hash entire invoice content deterministically
	raw = json.dumps(faktura, sort_keys=True, ensure_ascii=False)
	digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
	return f"ext:easyflex:{doc_endpoint}:h{digest}"


def _build_invoice_payload(
	faktura_data: Union[Dict[str, Any], InvoiceData],
	cfg: AppConfig,
	partner_ref: Optional[str],
	doc_endpoint: str,
) -> Dict[str, Any]:
	"""Assemble winstrom payload for faktura with guardrails."""
	# Convert InvoiceData to dict for compatibility with existing functions
	if isinstance(faktura_data, InvoiceData):
		faktura_dict = faktura_data.model_dump()
	else:
		faktura_dict = faktura_data

	body = _map_invoice_json(faktura_data, cfg.abra_series_map, cfg)
	# Set typDokl as coded value string expected by FlexiBee
	if cfg.abra_doc_type_code:
		body["typDokl"] = f"code:{cfg.abra_doc_type_code}"
	else:
		# Defaults: issued invoice → FAKTURA, received invoice → FAKTP
		body["typDokl"] = "code:FAKTURA" if doc_endpoint == "faktura-vydana" else "code:FAKTP"
	# Stabilní externí ID pro idempotentní import
	ext_id = _compute_ext_id(faktura_dict, doc_endpoint)
	body["id"] = ext_id

	# Interní kód dokladu – volitelný podle konfigurace
	internal_code = None
	if doc_endpoint == "faktura-vydana":
		if isinstance(faktura_data, InvoiceData):
			internal_code = body.pop("cisDosle", None) or faktura_data.cislo_dokladu or faktura_data.variabilni_symbol
		else:
			internal_code = body.pop("cisDosle", None) or faktura_data.get("cislo_dokladu") or faktura_data.get("variabilni_symbol")
		if cfg.abra_use_kod and internal_code:
			body["kod"] = str(internal_code)
		elif cfg.abra_use_kod and not internal_code:
			logger.warning("ABRA: abra_use_kod=true, ale kod chybí – nebude odeslán.")
			body.pop("kod", None)
		else:
			body.pop("kod", None)
	# Rozhodování mezi původní a novou logikou podle instrukcí
	if isinstance(faktura_data, InvoiceData) and should_use_items_logic(faktura_data):
		logger.info("Používám novou logiku s položkami pro ABRA export")
		body["polozkyDokladu"] = _build_positions_from_items(faktura_data)
	else:
		logger.info("Používám původní logiku pro ABRA export")
		body["polozkyDokladu"] = _build_positions_from_totals(faktura_dict)
	# Kurz neposíláme; měna je mapována přes relation "mena".
	body.pop("kurz", None)
	body.pop("kurzMnozstvi", None)

	body.update(_prepare_partner_section(faktura_dict, partner_ref, doc_endpoint))
	payload = _wrap_winstrom(doc_endpoint, body)
	_validate_winstrom_payload(payload, partner_ref=partner_ref)
	return payload


def _map_invoice_json(faktura: Union[Dict[str, Any], InvoiceData], series_map: Optional[str], cfg: AppConfig) -> Dict[str, Any]:
	# We set typDokl later from config to avoid wrong defaults here
	if isinstance(faktura, InvoiceData):
		dat_vyst = faktura.datum_vystaveni
		dat_splat = faktura.datum_splatnosti
		cislo_dokladu = faktura.cislo_dokladu
		variabilni_symbol = faktura.variabilni_symbol
		datum_duzp = faktura.datum_duzp
		mena_raw = faktura.mena
		warning_value = get_warning(faktura)
	else:
		dat_vyst = faktura.get("datum_vystaveni")
		dat_splat = faktura.get("datum_splatnosti")
		cislo_dokladu = faktura.get("cislo_dokladu")
		variabilni_symbol = faktura.get("variabilni_symbol")
		datum_duzp = faktura.get("datum_duzp")
		mena_raw = faktura.get("mena")
		warning_value = get_warning(faktura)
	
	# Apply extraction options from config, včetně domyšlení datumů, pokud je zapnuto
	inferred_warnings: list[str] = []
	if getattr(cfg, "infer_missing_dates", False):
		date_payload = {
			"datum_vystaveni": dat_vyst,
			"datum_splatnosti": dat_splat,
			"datum_duzp": datum_duzp,
		}
		day_first = getattr(cfg, "date_day_first", True)
		date_payload, inferred_warnings = domysleni_chybejicich_datumu(date_payload, day_first=day_first)
		dat_vyst = date_payload.get("datum_vystaveni") or dat_vyst
		dat_splat = date_payload.get("datum_splatnosti") or dat_splat
		datum_duzp = date_payload.get("datum_duzp") or datum_duzp
	
	body = {
		"cisDosle": cislo_dokladu,
		"varSym": variabilni_symbol,
		"datVyst": dat_vyst,
		"duzpPuv": datum_duzp,
		"datSplat": dat_splat,
		"mena": currency_reference(mena_raw, fallback="CZK"),
	}
	if mena_raw not in (None, "", "null") and not normalize_currency(mena_raw):
		logger.warning("ABRA: neplatná měna '%s' normalizována na CZK.", mena_raw)
	warning_items: list[str] = []

	def _consume_warning_value(value: Any) -> None:
		if not value:
			return
		if isinstance(value, (list, tuple, set)):
			for entry in value:
				_consume_warning_value(entry)
			return
		text = str(value).strip()
		if text and text.lower() != "bez problému":
			warning_items.append(text)

	_consume_warning_value(warning_value)
	if inferred_warnings:
		warning_items.extend(inferred_warnings)
	if warning_items:
		seen: set[str] = set()
		ordered: list[str] = []
		for msg in warning_items:
			if msg not in seen:
				ordered.append(msg)
				seen.add(msg)
		warning_text = " | ".join(ordered)
		if warning_text:
			logger.debug("Upozorneni pouze pro UI, do ABRA se neodesílá: %s", warning_text)
	if series_map:
		try:
			if isinstance(series_map, str):
				mapping = json.loads(series_map)
			else:
				mapping = series_map
			if isinstance(mapping, dict):
				doc_endpoint = getattr(cfg, "abra_doc_endpoint", "") or "faktura-prijata"
				series_value = mapping.get(doc_endpoint) or mapping.get("default")
				if series_value:
					if not (str(series_value).startswith("code:") or str(series_value).startswith("id:") or str(series_value).startswith("ext:")):
						series_value = f"code:{series_value}"
					body["rada"] = str(series_value)
		except Exception as exc:
			logger.warning("Neplatné nastavení series_map: %s", exc)
	return body


def import_to_abra(faktura_data: Union[Dict[str, Any], InvoiceData], cfg: Optional[AppConfig] = None) -> Optional[Dict[str, Any]]:
	"""Import one extracted invoice to ABRA Flexi.

	Returns parsed JSON of created/updated document on success, otherwise None.
	"""
	if cfg is None:
		cfg = load_config()
	if not (cfg.abra_server and cfg.abra_username and cfg.abra_password):
		logger.info("ABRA not configured; skipping import.")
		return None

	base_url = _build_base_url(cfg.abra_server, cfg.abra_port)
	auth = (cfg.abra_username, cfg.abra_password)
	logger.info("ABRA import attempt: server=%s, company_hint=%s", base_url, cfg.abra_company)

	# Probe connectivity to faktura endpoint (instructions step 1)
	probe_company = cfg.abra_company or "demo"
	doc_endpoint = cfg.abra_doc_endpoint or "faktura-prijata"
	probe_url = f"{base_url}/c/{probe_company}/{doc_endpoint}.json"
	probe_resp = _request_with_retry("GET", probe_url, auth=auth, timeout_s=cfg.abra_timeout_s, verify=cfg.abra_verify_tls)
	if probe_resp.status_code >= 400:
		logger.warning("ABRA probe failed (%s): %s", probe_resp.status_code, probe_resp.text)

	# Resolve company code used in path
	company_code = _ensure_company_id(base_url, auth, cfg.abra_timeout_s, cfg.abra_verify_tls, cfg.abra_company, faktura_data if isinstance(faktura_data, dict) else faktura_data.model_dump())
	# Best-effort lookup of partner in ABRA adresář (bez vytváření nových)
	partner_ref = _ensure_partner_ext_id(base_url, auth, cfg.abra_timeout_s, company_code, faktura_data if isinstance(faktura_data, dict) else faktura_data.model_dump(), cfg.abra_verify_tls, cfg.abra_partner_rel_code, doc_endpoint)

	payload = _build_invoice_payload(faktura_data, cfg, partner_ref, doc_endpoint)
	try:
		logger.debug("ABRA payload faktura: %s", json.dumps(payload, ensure_ascii=False))
	except Exception:
		logger.debug("ABRA payload faktura (repr): %s", payload)
	ext_identifier = _extract_ext_identifier(payload, doc_endpoint)
	kod_value = _extract_entry_value(payload, doc_endpoint, "kod")
	typ_dokl = _extract_entry_value(payload, doc_endpoint, "typDokl")
	logger.info("ABRA send doc=%s typ=%s ext_id=%s kod=%s", doc_endpoint, typ_dokl, ext_identifier, kod_value)

	# Try POST first
	url = f"{base_url}/c/{company_code}/{doc_endpoint}.json"
	resp = _request_with_retry("POST", url, auth=auth, timeout_s=cfg.abra_timeout_s, json_body=payload, verify=cfg.abra_verify_tls)
	if resp.status_code in (200, 201):
		data = resp.json()
		logger.info("ABRA import OK ext=%s id=%s", ext_identifier, data.get("id"))
		data["__status"] = "created"
		return data
	elif resp.status_code == 409 or _is_duplicate_kod_error(resp):
		logger.warning("ABRA hlásí duplicitní kód dokladu (kod=%s, ext=%s) resp=%s", kod_value, ext_identifier, resp.text)
		if cfg.abra_duplicate_kod_strategy == "skip":
			logger.info("Strategie duplicate_kod=skip – doklad přeskočen.")
			return {"__status": "skipped-duplicate", "kod": kod_value, "ext": ext_identifier}
		# safe_update
		if not kod_value:
			logger.error("Safe update podle kódu nelze – kod chybí, doklad přeskočen.")
			return {"__status": "skipped-duplicate", "kod": kod_value, "ext": ext_identifier}
		target_id, count, search_url = _find_invoice_id_by_kod(base_url, auth, cfg.abra_timeout_s, company_code, doc_endpoint, kod_value, cfg.abra_verify_tls)
		logger.info("Duplicate search kod=%s -> candidates=%s url=%s", kod_value, count, search_url)
		if not target_id:
			logger.error("ABRA tvrdí duplicitu, ale hledání kod=%s nenašlo doklad. Import ukončen pro tento řádek.", kod_value)
			return {"__status": "failed-duplicate-not-found", "kod": kod_value, "ext": ext_identifier, "search_url": search_url}
		detail = _get_invoice_detail(base_url, auth, cfg.abra_timeout_s, company_code, doc_endpoint, str(target_id), cfg.abra_verify_tls)
		detail_ext = _extract_ext_id_from_invoice_detail(detail or {})
		logger.info("Duplicate detail id=%s ext=%s", target_id, detail_ext)
		if detail_ext and detail_ext == ext_identifier:
			update_url = f"{base_url}/c/{company_code}/{doc_endpoint}/{target_id}.json"
			resp2 = _request_with_retry("PUT", update_url, auth=auth, timeout_s=cfg.abra_timeout_s, json_body=payload, verify=cfg.abra_verify_tls)
			if resp2.status_code in (200, 201):
				data = resp2.json()
				logger.info("ABRA update OK ext=%s id=%s", ext_identifier, data.get("id"))
				data["__status"] = "updated"
				return data
			logger.error("ABRA update po duplicitě selhala (%s): %s", resp2.status_code, resp2.text)
			return {"__status": "failed-duplicate-update", "kod": kod_value, "ext": ext_identifier, "id": target_id, "resp_status": resp2.status_code, "resp_text": resp2.text}
		if detail_ext and detail_ext != ext_identifier:
			logger.warning("Kod=%s patří jiné faktuře (ext=%s), doklad přeskočen.", kod_value, detail_ext)
			return {"__status": "skipped-duplicate-foreign", "kod": kod_value, "ext": ext_identifier, "found_ext": detail_ext}

		# detail_ext missing -> fallback check by ext-id directly
		ext_detail = _get_invoice_by_ext(base_url, auth, cfg.abra_timeout_s, company_code, doc_endpoint, ext_identifier or "", cfg.abra_verify_tls)
		ext_detail_id = ext_detail.get("id") if isinstance(ext_detail, dict) else None
		if ext_detail_id:
			update_url = f"{base_url}/c/{company_code}/{doc_endpoint}/{ext_detail_id}.json"
			resp2 = _request_with_retry("PUT", update_url, auth=auth, timeout_s=cfg.abra_timeout_s, json_body=payload, verify=cfg.abra_verify_tls)
			if resp2.status_code in (200, 201):
				data = resp2.json()
				logger.info("ABRA update OK dle ext-id ext=%s id=%s", ext_identifier, data.get("id"))
				data["__status"] = "updated"
				return data
			logger.error("ABRA update dle ext-id selhala (%s): %s", resp2.status_code, resp2.text)
			return {"__status": "failed-duplicate-update", "kod": kod_value, "ext": ext_identifier, "id": ext_detail_id, "resp_status": resp2.status_code, "resp_text": resp2.text}

		logger.warning("Kod duplicita nelze ověřit ext-id (žádné ext v detailu), doklad neimportován.")
		return {"__status": "failed-duplicate-unverifiable", "kod": kod_value, "ext": ext_identifier}
	else:
		logger.error("ABRA POST failed: %s %s", resp.status_code, resp.text)

	# Failure path: dump error payload
	# Write errors to a user-writable location
	_err_dir = get_errors_dir()
	_err_dir.mkdir(parents=True, exist_ok=True)
	error_id = ext_identifier or 'unknown'
	with open(os.path.join(_err_dir, f"abra_error_{error_id}.json"), "w", encoding="utf-8") as f:
		json.dump({"request": payload, "response_status": resp.status_code, "response_text": resp.text}, f, ensure_ascii=False, indent=2)
	return None
