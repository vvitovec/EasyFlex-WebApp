from types import SimpleNamespace

import pytest

from webapp.company_work import CompanyWorkAbraClient, CompanyWorkError, sanitize_employment_copy


class DummyResponse:
	def __init__(self, payload, status_code=200, text=""):
		self._payload = payload
		self.status_code = status_code
		self.text = text

	def json(self):
		return self._payload


def _cfg(**overrides):
	values = {
		"abra_server": "abra.local",
		"abra_port": 443,
		"abra_username": "user",
		"abra_password": "pass",
		"abra_verify_tls": True,
		"abra_timeout_s": 10,
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def _client_with_payloads(monkeypatch, payloads):
	calls = []
	queue = list(payloads)

	def fake_request(method, url, **kwargs):
		calls.append((method, url, kwargs.get("json_body")))
		item = queue.pop(0)
		if isinstance(item, DummyResponse):
			return item
		return DummyResponse(item)

	monkeypatch.setattr("webapp.company_work._request_with_retry", fake_request)
	return CompanyWorkAbraClient(_cfg()), calls


def test_list_companies_normalizes_winstrom_payload(monkeypatch):
	client, calls = _client_with_payloads(
		monkeypatch,
		[
			{
				"winstrom": {
					"c": [
						{"dbNazev": "beta", "nazev": "Beta s.r.o."},
						{"firma": "alpha", "nazev": "Alfa s.r.o."},
					]
				}
			}
		],
	)

	companies = client.list_companies()

	assert companies == [
		{"code": "alpha", "name": "Alfa s.r.o."},
		{"code": "beta", "name": "Beta s.r.o."},
	]
	assert calls[0][1].endswith("/c.json?limit=0")


def test_list_companies_accepts_companies_company_payload(monkeypatch):
	client, _calls = _client_with_payloads(
		monkeypatch,
		[
			{
				"companies": {
					"company": [
						{"dbNazev": "albac_s_r_o_", "nazev": "ALBAC s.r.o."},
					]
				}
			}
		],
	)

	assert client.list_companies() == [{"code": "albac_s_r_o_", "name": "ALBAC s.r.o."}]


def test_list_employees_builds_code_refs(monkeypatch):
	client, _calls = _client_with_payloads(
		monkeypatch,
		[
			{
				"winstrom": {
					"osoba": [
						{
							"id": "7",
							"kod": "U123",
							"jmeno": "Jana",
							"prijmeni": "Novakova",
							"osobaHlav": "42",
						},
						{"id": "8", "jmeno": "Petr", "prijmeni": "Svoboda"},
					]
				}
			}
		],
	)

	employees = client.list_employees("demo")

	assert employees[0]["ref"] == "42"
	assert employees[0]["label"] == "Jana Novakova (U123)"
	assert employees[1]["ref"] == "8"
	assert "osobaHlav" in _calls[0][1]


def test_sanitize_employment_copy_removes_identifiers_and_metadata():
	source = {
		"id": "12",
		"idPpv": "4003034975980",
		"kod": "OLD",
		"lastUpdate": "2026-01-01T10:00:00",
		"external-ids": ["ext:MYAPP:employment:seed"],
		"nazev": "Standard",
		"osoba": "code:U123",
		"typPomK": "typPom.hlavni",
		"pracPomHlav": "code:2-DPP",
		"stredisko": {"$": "code:C", "@showAs": "Centrum"},
		"nested": {"id": "99", "value": "keep", "@ref": "ignore"},
	}

	cleaned = sanitize_employment_copy(source)

	assert "id" not in cleaned
	assert "idPpv" not in cleaned
	assert "kod" not in cleaned
	assert "lastUpdate" not in cleaned
	assert "external-ids" not in cleaned
	assert "pracPomHlav" not in cleaned
	assert cleaned["osoba"] == "code:U123"
	assert cleaned["stredisko"] == "code:C"
	assert cleaned["nested"] == {"value": "keep"}


def test_list_employments_uses_path_filter(monkeypatch):
	client, calls = _client_with_payloads(
		monkeypatch,
		[
			{
				"winstrom": {
				"pracovni-pomer": [
						{"id": "10", "kod": "2-DPP", "osoba": "7", "nazev": "Dohoda", "aktivniOd": "2024-01-01"},
						{"id": "11", "kod": "2-DPP", "osoba": "7", "nazev": "Dohoda", "aktivniOd": "2026-01-01"},
					]
				}
			}
		],
	)

	rows = client.list_employments("demo", "7")

	assert [row["id"] for row in rows] == ["11", "10"]
	assert "/pracovni-pomer/(osoba=\"7\").json" in calls[0][1]


def test_build_duplicate_payload_applies_user_overrides(monkeypatch):
	client, _calls = _client_with_payloads(monkeypatch, [])
	source = {
		"id": "12",
		"kod": "OLD",
		"nazev": "Puvodni",
		"osoba": "code:U123",
		"aktivniOd": "2024-01-01",
		"zacatek": "2024-01-01",
		"skutecnyNastup": "2024-01-01",
	}

	payload = client.build_duplicate_payload(
		source,
		{"kod": "NEW", "nazev": "Novy pomer", "datumOd": "2026-08-01", "datumDo": "2026-12-31"},
	)
	entry = payload["winstrom"]["pracovni-pomer"][0]

	assert entry["kod"] == "NEW"
	assert entry["nazev"] == "Novy pomer"
	assert entry["aktivniOd"] == "2026-08-01"
	assert entry["zacatek"] == "2026-08-01"
	assert entry["skutecnyNastup"] == "2026-08-01"
	assert entry["aktivniDo"] == "2026-12-31"
	assert entry["konecPomeru"] == "2026-12-31"
	assert "datumOd" not in entry
	assert "datumDo" not in entry
	assert entry["osoba"] == "code:U123"
	assert "id" not in entry


def test_client_requires_abra_credentials():
	with pytest.raises(CompanyWorkError):
		CompanyWorkAbraClient(_cfg(abra_username=None))
