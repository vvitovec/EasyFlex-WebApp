from EasyFlex.abra import _map_invoice_json
from EasyFlex.config import AppConfig


def _make_config(**overrides) -> AppConfig:
	cfg = AppConfig(
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
		abra_server="server",
		abra_port=443,
		abra_company="demo",
		abra_username="user",
		abra_password="pass",
		abra_timeout_s=10,
		abra_series_map=None,
		abra_doc_endpoint="faktura-prijata",
		abra_doc_type_code=None,
		abra_partner_rel_code=None,
		abra_verify_tls=True,
		use_doc_number_as_variable_symbol=False,
		infer_missing_dates=True,
		enable_multi_invoice_segmentation=False,
		csv_enable_llm_mapping=False,
	)
	for key, value in overrides.items():
		setattr(cfg, key, value)
	setattr(cfg, "use_issue_date_as_due_date", cfg.infer_missing_dates)
	return cfg


def test_map_invoice_json_infers_due_date_and_warnings() -> None:
	cfg = _make_config(infer_missing_dates=True)
	faktura = {
		"cislo_dokladu": "F-2024-001",
		"variabilni_symbol": "F-2024-001",
		"datum_vystaveni": "2024-05-01",
		"datum_duzp": None,
		"datum_splatnosti": None,
	}
	body = _map_invoice_json(faktura, None, cfg)
	assert body["datVyst"] == "2024-05-01"
	assert body["datSplat"] == "2024-05-01"
	assert "podkladUpozorneni" not in body
