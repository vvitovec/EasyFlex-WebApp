import asyncio

from PIL import Image

from EasyFlex.config import AppConfig, model_supports_sampling_params
from EasyFlex.extractor import InvoiceExtractor, SegmentInfo, ExtractResult


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


def test_postprocess_payload_normalizes_currency_variants() -> None:
	extractor = InvoiceExtractor(config=_make_config(), api_key="sk-test")
	payload = {"mena": "€", "zaklad_dane_12": "100"}
	normalized = extractor._postprocess_payload(payload)
	assert normalized["mena"] == "EUR"


def test_postprocess_payload_falls_back_to_czk_for_unknown_currency() -> None:
	extractor = InvoiceExtractor(config=_make_config(), api_key="sk-test")
	payload = {"mena": "usd", "zaklad_dane_12": "100"}
	normalized = extractor._postprocess_payload(payload)
	assert normalized["mena"] == "CZK"


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


def test_call_openai_stops_after_two_empty_contents_on_gpt5(monkeypatch) -> None:
	extractor = InvoiceExtractor(config=_make_config(openai_max_retries=5), api_key="sk-test")
	scripted = [
		_FakeResponse(_FakeChoice(content="", finish_reason=None)),
		_FakeResponse(_FakeChoice(content="", finish_reason=None)),
	]
	completions = _FakeCompletions(scripted)
	client = _FakeClient(completions)

	async def _noop():
		return None

	monkeypatch.setattr(extractor, "_get_client", lambda: client)
	monkeypatch.setattr(extractor, "_throttle_request", _noop)
	monkeypatch.setattr(extractor, "_calculate_backoff_delay", lambda _attempt: 0.0)

	try:
		asyncio.run(extractor._call_openai(["ZmFrZS1pbWFnZQ=="]))
		assert False, "Expected RuntimeError for repeated empty content"
	except RuntimeError as exc:
		assert "prázdný obsah" in str(exc).lower()
	assert len(completions.calls) == 2
	assert completions.calls[0].get("reasoning_effort") == "medium"
	assert completions.calls[1].get("reasoning_effort") == "minimal"


def test_call_openai_respects_invoice_runtime_deadline(monkeypatch) -> None:
	extractor = InvoiceExtractor(
		config=_make_config(openai_max_retries=0, openai_timeout_s=30),
		api_key="sk-test",
		invoice_runtime_limit_s=1,
	)

	async def _noop():
		return None

	monkeypatch.setattr(extractor, "_throttle_request", _noop)
	monkeypatch.setattr(extractor, "_get_client", lambda: _FakeClient(_FakeCompletions([])))

	try:
		asyncio.run(
			extractor._call_openai(
				["ZmFrZS1pbWFnZQ=="],
				invoice_deadline_monotonic=0.0,
			)
		)
		assert False, "Expected timeout due to exhausted invoice runtime budget"
	except asyncio.TimeoutError as exc:
		assert "runtime limit" in str(exc).lower()


def test_extract_auto_streams_group_results(monkeypatch) -> None:
	extractor = InvoiceExtractor(
		config=_make_config(enable_multi_invoice_segmentation=True),
		api_key="sk-test",
	)

	async def _fake_pdf_to_images(_pdf_path: str):
		return [object(), object(), object()]

	async def _fake_segment_images(_images):
		return []

	def _fake_build_groups(_segments):
		return [(0, 0, None), (1, 1, None), (2, 2, None)]

	async def _fake_extract_from_images(_images, pdf_path: str):
		return ExtractResult(file_path=pdf_path, data=None, error="mock-error")

	monkeypatch.setattr(extractor, "_pdf_to_images", _fake_pdf_to_images)
	monkeypatch.setattr(extractor, "_segment_images", _fake_segment_images)
	monkeypatch.setattr(extractor, "_build_groups_from_segments", _fake_build_groups)
	monkeypatch.setattr(extractor, "_extract_from_images", _fake_extract_from_images)

	streamed_indexes = []

	def _on_result(res):
		streamed_indexes.append(getattr(res, "_invoice_group_index", None))

	results = asyncio.run(extractor.extract_auto("dummy.pdf", on_result=_on_result))
	assert len(results) == 3
	assert streamed_indexes == [1, 2, 3]


def test_extract_auto_image_uses_vision_without_pdf_conversion(monkeypatch, tmp_path) -> None:
	extractor = InvoiceExtractor(
		config=_make_config(enable_multi_invoice_segmentation=False),
		api_key="sk-test",
	)
	image_path = tmp_path / "invoice.jpg"
	Image.new("RGB", (16, 16), "white").save(image_path, format="JPEG")

	async def _unexpected_pdf_to_images(_pdf_path: str):
		raise AssertionError("PDF conversion should not run for images")

	async def _fake_run_vision(images_b64, *, invoice_deadline_monotonic=None):
		assert len(images_b64) == 1
		return {"cislo_dokladu": "IMG-001", "mena": "CZK"}, []

	monkeypatch.setattr(extractor, "_pdf_to_images", _unexpected_pdf_to_images)
	monkeypatch.setattr(extractor, "_run_vision_extraction", _fake_run_vision)

	results = asyncio.run(extractor.extract_auto(str(image_path)))
	assert len(results) == 1
	assert results[0].error is None
	assert results[0].data is not None
	assert results[0].data.cislo_dokladu == "IMG-001"


def test_extract_auto_image_ignores_pdf_segmentation(monkeypatch, tmp_path) -> None:
	extractor = InvoiceExtractor(
		config=_make_config(enable_multi_invoice_segmentation=True),
		api_key="sk-test",
	)
	image_path = tmp_path / "invoice.png"
	Image.new("RGB", (16, 16), "white").save(image_path, format="PNG")

	async def _unexpected_segment_images(_images):
		raise AssertionError("PDF segmentation should not run for images")

	async def _fake_run_vision(images_b64, *, invoice_deadline_monotonic=None):
		assert len(images_b64) == 1
		return {"cislo_dokladu": "IMG-002", "mena": "CZK"}, []

	monkeypatch.setattr(extractor, "_segment_images", _unexpected_segment_images)
	monkeypatch.setattr(extractor, "_run_vision_extraction", _fake_run_vision)

	streamed_indexes = []

	def _on_result(res):
		streamed_indexes.append(getattr(res, "_invoice_group_index", None))

	results = asyncio.run(extractor.extract_auto(str(image_path), on_result=_on_result))
	assert len(results) == 1
	assert streamed_indexes == [1]
	assert results[0].data is not None
	assert results[0].data.cislo_dokladu == "IMG-002"


def test_extract_auto_invalid_image_returns_error(monkeypatch, tmp_path) -> None:
	extractor = InvoiceExtractor(
		config=_make_config(enable_multi_invoice_segmentation=True),
		api_key="sk-test",
	)
	image_path = tmp_path / "broken.png"
	image_path.write_bytes(b"not an image")

	async def _unexpected_run_vision(*_args, **_kwargs):
		raise AssertionError("Vision extraction should not run for invalid images")

	monkeypatch.setattr(extractor, "_run_vision_extraction", _unexpected_run_vision)

	results = asyncio.run(extractor.extract_auto(str(image_path)))
	assert len(results) == 1
	assert results[0].data is None
	assert results[0].error is not None
	assert "Načtení obrázku selhalo" in results[0].error
