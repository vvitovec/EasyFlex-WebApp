from EasyFlex.config import invalidate_config_cache
from EasyFlex.date_helpers import domysleni_chybejicich_datumu, parse_invoice_date



def test_parse_invoice_date_handles_common_formats(monkeypatch) -> None:
	monkeypatch.delenv("EXTRACTION_DATE_ORDER", raising=False)
	invalidate_config_cache()
	assert parse_invoice_date("1.5.2024") == "2024-05-01"
	assert parse_invoice_date("2024-05-01") == "2024-05-01"
	assert parse_invoice_date("") is None
	# Reset cache for other tests
	invalidate_config_cache()


def test_parse_invoice_date_respects_month_first(monkeypatch) -> None:
	monkeypatch.setenv("EXTRACTION_DATE_ORDER", "mm-dd")
	invalidate_config_cache()
	try:
		assert parse_invoice_date("09/01/2024") == "2024-09-01"
	finally:
		monkeypatch.delenv("EXTRACTION_DATE_ORDER", raising=False)
		invalidate_config_cache()


def test_domysleni_infers_missing_due_date() -> None:
	data, warnings = domysleni_chybejicich_datumu({
		"datum_vystaveni": "2024-05-01",
		"datum_splatnosti": None,
		"datum_duzp": None,
	})
	assert data["datum_splatnosti"] == "2024-05-01"
	assert warnings
	assert any("splatnosti" in warning.lower() for warning in warnings)
