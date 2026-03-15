import asyncio

from EasyFlex.config import AppConfig, model_supports_sampling_params
from EasyFlex.extractor import InvoiceExtractor, SegmentInfo


def _make_config(**overrides) -> AppConfig:
	cfg = AppConfig(
		openai_api_key="sk-test",
		openai_model="gpt-5",
		openai_temperature=0.0,
		openai_top_p=0.15,
		openai_reasoning_effort="medium",
		poppler_path=None,
		concurrency=2,
		dpi=200,
		max_pages=4,
		max_tokens=300,
		image_max_width=1600,
		image_jpeg_quality=85,
		openai_max_retries=3,
		openai_retry_delay=0.0,
		openai_max_delay=0.0,
		openai_backoff_factor=2.0,
		openai_request_delay=0.0,
		openai_timeout_s=10,
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
		enable_multi_invoice_segmentation=True,
		date_day_first=True,
		csv_enable_llm_mapping=False,
	)
	for key, value in overrides.items():
		setattr(cfg, key, value)
	return cfg


class _FakeMessage:
	def __init__(self, content=None, parsed=None):
		self.content = content
		self.parsed = parsed


class _FakeChoice:
	def __init__(self, content=None, parsed=None, finish_reason=None):
		self.message = _FakeMessage(content=content, parsed=parsed)
		self.finish_reason = finish_reason


class _FakeResponse:
	def __init__(self, choice: _FakeChoice):
		self.choices = [choice]


class _FakeCompletions:
	def __init__(self, scripted):
		self._scripted = list(scripted)
		self.calls = []

	def create(self, **kwargs):
		self.calls.append(kwargs)
		item = self._scripted.pop(0)
		if isinstance(item, Exception):
			raise item
		return item


class _FakeChat:
	def __init__(self, completions: _FakeCompletions):
		self.completions = completions


class _FakeClient:
	def __init__(self, completions: _FakeCompletions):
		self.chat = _FakeChat(completions)


def test_model_supports_sampling_params_disables_for_gpt5_family() -> None:
	assert model_supports_sampling_params("gpt-5") is False
	assert model_supports_sampling_params("gpt-5-mini") is False
	assert model_supports_sampling_params("gpt-4.1-mini") is True


def test_try_parse_json_payload_accepts_structured_content_list() -> None:
	extractor = InvoiceExtractor(config=_make_config(), api_key="sk-test")
	payload = extractor._try_parse_json_payload(
		[
			{
				"type": "output_text",
				"text": '{"is_invoice_page": true, "role": "single", "doc_key": "INV-1", "confidence": 0.98}',
			}
		]
	)
	assert payload["is_invoice_page"] is True
	assert payload["role"] == "single"


def test_segment_page_retries_on_empty_json_with_higher_token_budget(monkeypatch) -> None:
	extractor = InvoiceExtractor(config=_make_config(max_tokens=300, openai_max_retries=2), api_key="sk-test")
	scripted = [
		_FakeResponse(_FakeChoice(content="", finish_reason="length")),
		_FakeResponse(
			_FakeChoice(
				content='{"is_invoice_page": true, "role": "single", "doc_key": "INV-2", "confidence": 0.95}'
			)
		),
	]
	completions = _FakeCompletions(scripted)
	client = _FakeClient(completions)

	async def _noop():
		return None

	monkeypatch.setattr(extractor, "_get_client", lambda: client)
	monkeypatch.setattr(extractor, "_throttle_request", _noop)
	monkeypatch.setattr(extractor, "_calculate_backoff_delay", lambda _attempt: 0.0)

	result = asyncio.run(extractor._call_openai_segment_page("ZmFrZS1pbWFnZQ=="))
	assert result["doc_key"] == "INV-2"
	assert len(completions.calls) == 2
	assert completions.calls[0]["max_completion_tokens"] == 300
	assert completions.calls[1]["max_completion_tokens"] == 600
	assert "temperature" not in completions.calls[0]
	assert completions.calls[0]["reasoning_effort"] == "minimal"


def test_build_groups_from_segments_splits_unknown_on_confident_key_change() -> None:
	extractor = InvoiceExtractor(config=_make_config(), api_key="sk-test")
	segments = [
		SegmentInfo(page_index=0, is_invoice_page=True, role="start", doc_key="INV-1", confidence=0.9),
		SegmentInfo(page_index=1, is_invoice_page=True, role="unknown", doc_key="INV-2", confidence=0.9),
		SegmentInfo(page_index=2, is_invoice_page=True, role="end", doc_key="INV-2", confidence=0.9),
	]
	groups = extractor._build_groups_from_segments(segments)
	assert groups == [(0, 0, "INV-1"), (1, 2, "INV-2")]
