import json
import logging
import os
import time
from typing import Any, Dict, Optional, Union
import re

import requests

from .config import load_config, get_errors_dir, AppConfig
from .models import InvoiceData, InvoiceItem, VATRate
from .invoice_warnings import get_warning
from .invoice_processor import should_use_items_logic
from .date_helpers import domysleni_chybejicich_datumu


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
	- zaklad_dane_0 → typSzbDph.dphNul
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
				"typSzbDphK": "typSzbDph.dphNul",
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
					vat_guess = "typSzbDph.dphNul"
				else:
					# Fall back to ratio-based inference if only one non-zero rate can be deduced
					faktura_dict = invoice_data.model_dump()
					inferred = _infer_vat_code(faktura_dict)
					vat_guess = {"high": "typSzbDph.dphZakl", "low": "typSzbDph.dphSniz", "none": "typSzbDph.dphNul"}.get(inferred)
			except Exception:
				vat_guess = None
			position["typSzbDphK"] = vat_guess or "typSzbDph.dphNul"
		
		positions.append(position)
	
	return positions


def _map_vat_rate_to_code(vat_rate: VATRate) -> str:
    """Map VAT rate enum to ABRA Flexi typSzbDphK code."""
    if vat_rate == VATRate.HIGH:
        return "typSzbDph.dphZakl"    # 21%
    elif vat_rate == VATRate.LOW:
        return "typSzbDph.dphSniz"    # 12%
    else:
        return "typSzbDph.dphNul"     # 0%


def _request_with_retry(method: str, url: str, *, auth: tuple[str, str], timeout_s: int, json_body: Optional[Dict] = None, verify: bool = True) -> requests.Response:
	retries = [0.5, 1.0, 2.0, 4.0]
	last_exc: Optional[Exception] = None
	for attempt, backoff in enumerate([0.0] + retries, start=1):
		if backoff > 0:
			time.sleep(backoff)
		try:
			resp = requests.request(method, url, auth=auth, timeout=timeout_s, json=json_body, verify=verify)
		except requests.exceptions.ConnectionError as exc:
			if "getaddrinfo failed" in str(exc) or "Failed to resolve" in str(exc):
				logger.error("ABRA DNS resolution failed for %s: %s", url, exc)
				raise RuntimeError(f"ABRA server not reachable: {url}") from exc
			last_exc = exc
			logger.warning("ABRA %s %s connection failed on attempt %s: %s", method, url, attempt, exc)
			continue
		except requests.exceptions.Timeout as exc:
			last_exc = exc
			logger.warning("ABRA %s %s timeout on attempt %s: %s", method, url, attempt, exc)
			continue
		except Exception as exc:  # noqa: BLE001
			last_exc = exc
			logger.warning("ABRA %s %s failed on attempt %s: %s", method, url, attempt, exc)
			continue

		if 500 <= resp.status_code < 600:
			last_exc = RuntimeError(f"ABRA HTTP {resp.status_code}")
			logger.warning("ABRA %s %s → %s, retrying...", method, url, resp.status_code)
			continue

		if resp.status_code in (429, 408, 425, 423):
			retry_after_hdr = resp.headers.get('Retry-After')
			last_exc = RuntimeError(f"ABRA HTTP {resp.status_code}")
			if retry_after_hdr:
				try:
					delay = float(retry_after_hdr)
					logger.warning("ABRA %s %s → %s, respecting Retry-After: %ss", method, url, resp.status_code, delay)
					time.sleep(delay)
				except Exception:
					pass
			continue

		return resp

	raise RuntimeError(f"ABRA request failed after retries: {method} {url}") from last_exc



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



def _ensure_partner_ext_id(base_url: str, auth: tuple[str, str], timeout_s: int, company_code: str, faktura: Dict[str, Any], verify: bool, partner_rel_code: Optional[str]) -> Optional[str]:
	"""Najdi existující záznam v Adresáři dle IČO/DIČ/názvu a vrať jeho referenci.

	Nikdy nevytváří nové adresy – pokud se nenajde shoda nebo adresář nelze načíst,
	vrací None a import pokračuje bez přiřazené firmy.
	"""
	# typVztahuK (partner_rel_code) se zde nepoužívá, protože adresář pouze čteme
	_ = partner_rel_code
	def _normalize_name(value: Optional[str]) -> Optional[str]:
		if not value:
			return None
		return re.sub(r"\s+", " ", str(value)).strip().lower() or None

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

	# Prefer odběratel; pokud chybí, zkusíme dodavatele z faktury
	nazev_raw = faktura.get("odberatel_jmeno") or faktura.get("dodavatel_jmeno")
	ico = _normalize_ico(faktura.get("odberatel_ic") or faktura.get("dodavatel_ic"))
	dic = _normalize_dic(faktura.get("odberatel_dic") or faktura.get("dodavatel_dic"))
	nazev_norm = _normalize_name(nazev_raw)

	if not any((ico, dic, nazev_norm)):
		logger.info("ABRA: faktura postrádá IČO/DIČ/název pro přiřazení firmy, import pokračuje bez vazby.")
		return None

	# Načti celý adresář s omezeným detailem a hledej lokálně shody
	detail_fields = "id,kod,zkratka,nazev,ic,ico,dic"
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
		entry_name = _normalize_name(entry.get("nazev") or entry.get("firma") or entry.get("obchNazev") or entry.get("jmeno"))
		candidates.append({"ref": ref, "ico": entry_ico, "dic": entry_dic, "name": entry_name})

	def _pick_by(predicate) -> Optional[str]:
		for cand in candidates:
			if predicate(cand):
				return cand["ref"]
		return None

	# Priorita: shoda ICO+DIČ → IČO → DIČ → název
	if ico and dic:
		ref = _pick_by(lambda c: c["ico"] == ico and c["dic"] == dic)
		if ref:
			logger.info("ABRA: nalezena firma v adresáři dle IČO a DIČ: %s", ref)
			return ref
	if ico:
		ref = _pick_by(lambda c: c["ico"] == ico)
		if ref:
			logger.info("ABRA: nalezena firma v adresáři dle IČO: %s", ref)
			return ref
	if dic:
		ref = _pick_by(lambda c: c["dic"] == dic)
		if ref:
			logger.info("ABRA: nalezena firma v adresáři dle DIČ: %s", ref)
			return ref
	if nazev_norm:
		ref = _pick_by(lambda c: c["name"] == nazev_norm)
		if ref:
			logger.info("ABRA: nalezena firma v adresáři dle názvu: %s", ref)
			return ref

	logger.info("ABRA: žádná shoda v adresáři (ico=%s, dic=%s, nazev=%s). Import pokračuje bez vazby na firmu.", ico, dic, nazev_raw)
	return None


def _map_invoice_json(faktura: Union[Dict[str, Any], InvoiceData], series_map: Optional[str], cfg: AppConfig) -> Dict[str, Any]:
	# We set typDokl later from config to avoid wrong defaults here
	if isinstance(faktura, InvoiceData):
		dat_vyst = faktura.datum_vystaveni
		dat_splat = faktura.datum_splatnosti
		cislo_dokladu = faktura.cislo_dokladu
		variabilni_symbol = faktura.variabilni_symbol
		datum_duzp = faktura.datum_duzp
		warning_value = get_warning(faktura)
	else:
		dat_vyst = faktura.get("datum_vystaveni")
		dat_splat = faktura.get("datum_splatnosti")
		cislo_dokladu = faktura.get("cislo_dokladu")
		variabilni_symbol = faktura.get("variabilni_symbol")
		datum_duzp = faktura.get("datum_duzp")
		warning_value = get_warning(faktura)
	
	# Apply extraction options from config, včetně domyšlení datumů, pokud je zapnuto
	inferred_warnings: list[str] = []
	if getattr(cfg, "infer_missing_dates", False):
		date_payload = {
			"datum_vystaveni": dat_vyst,
			"datum_splatnosti": dat_splat,
			"datum_duzp": datum_duzp,
		}
		date_payload, inferred_warnings = domysleni_chybejicich_datumu(date_payload)
		dat_vyst = date_payload.get("datum_vystaveni") or dat_vyst
		dat_splat = date_payload.get("datum_splatnosti") or dat_splat
		datum_duzp = date_payload.get("datum_duzp") or datum_duzp
	
	body = {
		"cisDosle": cislo_dokladu,
		"varSym": variabilni_symbol,
		"datVyst": dat_vyst,
		"duzpPuv": datum_duzp,
		"datSplat": dat_splat,
	}
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

	# Convert InvoiceData to dict for compatibility with existing functions
	if isinstance(faktura_data, InvoiceData):
		faktura_dict = faktura_data.model_dump()
	else:
		faktura_dict = faktura_data
	
	# Resolve company code used in path
	company_code = _ensure_company_id(base_url, auth, cfg.abra_timeout_s, cfg.abra_verify_tls, cfg.abra_company, faktura_dict)
	# Best-effort lookup of buyer (odběratel) in ABRA adresář (bez vytváření nových)
	partner_ref = _ensure_partner_ext_id(base_url, auth, cfg.abra_timeout_s, company_code, faktura_dict, cfg.abra_verify_tls, cfg.abra_partner_rel_code)

	# Build invoice payload
	body = _map_invoice_json(faktura_data, cfg.abra_series_map, cfg)
	# Set typDokl as coded value string expected by FlexiBee
	if cfg.abra_doc_type_code:
		body["typDokl"] = f"code:{cfg.abra_doc_type_code}"
	else:
		# Defaults: issued invoice → FAKTURA, received invoice → FAKTP
		body["typDokl"] = "code:FAKTURA" if doc_endpoint == "faktura-vydana" else "code:FAKTP"
	# For issued invoices, ABRA requires internal code 'kod'
	if doc_endpoint == "faktura-vydana":
		if isinstance(faktura_data, InvoiceData):
			internal_code = body.pop("cisDosle", None) or faktura_data.cislo_dokladu or faktura_data.variabilni_symbol or f"AI-{int(time.time())}"
		else:
			internal_code = body.pop("cisDosle", None) or faktura_data.get("cislo_dokladu") or faktura_data.get("variabilni_symbol") or f"AI-{int(time.time())}"
		body["kod"] = str(internal_code)
	# Rozhodování mezi původní a novou logikou podle instrukcí
	if isinstance(faktura_data, InvoiceData) and should_use_items_logic(faktura_data):
		# Nová logika: export po řádcích se správně přiřazenou sazbou DPH
		logger.info("Používám novou logiku s položkami pro ABRA export")
		body["polozkyDokladu"] = _build_positions_from_items(faktura_data)
	else:
		# Původní logika: souhrnný export beze změn
		logger.info("Používám původní logiku pro ABRA export")
		# Pozn.: předáváme dict, nikoliv InvoiceData
		body["polozkyDokladu"] = _build_positions_from_totals(faktura_dict)
	# Link partner (odběratel) by external-id reference in buyer field when found
	# FlexiBee supports 'ext:' references directly on foreign keys
	if partner_ref:
		body["firma"] = partner_ref
	else:
		logger.info("ABRA: faktura bude importována bez vazby na adresář (firma).")
	payload = _wrap_winstrom(doc_endpoint, body)

	# Try POST first
	url = f"{base_url}/c/{company_code}/{doc_endpoint}.json"
	resp = _request_with_retry("POST", url, auth=auth, timeout_s=cfg.abra_timeout_s, json_body=payload, verify=cfg.abra_verify_tls)
	if resp.status_code in (200, 201):
		data = resp.json()
		logger.info("ABRA import OK ext=%s id=%s", body.get("cisDosle"), data.get("id"))
		return data
	elif resp.status_code == 409:
		# Conflict: try PUT (update)
		resp2 = _request_with_retry("PUT", url, auth=auth, timeout_s=cfg.abra_timeout_s, json_body=payload, verify=cfg.abra_verify_tls)
		if resp2.status_code in (200, 201):
			data = resp2.json()
			logger.info("ABRA update OK ext=%s id=%s", body.get("cisDosle"), data.get("id"))
			return data
		logger.error("ABRA PUT failed: %s %s", resp2.status_code, resp2.text)
	else:
		logger.error("ABRA POST failed: %s %s", resp.status_code, resp.text)

	# Failure path: dump error payload
	# Write errors to a user-writable location
	_err_dir = get_errors_dir()
	_err_dir.mkdir(parents=True, exist_ok=True)
	error_id = body.get('cisDosle') or 'unknown'
	with open(os.path.join(_err_dir, f"abra_error_{error_id}.json"), "w", encoding="utf-8") as f:
		json.dump({"request": payload, "response_status": resp.status_code, "response_text": resp.text}, f, ensure_ascii=False, indent=2)
	return None
