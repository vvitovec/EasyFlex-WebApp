#!/usr/bin/env python3
"""Dry-run builder for ABRA faktura payloads (match vs no-match)."""

from __future__ import annotations

import json

from pathlib import Path
import sys

# Ensure local EasyFlex package is importable when run from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

def _make_config(doc_endpoint: str = "faktura-vydana"):
	from EasyFlex.config import AppConfig

	return AppConfig(
		openai_api_key=None,
		openai_model="gpt-5",
		openai_temperature=0.0,
		openai_top_p=0.15,
		openai_reasoning_effort="medium",
		poppler_path=None,
		concurrency=2,
		dpi=200,
		max_pages=2,
		max_tokens=1024,
		image_max_width=1600,
		image_jpeg_quality=85,
		openai_max_retries=3,
		openai_retry_delay=0.5,
		openai_max_delay=60.0,
		openai_backoff_factor=2.0,
		openai_request_delay=0.5,
		openai_timeout_s=30,
		openai_connection_timeout_s=10,
		abra_server="dry-run",
		abra_port=443,
		abra_company="demo",
		abra_username="dry",
		abra_password="run",
		abra_timeout_s=5,
		abra_series_map=None,
		abra_doc_endpoint=doc_endpoint,
		abra_doc_type_code=None,
		abra_partner_rel_code=None,
		abra_verify_tls=True,
		abra_use_kod=True,
		abra_duplicate_kod_strategy="update",
		use_doc_number_as_variable_symbol=False,
		infer_missing_dates=True,
		enable_multi_invoice_segmentation=False,
		date_day_first=True,
		csv_enable_llm_mapping=False,
	)


def _print_payload(title: str, payload: dict) -> None:
	print(f"\n== {title} ==")
	print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
	from EasyFlex.abra import _build_invoice_payload

	cfg = _make_config()
	# Simulovaný match – partner_ref známý (např. z adresáře)
	match_invoice = {
		"cislo_dokladu": "MATCH-001",
		"variabilni_symbol": "MATCH-001",
		"datum_vystaveni": "2024-01-10",
		"odberatel_jmeno": "Demo s.r.o.",
		"odberatel_adresa": "Hlavní 1, 110 00 Praha",
		"odberatel_ic": "12345678",
		"odberatel_dic": "CZ12345678",
	}
	match_payload = _build_invoice_payload(match_invoice, cfg, "code:DEMO", cfg.abra_doc_endpoint or "faktura-vydana")
	_print_payload("Payload – shoda (firma=code:DEMO)", match_payload)

	# Simulovaný no-match – bez vazby na adresář, jen snapshot
	no_match_invoice = {
		"cislo_dokladu": "NOMATCH-001",
		"variabilni_symbol": "NOMATCH-001",
		"datum_vystaveni": "2024-02-15",
		"odberatel_jmeno": "Neznámý odběratel a.s.",
		"odberatel_adresa": "Testovací 9, 120 00 Praha",
		"odberatel_ic": "87654321",
		"odberatel_dic": "SK1234567890",
		"odberatel_stat": "SK",
	}
	no_match_payload = _build_invoice_payload(no_match_invoice, cfg, None, cfg.abra_doc_endpoint or "faktura-vydana")
	_print_payload("Payload – bez shody (firma neodeslána)", no_match_payload)

	# Varianta bez posílání 'kod' (pokud nechceme ABRA kód)
	cfg_no_kod = _make_config()
	cfg_no_kod.abra_use_kod = False
	no_kod_payload = _build_invoice_payload(match_invoice, cfg_no_kod, "code:DEMO", cfg_no_kod.abra_doc_endpoint or "faktura-vydana")
	_print_payload("Payload – shoda bez 'kod' (abra_use_kod=false)", no_kod_payload)


if __name__ == "__main__":
	main()
