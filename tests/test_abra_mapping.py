import pytest

from EasyFlex.abra import (
	_build_invoice_payload,
	_ensure_partner_ext_id,
	_map_invoice_json,
	_prepare_buyer_section,
	_request_with_retry,
	import_to_abra,
)
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
		abra_use_kod=True,
		abra_duplicate_kod_strategy="safe_update",
		use_doc_number_as_variable_symbol=False,
		infer_missing_dates=True,
		enable_multi_invoice_segmentation=False,
		date_day_first=True,
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


class _DummyResponse:
	def __init__(self, payload, status_code: int = 200):
		self._payload = payload
		self.status_code = status_code
		self.text = ""
		self.headers = {}

	def json(self):
		return self._payload


def _stub_adresar(monkeypatch, payload, status_code: int = 200) -> None:
	def _fake_request(method, url, auth, timeout_s, json_body=None, verify=True):
		return _DummyResponse(payload, status_code)
	monkeypatch.setattr("EasyFlex.abra._request_with_retry", _fake_request)


def test_prepare_buyer_payload_sets_firma_when_partner_found(monkeypatch) -> None:
	adresar_payload = {
		"winstrom": {
			"adresar": [
				{"kod": "C001", "ic": "12345678", "dic": "CZ12345678", "nazev": "ACME s.r.o.", "psc": "11000", "mesto": "Praha"}
			]
		}
	}
	_stub_adresar(monkeypatch, adresar_payload)
	invoice = {
		"odberatel_jmeno": "ACME s.r.o.",
		"odberatel_ic": "12345678",
		"odberatel_dic": "CZ12345678",
		"odberatel_adresa": "Hlavní 99, 110 00 Praha",
		"odberatel_stat": "CZ",
	}
	ref = _ensure_partner_ext_id("https://server", ("u", "p"), 10, "demo", invoice, True, None)
	assert ref == "code:C001"
	buyer_payload = _prepare_buyer_section(invoice, ref)
	assert buyer_payload["firma"] == "code:C001"
	assert buyer_payload["nazFirmy"] == "ACME s.r.o."
	assert buyer_payload["psc"] == "11000"
	assert buyer_payload["mesto"].lower().startswith("praha")
	assert buyer_payload["ic"] == "12345678"
	assert buyer_payload["dic"] == "CZ12345678"
	assert buyer_payload["stat"] == "code:CZ"


def test_prepare_buyer_payload_without_partner_includes_snapshot_only() -> None:
	invoice = {
		"odberatel_jmeno": "Bez shody s.r.o.",
		"odberatel_ic": "87654321",
		"odberatel_dic": "SK1234567890",
		"odberatel_adresa": "Testovací 1, 120 00 Praha",
		"odberatel_stat": "Slovensko",
	}
	buyer_payload = _prepare_buyer_section(invoice, None)
	assert "firma" not in buyer_payload
	assert buyer_payload["nazFirmy"] == "Bez shody s.r.o."
	assert buyer_payload["ulice"].startswith("Testovací")
	assert buyer_payload["psc"] == "12000"
	assert buyer_payload["mesto"].lower().startswith("praha")
	assert buyer_payload["ic"] == "87654321"
	assert buyer_payload["dic"] == "SK1234567890"
	assert buyer_payload["stat"] == "code:SK"


def test_resolve_partner_by_dic(monkeypatch) -> None:
	adresar_payload = {"winstrom": {"adresar": [{"kod": "DIC1", "dic": "CZ111", "nazev": "Foo"}]}}
	_stub_adresar(monkeypatch, adresar_payload)
	invoice = {"odberatel_dic": "CZ111", "odberatel_jmeno": "Foo"}
	ref = _ensure_partner_ext_id("https://server", ("u", "p"), 10, "demo", invoice, True, None)
	assert ref == "code:DIC1"


def test_resolve_partner_by_name_and_psc(monkeypatch) -> None:
	adresar_payload = {
		"winstrom": {
			"adresar": [
				{"kod": "A1", "nazev": "Novak s.r.o.", "psc": "12000", "mesto": "Praha"},
				{"kod": "A2", "nazev": "Novak s.r.o.", "psc": "99999", "mesto": "Brno"},
			]
		}
	}
	_stub_adresar(monkeypatch, adresar_payload)
	invoice = {
		"odberatel_jmeno": "Novák s.r.o.",
		"odberatel_adresa": "Hlavní 5, 120 00 Praha",
	}
	ref = _ensure_partner_ext_id("https://server", ("u", "p"), 10, "demo", invoice, True, None)
	assert ref == "code:A1"


def test_build_payload_no_match_has_snapshot_and_no_firma() -> None:
	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	invoice = {
		"odberatel_jmeno": "Bez shody s.r.o.",
		"odberatel_ic": "87654321",
		"odberatel_dic": "SK1234567890",
		"odberatel_adresa": "Testovací 1, 120 00 Praha",
		"odberatel_stat": "Slovensko",
		"cislo_dokladu": "X1",
	}
	payload = _build_invoice_payload(invoice, cfg, None, "faktura-vydana")
	entry = payload["winstrom"]["faktura-vydana"][0]
	assert "firma" not in entry
	for key in ("nazFirmy", "ulice", "mesto", "psc", "ic", "dic"):
		assert entry.get(key)


def test_build_payload_match_includes_firma(monkeypatch) -> None:
	adresar_payload = {"winstrom": {"adresar": [{"kod": "HIT", "ic": "555", "nazev": "Match"}]}}
	_stub_adresar(monkeypatch, adresar_payload)
	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	invoice = {"odberatel_ic": "555", "odberatel_jmeno": "Match", "cislo_dokladu": "Y1"}
	ref = _ensure_partner_ext_id("https://server", ("u", "p"), 10, "demo", invoice, True, None)
	payload = _build_invoice_payload(invoice, cfg, ref, "faktura-vydana")
	entry = payload["winstrom"]["faktura-vydana"][0]
	assert entry["firma"] == "code:HIT"
	assert entry["nazFirmy"] == "Match"


def test_request_guard_blocks_adresar_write() -> None:
	with pytest.raises(RuntimeError):
		_request_with_retry("POST", "https://server/c/demo/adresar.json", auth=("u", "p"), timeout_s=1, verify=False)


def test_import_to_abra_logs_ext_id_without_nameerror(monkeypatch, caplog) -> None:
	captured_payloads = []

	class _DummyResponse:
		def __init__(self, payload, status_code: int = 200):
			self._payload = payload
			self.status_code = status_code
			self.text = ""
			self.headers = {}

		def json(self):
			return self._payload

	def _fake_request(method, url, auth, timeout_s, json_body=None, verify=True):
		# Probe and company/adresar GETs return empty OK
		if method == "GET":
			return _DummyResponse({}, 200)
		if method == "POST":
			captured_payloads.append(json_body)
			return _DummyResponse({"id": "RID-1"}, 200)
		raise RuntimeError(f"Unexpected method {method}")

	monkeypatch.setattr("EasyFlex.abra._request_with_retry", _fake_request)
	monkeypatch.setattr("EasyFlex.abra._ensure_company_id", lambda *args, **kwargs: "demo")
	monkeypatch.setattr("EasyFlex.abra._ensure_partner_ext_id", lambda *args, **kwargs: "code:XYZ")

	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	invoice = {"cislo_dokladu": "TEST-123", "odberatel_jmeno": "Test s.r.o."}
	with caplog.at_level("INFO"):
		result = import_to_abra(invoice, cfg)
	assert isinstance(result, dict)
	assert result.get("id") == "RID-1"
	assert result.get("__status") == "created"
	assert captured_payloads, "import_to_abra should send payload"
	entry = captured_payloads[0]["winstrom"]["faktura-vydana"][0]
	assert entry.get("kod") == "TEST-123"
	assert entry.get("firma") == "code:XYZ"


def test_ext_id_is_stable() -> None:
	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	invoice = {"cislo_dokladu": "A-1", "variabilni_symbol": "VS-1"}
	payload1 = _build_invoice_payload(invoice, cfg, None, "faktura-vydana")
	payload2 = _build_invoice_payload(invoice, cfg, None, "faktura-vydana")
	id1 = payload1["winstrom"]["faktura-vydana"][0].get("id")
	id2 = payload2["winstrom"]["faktura-vydana"][0].get("id")
	assert id1 == id2


def test_duplicate_kod_strategy_update(monkeypatch) -> None:
	responses = []

	class _DummyResponse:
		def __init__(self, payload, status_code: int):
			self._payload = payload
			self.status_code = status_code
			self.text = ""
			self.headers = {}

		def json(self):
			return self._payload

	def _fake_request(method, url, auth, timeout_s, json_body=None, verify=True):
		# Company probe + company id resolution
		if method == "GET" and url.endswith("/c.json"):
			return _DummyResponse([{"ico": "1", "firma": "demo"}], 200)
		if method == "GET" and "/faktura-vydana.json" in url:
			# search by kod response
			return _DummyResponse({"winstrom": {"faktura-vydana": [{"id": "123"}]}}, 200)
		if method == "GET" and "/faktura-vydana/" in url:
			return _DummyResponse({"winstrom": {"faktura-vydana": [{"id": "123", "externalIds": ["ext:easyflex:faktura-vydana:DUP-1"]}]}}, 200)
		if method == "POST" and url.endswith("/faktura-vydana.json"):
			# duplicate kod error
			err = {"winstrom": {"results": [{"errors": [{"messageCode": "dokladNeniUnikatniKod"}]}]}}
			return _DummyResponse(err, 400)
		if method == "PUT":
			responses.append(json_body)
			return _DummyResponse({"id": "123"}, 200)
		return _DummyResponse({}, 200)

	monkeypatch.setattr("EasyFlex.abra._request_with_retry", _fake_request)
	cfg = _make_config(abra_doc_endpoint="faktura-vydana", abra_duplicate_kod_strategy="safe_update")
	invoice = {"cislo_dokladu": "DUP-1", "odberatel_jmeno": "Test s.r.o."}
	result = import_to_abra(invoice, cfg)
	assert isinstance(result, dict) and result.get("id") == "123"
	assert responses, "Expected PUT update payload"
	entry = responses[0]["winstrom"]["faktura-vydana"][0]
	assert entry.get("kod") == "DUP-1"


def test_duplicate_kod_strategy_skip(monkeypatch) -> None:
	class _DummyResponse:
		def __init__(self, payload, status_code: int):
			self._payload = payload
			self.status_code = status_code
			self.text = ""
			self.headers = {}

		def json(self):
			return self._payload

	def _fake_request(method, url, auth, timeout_s, json_body=None, verify=True):
		if method == "GET":
			if url.endswith("/c.json"):
				return _DummyResponse([{"ico": "1", "firma": "demo"}], 200)
			return _DummyResponse([{"ico": "1", "firma": "demo"}], 200)
		if method == "POST":
			err = {"winstrom": {"results": [{"errors": [{"messageCode": "dokladNeniUnikatniKod"}]}]}}
			return _DummyResponse(err, 400)
		return _DummyResponse({}, 200)

	monkeypatch.setattr("EasyFlex.abra._request_with_retry", _fake_request)
	cfg = _make_config(abra_doc_endpoint="faktura-vydana", abra_duplicate_kod_strategy="skip")
	invoice = {"cislo_dokladu": "DUP-2", "odberatel_jmeno": "Test s.r.o."}
	result = import_to_abra(invoice, cfg)
	assert isinstance(result, dict)
	assert result.get("__status") == "skipped-duplicate"


def test_payload_kod_changes_with_input() -> None:
	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	inv1 = {"cislo_dokladu": "KOD-1"}
	inv2 = {"cislo_dokladu": "KOD-2"}
	p1 = _build_invoice_payload(inv1, cfg, None, "faktura-vydana")
	p2 = _build_invoice_payload(inv2, cfg, None, "faktura-vydana")
	k1 = p1["winstrom"]["faktura-vydana"][0].get("kod")
	k2 = p2["winstrom"]["faktura-vydana"][0].get("kod")
	assert k1 != k2


def test_foreign_skip_only_with_proof(monkeypatch) -> None:
	class _DummyResponse:
		def __init__(self, payload, status_code: int):
			self._payload = payload
			self.status_code = status_code
			self.text = ""
			self.headers = {}

		def json(self):
			return self._payload

	def _fake_request(method, url, auth, timeout_s, json_body=None, verify=True):
		if method == "GET" and url.endswith("/c.json"):
			return _DummyResponse([{"ico": "1", "firma": "demo"}], 200)
		if method == "GET" and "/faktura-vydana.json" in url:
			return _DummyResponse({"winstrom": {"faktura-vydana": [{"id": "999"}]}}, 200)
		if method == "GET" and "/faktura-vydana/999" in url:
			return _DummyResponse({"winstrom": {"faktura-vydana": [{"id": "999", "externalIds": ["ext:other"]}]}}, 200)
		if method == "POST":
			err = {"winstrom": {"results": [{"errors": [{"messageCode": "dokladNeniUnikatniKod"}]}]}}
			return _DummyResponse(err, 400)
		return _DummyResponse({}, 200)

	monkeypatch.setattr("EasyFlex.abra._request_with_retry", _fake_request)
	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	invoice = {"cislo_dokladu": "COLLIDE", "odberatel_jmeno": "Test s.r.o."}
	result = import_to_abra(invoice, cfg)
	assert isinstance(result, dict)
	assert result.get("__status") == "skipped-duplicate-foreign"


def test_no_false_foreign_when_search_empty(monkeypatch) -> None:
	class _DummyResponse:
		def __init__(self, payload, status_code: int):
			self._payload = payload
			self.status_code = status_code
			self.text = ""
			self.headers = {}

		def json(self):
			return self._payload

	def _fake_request(method, url, auth, timeout_s, json_body=None, verify=True):
		if method == "GET" and url.endswith("/c.json"):
			return _DummyResponse([{"ico": "1", "firma": "demo"}], 200)
		if method == "GET" and "/faktura-vydana.json" in url:
			return _DummyResponse({"winstrom": {"faktura-vydana": []}}, 200)
		if method == "POST":
			err = {"winstrom": {"results": [{"errors": [{"messageCode": "dokladNeniUnikatniKod"}]}]}}
			return _DummyResponse(err, 400)
		return _DummyResponse({}, 200)

	monkeypatch.setattr("EasyFlex.abra._request_with_retry", _fake_request)
	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	invoice = {"cislo_dokladu": "COLLIDE2", "odberatel_jmeno": "Test s.r.o."}
	result = import_to_abra(invoice, cfg)
	assert isinstance(result, dict)
	assert result.get("__status") == "failed-duplicate-not-found"


def test_unverifiable_extid_not_claimed_foreign(monkeypatch) -> None:
	class _DummyResponse:
		def __init__(self, payload, status_code: int):
			self._payload = payload
			self.status_code = status_code
			self.text = ""
			self.headers = {}

		def json(self):
			return self._payload

	def _fake_request(method, url, auth, timeout_s, json_body=None, verify=True):
		if method == "GET" and url.endswith("/c.json"):
			return _DummyResponse([{"ico": "1", "firma": "demo"}], 200)
		if method == "GET" and "/faktura-vydana.json" in url:
			return _DummyResponse({"winstrom": {"faktura-vydana": [{"id": "500"}]}}, 200)
		if method == "GET" and "/faktura-vydana/500" in url:
			return _DummyResponse({"winstrom": {"faktura-vydana": [{"id": "500"}]}}, 200)  # no ext
		if method == "GET" and "/faktura-vydana/ext%3A" in url:
			return _DummyResponse({"winstrom": {"faktura-vydana": []}}, 404)
		if method == "POST":
			err = {"winstrom": {"results": [{"errors": [{"messageCode": "dokladNeniUnikatniKod"}]}]}}
			return _DummyResponse(err, 400)
		return _DummyResponse({}, 200)

	monkeypatch.setattr("EasyFlex.abra._request_with_retry", _fake_request)
	cfg = _make_config(abra_doc_endpoint="faktura-vydana")
	invoice = {"cislo_dokladu": "COLLIDE3", "odberatel_jmeno": "Test s.r.o."}
	result = import_to_abra(invoice, cfg)
	assert isinstance(result, dict)
	assert result.get("__status") == "failed-duplicate-unverifiable"
