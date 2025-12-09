import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import time
import hashlib
import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, Set
from pathlib import Path

from pdf2image import convert_from_path
try:
	from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError
except Exception:  # pragma: no cover - fallback when submodule missing
	PDFInfoNotInstalledError = PDFPageCountError = PDFSyntaxError = Exception
from PIL import Image
from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError
import pandas as pd

from .models import InvoiceData, InvoiceItem, VATSummary, VATRate
from .config import AppConfig, DEFAULT_OPENAI_MODEL, load_config, get_cache_dir, model_supports_sampling_params
from .invoice_processor import process_invoice_data
from .date_helpers import DATE_FIELDS, DATE_FRIENDLY, domysleni_chybejicich_datumu
from .invoice_warnings import get_warning


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ZERO_DEFAULT_FIELDS = (
	"zaklad_dane_0",
	"zaklad_dane_12",
	"zaklad_dane_21",
	"vyse_dph_12",
	"vyse_dph_21",
)

_global_rate_lock = threading.Lock()
_global_last_request_time = 0.0


@dataclass
class ExtractResult:
	file_path: str
	data: Optional[InvoiceData]
	error: Optional[str] = None
	warnings: List[str] = field(default_factory=list)


@dataclass
class SegmentInfo:
	"""Metainfo jedné PDF stránky po segmentační fázi."""
	page_index: int
	is_invoice_page: bool
	role: str  # start | middle | end | single | unknown
	doc_key: Optional[str]
	confidence: float = 0.0


class InvoiceExtractor:
	"""Extractor for invoices using OpenAI Vision + Structured Outputs."""

	def __init__(
		self,
		config: Optional[AppConfig] = None,
		api_key: Optional[str] = None,
		model: Optional[str] = None,
		temperature: Optional[float] = None,
		top_p: Optional[float] = None,
		reasoning_effort: Optional[str] = None,
		max_pages: Optional[int] = None,
		dpi: Optional[int] = None,
		concurrency: Optional[int] = None,
		poppler_path: Optional[str] = None,
		max_tokens: Optional[int] = None,
		max_retries: Optional[int] = None,
		retry_delay: Optional[float] = None,
		max_delay: Optional[float] = None,
		backoff_factor: Optional[float] = None,
		request_delay: Optional[float] = None,
		timeout_s: Optional[int] = None,
		connection_timeout_s: Optional[int] = None,
		image_max_width: Optional[int] = None,
		image_jpeg_quality: Optional[int] = None,
	):
		self._config: AppConfig = config or load_config()
		self._override_flags = {
			"api_key": api_key is not None,
			"model": model is not None,
			"temperature": temperature is not None,
			"top_p": top_p is not None,
			"reasoning_effort": reasoning_effort is not None,
			"max_pages": max_pages is not None,
			"dpi": dpi is not None,
			"concurrency": concurrency is not None,
			"poppler_path": poppler_path is not None,
			"max_tokens": max_tokens is not None,
			"max_retries": max_retries is not None,
			"retry_delay": retry_delay is not None,
			"max_delay": max_delay is not None,
			"backoff_factor": backoff_factor is not None,
			"request_delay": request_delay is not None,
			"timeout_s": timeout_s is not None,
			"connection_timeout_s": connection_timeout_s is not None,
			"image_max_width": image_max_width is not None,
			"image_jpeg_quality": image_jpeg_quality is not None,
		}
		cfg = self._config
		self._provided_api_key = api_key if api_key is not None else cfg.openai_api_key
		self._client: Optional[OpenAI] = None
		self.model = model if model is not None else (cfg.openai_model or DEFAULT_OPENAI_MODEL)
		self.temperature = float(temperature if temperature is not None else getattr(cfg, "openai_temperature", 0.0))
		self.top_p = max(0.01, min(1.0, float(top_p if top_p is not None else getattr(cfg, "openai_top_p", 0.15))))
		self.reasoning_effort = str(reasoning_effort if reasoning_effort is not None else getattr(cfg, "openai_reasoning_effort", "medium") or "medium")
		self.max_pages = int(max_pages if max_pages is not None else cfg.max_pages)
		self.dpi = int(dpi if dpi is not None else cfg.dpi)
		conc_value = int(concurrency if concurrency is not None else cfg.concurrency)
		self.concurrency = max(1, conc_value)
		self.poppler_path = poppler_path if poppler_path is not None else cfg.poppler_path
		max_tokens_val = int(max_tokens if max_tokens is not None else cfg.max_tokens)
		self.max_tokens = max(1, max_tokens_val)
		self.max_retries = int(max_retries if max_retries is not None else cfg.openai_max_retries)
		self.retry_delay = float(retry_delay if retry_delay is not None else cfg.openai_retry_delay)
		self.max_delay = float(max_delay if max_delay is not None else cfg.openai_max_delay)
		self.backoff_factor = float(backoff_factor if backoff_factor is not None else cfg.openai_backoff_factor)
		self.request_delay = float(request_delay if request_delay is not None else cfg.openai_request_delay)
		self.timeout_s = int(timeout_s if timeout_s is not None else cfg.openai_timeout_s)
		self.connection_timeout_s = int(connection_timeout_s if connection_timeout_s is not None else cfg.openai_connection_timeout_s)
		self._last_request_time = 0.0
		self._cache: Dict[str, Dict[str, Any]] = {}  # Cache pro výsledky extrakce
		self._cache_dir: Path = get_cache_dir()
		self.image_max_width = int(image_max_width if image_max_width is not None else getattr(cfg, "image_max_width", 1600))
		self.image_jpeg_quality = int(image_jpeg_quality if image_jpeg_quality is not None else getattr(cfg, "image_jpeg_quality", 85))
		logger.info(
			"InvoiceExtractor init: model=%s, max_pages=%s, dpi=%s, conc=%s, max_tokens=%s, max_retries=%s, timeout=%ss",
			self.model,
			self.max_pages,
			self.dpi,
			self.concurrency,
			self.max_tokens,
			self.max_retries,
			self.timeout_s,
		)

	def update_config(self, config: AppConfig) -> None:
		"""Refresh runtime configuration, preserving explicit overrides."""
		self._config = config
		if not self._override_flags.get("api_key"):
			if self._provided_api_key != config.openai_api_key:
				self._provided_api_key = config.openai_api_key
				self._client = None  # force re-init with new key
		if not self._override_flags.get("model"):
			self.model = config.openai_model or self.model
		if not self._override_flags.get("temperature"):
			self.temperature = float(getattr(config, "openai_temperature", self.temperature))
		if not self._override_flags.get("top_p"):
			self.top_p = max(0.01, min(1.0, float(getattr(config, "openai_top_p", self.top_p))))
		if not self._override_flags.get("reasoning_effort"):
			self.reasoning_effort = str(getattr(config, "openai_reasoning_effort", self.reasoning_effort))
		if not self._override_flags.get("max_pages"):
			self.max_pages = int(config.max_pages)
		if not self._override_flags.get("dpi"):
			self.dpi = int(config.dpi)
		if not self._override_flags.get("concurrency"):
			self.concurrency = max(1, int(config.concurrency))
		if not self._override_flags.get("poppler_path"):
			self.poppler_path = config.poppler_path
		if not self._override_flags.get("max_tokens"):
			self.max_tokens = max(1, int(config.max_tokens))
		if not self._override_flags.get("max_retries"):
			self.max_retries = int(config.openai_max_retries)
		if not self._override_flags.get("retry_delay"):
			self.retry_delay = float(config.openai_retry_delay)
		if not self._override_flags.get("max_delay"):
			self.max_delay = float(config.openai_max_delay)
		if not self._override_flags.get("backoff_factor"):
			self.backoff_factor = float(config.openai_backoff_factor)
		if not self._override_flags.get("request_delay"):
			self.request_delay = float(config.openai_request_delay)
		if not self._override_flags.get("timeout_s"):
			self.timeout_s = int(config.openai_timeout_s)
		if not self._override_flags.get("connection_timeout_s"):
			self.connection_timeout_s = int(config.openai_connection_timeout_s)
		if not self._override_flags.get("image_max_width"):
			self.image_max_width = int(getattr(config, "image_max_width", self.image_max_width))
		if not self._override_flags.get("image_jpeg_quality"):
			self.image_jpeg_quality = int(getattr(config, "image_jpeg_quality", self.image_jpeg_quality))

	async def extract_auto(self, pdf_path: str) -> List[ExtractResult]:
		"""Rozhodne dle konfigurace, zda provést jednoduchou extrakci nebo dvoufázovou (segmentace → skupiny)."""
		cfg = self._config
		if getattr(cfg, "enable_multi_invoice_segmentation", False):
			return await self.extract_from_pdf_multi_segmented(pdf_path)
		else:
			res = await self.extract_from_pdf(pdf_path)
			return [res]

	def _get_cache_key(self, pdf_path: str) -> str:
		"""Vytvoří cache klíč na základě cesty k PDF a jeho obsahu."""
		try:
			# Použijeme velikost souboru a čas modifikace pro rychlý cache klíč
			stat = os.stat(pdf_path)
			key_data = f"{pdf_path}:{stat.st_size}:{stat.st_mtime}"
			return hashlib.md5(key_data.encode()).hexdigest()
		except Exception:
			# Fallback na hash cesty
			return hashlib.md5(pdf_path.encode()).hexdigest()
	
	def _get_cached_result(self, pdf_path: str) -> Optional[Tuple[Dict[str, Any], List[str]]]:
		"""Získá výsledek z cache pokud existuje."""
		cache_key = self._get_cache_key(pdf_path)
		entry = self._cache.get(cache_key)
		if entry is None:
			entry = self._load_cache_entry(cache_key)
			if entry is not None:
				self._cache[cache_key] = entry
		if entry is None:
			return None
		if "payload" in entry:
			payload = entry.get("payload") or {}
			warnings_raw = entry.get("warnings")
		else:
			# Backwards compatibility: původně se ukládal přímo payload bez metadat
			payload = entry
			warnings_raw = entry.get("__warnings__") if isinstance(entry, dict) else None
		warnings_list: List[str] = []
		if warnings_raw:
			if isinstance(warnings_raw, (list, tuple, set)):
				warnings_list = [str(w).strip() for w in warnings_raw if str(w).strip()]
			else:
				text = str(warnings_raw).strip()
				if text:
					warnings_list = [text]
		return payload, warnings_list

	def _load_cache_entry(self, cache_key: str) -> Optional[Dict[str, Any]]:
		"""Načti uloženou cache položku z disku."""
		try:
			cache_path = self._cache_dir / f"{cache_key}.json"
			if not cache_path.exists():
				return None
			with cache_path.open("r", encoding="utf-8") as fh:
				data = json.load(fh)
			if isinstance(data, dict):
				return data
		except Exception:
			logger.debug("Načtení cache souboru selhalo (key=%s)", cache_key, exc_info=True)
		return None

	def _cache_result(self, pdf_path: str, payload: Dict[str, Any], warnings: List[str]) -> None:
		"""Uloží výsledek do cache."""
		cache_key = self._get_cache_key(pdf_path)
		entry = {"payload": payload, "warnings": list(warnings)}
		self._cache[cache_key] = entry
		try:
			cache_path = self._cache_dir / f"{cache_key}.json"
			with cache_path.open("w", encoding="utf-8") as fh:
				json.dump(entry, fh, ensure_ascii=False)
			logger.debug("Výsledek uložen do cache: %s", cache_key)
		except Exception:
			logger.debug("Zápis cache souboru selhal (key=%s)", cache_key, exc_info=True)

	def _get_client(self) -> OpenAI:
		api_key = self._provided_api_key or os.getenv("OPENAI_API_KEY")
		if not api_key:
			raise RuntimeError("OpenAI API key is not configured. Zadejte API klíč v Nastavení.")
		if self._client is None:
			self._client = OpenAI(api_key=api_key)
		return self._client

	async def extract_from_pdf(self, pdf_path: str) -> ExtractResult:
		"""Extract structured invoice data from a single PDF file."""
		logger.info("Začínám extrakci: %s", pdf_path)
		warnings: List[str] = []

		# Zkontroluj cache podle instrukcí
		cached_result = self._get_cached_result(pdf_path)
		if cached_result:
			payload_cached, cached_warnings = cached_result
			logger.info("Používám výsledek z cache: %s", pdf_path)
			validated = InvoiceData.model_validate(payload_cached)
			processed_invoice = process_invoice_data(validated)
			return ExtractResult(file_path=pdf_path, data=processed_invoice, warnings=cached_warnings)

		try:
			images = await self._pdf_to_images(pdf_path)
			if not images:
				raise ValueError("PDF neobsahuje žádné stránky nebo konverze selhala.")

			# Limit number of pages sent to the model
			images = images[: self.max_pages]
			b64_images = [self._pil_image_to_base64(img) for img in images]
			payload, warnings = await self._run_vision_extraction(b64_images)

			# Apply extraction options from config
			cfg = self._config
			if cfg.use_doc_number_as_variable_symbol and payload.get("cislo_dokladu"):
				payload["variabilni_symbol"] = payload["cislo_dokladu"]
			if getattr(cfg, "infer_missing_dates", False):
				payload, inferred_warnings = domysleni_chybejicich_datumu(payload)
				if inferred_warnings:
					warnings.extend(inferred_warnings)

			validated = InvoiceData.model_validate(payload)

			# Uložení do cache podle instrukcí
			self._cache_result(pdf_path, payload, warnings)

			# Zpracování faktury podle instrukcí (rozhodování mezi původní a novou logikou)
			processed_invoice = process_invoice_data(validated)

			logger.info("Extrakce OK: %s", pdf_path)
			return ExtractResult(file_path=pdf_path, data=processed_invoice, warnings=warnings)
		except Exception as exc:  # noqa: BLE001
			logger.exception("Selhala extrakce pro %s", pdf_path)
			return ExtractResult(file_path=pdf_path, data=None, error=str(exc), warnings=warnings)


	async def batch_extract(
		self,
		pdf_folder: str,
		on_progress: Optional[Callable[[int, int, str], None]] = None,
	) -> List[ExtractResult]:
		"""Batch process a folder of PDFs asynchronously.

		Args:
			pdf_folder: Path to folder with .pdf files
			on_progress: Optional callback(current, total, last_done_file)
		"""
		logger.info("Batch start: %s", pdf_folder)
		pdf_files = [
			os.path.join(pdf_folder, f)
			for f in os.listdir(pdf_folder)
			if f.lower().endswith(".pdf")
		]
		pdf_files.sort()
		total = len(pdf_files)
		results: List[ExtractResult] = []
		if total == 0:
			logger.warning("Ve složce nebyly nalezeny žádné PDF: %s", pdf_folder)
			return results

		sem = asyncio.Semaphore(self.concurrency)

		async def _task(file_path: str) -> Tuple[str, List[ExtractResult]]:
			async with sem:
				# Použij automatiku: pokud je zapnuta segmentace, může vrátit více výsledků
				results = await self.extract_auto(file_path)
				return file_path, results

		completed = 0
		tasks = [asyncio.create_task(_task(p)) for p in pdf_files]
		for coro in asyncio.as_completed(tasks):
			file_path, many = await coro
			results.extend(many)
			completed += 1
			if on_progress:
				on_progress(completed, total, file_path)

		# Keep the original order by file name
		results.sort(key=lambda r: r.file_path)
		logger.info("Batch done: %s files", total)
		return results

	async def extract_from_pdf_multi_segmented(self, pdf_path: str) -> List[ExtractResult]:
		"""Dvoufázová extrakce: 1) segmentace stránek → 2) extrakce po skupinách stránek.

		Vrací list výsledků (jedna položka = jedna faktura nalezená v PDF).
		Při selhání segmentace fallback na standardní extrakci jedné faktury.
		"""
		logger.info("Začínám dvoufázovou extrakci: %s", pdf_path)
		try:
			images = await self._pdf_to_images(pdf_path)
			if not images:
				raise ValueError("PDF neobsahuje žádné stránky nebo konverze selhala.")

			segments = await self._segment_images(images)
			groups = self._build_groups_from_segments(segments)
			# Pokud segmentace nic nenašla, fallback na klasickou extrakci
			if not groups:
				logger.warning("Segmentace nenašla žádné skupiny, fallback na single extrakci: %s", pdf_path)
				return [await self.extract_from_pdf(pdf_path)]

			results: List[ExtractResult] = []
			for start_idx, end_idx, _doc_key in groups:
				subset = images[start_idx : end_idx + 1]
				res = await self._extract_from_images(subset, pdf_path)
				results.append(res)

			logger.info("Dvoufázová extrakce OK: %s → %d faktur", pdf_path, len(results))
			return results
		except Exception as exc:  # noqa: BLE001
			logger.exception("Dvoufázová extrakce selhala pro %s", pdf_path)
			# Fallback: vrať chybu zabalenou jako jeden výsledek
			return [ExtractResult(file_path=pdf_path, data=None, error=str(exc))]

	async def _extract_from_images(self, images: List[Image.Image], pdf_path: str) -> ExtractResult:
		"""Vytěží data z poskytnutých obrázků bez opětovné konverze PDF."""
		warnings: List[str] = []
		try:
			images = images[: self.max_pages]
			b64_images = [self._pil_image_to_base64(img) for img in images]
			payload, warnings = await self._run_vision_extraction(b64_images)

			cfg = self._config
			if cfg.use_doc_number_as_variable_symbol and payload.get("cislo_dokladu"):
				payload["variabilni_symbol"] = payload["cislo_dokladu"]
			if getattr(cfg, "infer_missing_dates", False):
				payload, inferred_warnings = domysleni_chybejicich_datumu(payload)
				if inferred_warnings:
					warnings.extend(inferred_warnings)

			validated = InvoiceData.model_validate(payload)
			processed_invoice = process_invoice_data(validated)
			return ExtractResult(file_path=pdf_path, data=processed_invoice, warnings=warnings)
		except Exception as exc:  # noqa: BLE001
			logger.exception("Selhala extrakce ze subsetu stránek pro %s", pdf_path)
			return ExtractResult(file_path=pdf_path, data=None, error=str(exc), warnings=warnings)

	
	def _build_segmentation_schema(self) -> Dict:
		"""JSON schema pro segmentaci jedné stránky."""
		return {
			"type": "object",
			"additionalProperties": False,
			"properties": {
				"is_invoice_page": {"type": "boolean"},
				"role": {"type": "string", "enum": ["start", "middle", "end", "single", "unknown"]},
				"doc_key": {"type": ["string", "null"]},
				"confidence": {"type": "number"},
			},
			"required": ["is_invoice_page", "role", "doc_key", "confidence"],
		}

	async def _call_openai_segment_page(self, image_b64: str) -> Dict:
		"""Zavolá OpenAI pro klasifikaci jedné stránky (segmentační fáze) s lehkou retry logikou."""
		client = self._get_client()
		messages = [
			{
				"role": "system",
				"content": (
					"Jsi klasifikátor stránek PDF s fakturami. Odpověz jediným JSON dle schématu a bez textu navíc. "
					"Rozhodni, zda jde o stránku faktury, určitou roli (start/middle/end/single/unknown) a doc_key "
					"(přednostně číslo dokladu, jinak stabilní spojení dodavatele a data; pokud nejde bezpečně určit, dej null). "
					"confidence uveď v rozsahu 0–1 podle jistoty. Nic nedopočítávej ani nevymýšlej."
				),
			},
			{
				"role": "user",
				"content": [
					{"type": "text", "text": "Urči is_invoice_page, role, doc_key a confidence pro tuto stránku."},
					{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
				],
			},
		]
		response_format = {
			"type": "json_schema",
			"json_schema": {
				"name": "PageSegmentation",
				"schema": self._build_segmentation_schema(),
				"strict": True,
			},
		}
		await self._throttle_request()
		last_exception = None
		current_model = self.model or DEFAULT_OPENAI_MODEL
		fallback_model = DEFAULT_OPENAI_MODEL
		used_fallback = False
		for attempt in range(self.max_retries + 1):
			try:
				token_param = "max_completion_tokens" if "gpt-5" in str(current_model) else "max_tokens"
				request_kwargs = {
					"model": current_model,
					"messages": messages,
					"response_format": response_format,
					"timeout": self.timeout_s,
				}
				if model_supports_sampling_params(current_model):
					request_kwargs["temperature"] = 0
					request_kwargs["top_p"] = self.top_p
				if self.reasoning_effort and "gpt-5" in str(current_model):
					request_kwargs["reasoning_effort"] = self.reasoning_effort
				request_kwargs[token_param] = min(128, self.max_tokens)
				resp = await asyncio.to_thread(
					lambda: client.chat.completions.create(**request_kwargs)
				)
				choice = resp.choices[0]
				parsed = getattr(getattr(choice, "message", None), "parsed", None)
				if parsed is not None:
					return parsed  # type: ignore[return-value]
				content = choice.message.content  # type: ignore[attr-defined]
				return self._try_parse_json_payload(content)
			except asyncio.TimeoutError as e:
				last_exception = e
				if attempt < self.max_retries:
					delay = self._calculate_backoff_delay(attempt)
					await asyncio.sleep(delay)
					continue
				raise
			except RateLimitError as e:  # type: ignore[name-defined]
				last_exception = e
				if attempt < self.max_retries:
					delay = self._calculate_backoff_delay(attempt)
					await asyncio.sleep(delay)
					continue
				raise
			except (APITimeoutError, APIConnectionError) as e:  # type: ignore[name-defined]
				last_exception = e
				if attempt < self.max_retries:
					delay = self._calculate_backoff_delay(attempt)
					await asyncio.sleep(delay)
					continue
				raise
			except Exception as e:  # noqa: BLE001
				if not used_fallback and current_model != fallback_model:
					current_model = fallback_model
					used_fallback = True
					delay = self._calculate_backoff_delay(attempt)
					await asyncio.sleep(delay)
					continue
				raise
		# Should not reach here
		if last_exception:
			raise last_exception
		return {"is_invoice_page": False, "role": "unknown", "doc_key": None, "confidence": 0.0}

	async def _segment_images(self, images: List[Image.Image]) -> List[SegmentInfo]:
		"""Segmentuje jednotlivé stránky paralelně s omezenou mírou souběžnosti."""
		b64 = [self._pil_image_to_base64(img) for img in images]
		sem = asyncio.Semaphore(self.concurrency)

		async def _seg_task(idx: int, b64img: str) -> SegmentInfo:
			async with sem:
				try:
					payload = await self._call_openai_segment_page(b64img)
					is_invoice = bool(payload.get("is_invoice_page", False))
					role = str(payload.get("role") or "unknown")
					doc_key = payload.get("doc_key")
					if doc_key is not None:
						doc_key = str(doc_key)
					confidence = float(payload.get("confidence") or 0.0)
					return SegmentInfo(page_index=idx, is_invoice_page=is_invoice, role=role, doc_key=doc_key, confidence=confidence)
				except Exception:  # noqa: BLE001
					logger.exception("Segmentace selhala pro stránku %d", idx)
					return SegmentInfo(page_index=idx, is_invoice_page=False, role="unknown", doc_key=None, confidence=0.0)

		tasks = [asyncio.create_task(_seg_task(i, img)) for i, img in enumerate(b64)]
		results: List[SegmentInfo] = [None] * len(tasks)  # type: ignore[assignment]
		for coro in asyncio.as_completed(tasks):
			seg = await coro
			results[seg.page_index] = seg
		return results

	def _build_groups_from_segments(self, segments: List[SegmentInfo]) -> List[Tuple[int, int, Optional[str]]]:
		"""Sestaví souvislé skupiny stránek reprezentující jednotlivé faktury.

		Vrací list trojic (start_index, end_index, doc_key).
		"""
		groups: List[Tuple[int, int, Optional[str]]] = []
		current_start: Optional[int] = None
		current_key: Optional[str] = None
		for seg in segments:
			# Ne-fakturové stránky ukončí probíhající skupinu
			if not seg.is_invoice_page:
				if current_start is not None:
					groups.append((current_start, seg.page_index - 1, current_key))
					current_start, current_key = None, None
				continue

			role = seg.role or "unknown"
			# Single = samostatná skupina
			if role == "single":
				if current_start is not None:
					groups.append((current_start, seg.page_index - 1, current_key))
					current_start, current_key = None, None
				groups.append((seg.page_index, seg.page_index, seg.doc_key))
				continue

			if role == "start" or (role == "unknown" and current_start is None):
				# Začni novou skupinu
				if current_start is not None:
					groups.append((current_start, seg.page_index - 1, current_key))
				current_start, current_key = seg.page_index, seg.doc_key
				continue

			if role == "middle":
				if current_start is None:
					current_start, current_key = seg.page_index, seg.doc_key
				else:
					# Pokud se klíč výrazně změní, uzavři a začni novou skupinu
					if seg.doc_key and current_key and seg.doc_key != current_key and seg.confidence >= 0.6:
						groups.append((current_start, seg.page_index - 1, current_key))
						current_start, current_key = seg.page_index, seg.doc_key
				continue

			if role == "end":
				if current_start is None:
					# obranně začni i ukonči na téže stránce
					groups.append((seg.page_index, seg.page_index, seg.doc_key))
				else:
					groups.append((current_start, seg.page_index, current_key))
				current_start, current_key = None, None

		# Uzavři otevřenou skupinu na konci
		if current_start is not None:
			groups.append((current_start, len(segments) - 1, current_key))

		# Sloučení triviálních chyb: negativní nebo prázdné rozsahy odfiltruj
		cleaned = [(s, e, k) for (s, e, k) in groups if 0 <= s <= e]
		return cleaned

	async def batch_extract_with_tqdm(self, pdf_folder: str) -> List[ExtractResult]:
		"""Batch process with a console progress bar using tqdm and retry failed files.
		try:
			from tqdm import tqdm  # type: ignore
		except ModuleNotFoundError as exc:
			raise RuntimeError("Pro konzolový režim je nutné nainstalovat balíček 'tqdm' (pip install tqdm).") from exc


		Respektuje automatické přepnutí na dvoufázovou segmentaci, pokud je povolena v konfiguraci.
		"""
		pdf_files = [
			os.path.join(pdf_folder, f)
			for f in os.listdir(pdf_folder)
			if f.lower().endswith(".pdf")
		]
		pdf_files.sort()
		total = len(pdf_files)
		results: List[ExtractResult] = []
		if total == 0:
			return results

		sem = asyncio.Semaphore(self.concurrency)

		async def _task(file_path: str) -> Tuple[str, List[ExtractResult]]:
			async with sem:
				return file_path, await self.extract_auto(file_path)

		# First pass: process all files
		tasks = [asyncio.create_task(_task(p)) for p in pdf_files]
		with tqdm(total=total, unit="soubor", desc="Extrakce") as pbar:
			for coro in asyncio.as_completed(tasks):
				file_path, many = await coro
				results.extend(many)
				pbar.update(1)

		# Second pass: retry failed files
		failed_files = list({r.file_path for r in results if r.error is not None})
		if failed_files:
			logger.info("Retry %d neúspěšných souborů", len(failed_files))
			retry_tasks = [asyncio.create_task(_task(f)) for f in failed_files]
			with tqdm(total=len(failed_files), unit="retry", desc="Retry") as pbar:
				for coro in asyncio.as_completed(retry_tasks):
					file_path, many = await coro
					# Remove all previous results for that file and append new ones
					results = [r for r in results if r.file_path != file_path]
					results.extend(many)
					pbar.update(1)

		results.sort(key=lambda r: r.file_path)
		return results

	def save_to_csv(self, data: List[Union[ExtractResult, InvoiceData, Dict]], output_file: str) -> None:
		"""Save extracted data to CSV.

		Accepts a list of ExtractResult, InvoiceData, or plain dicts.
		"""
		rows = self._results_to_rows(data)
		if not rows:
			raise ValueError("Žádná data k uložení do CSV.")

		try:
			df = pd.DataFrame(rows)
			df.to_csv(output_file, index=False, encoding="utf-8")
			logger.info("CSV uloženo: %s", output_file)
		except Exception as exc:  # noqa: BLE001
			logger.exception("Selhalo ukládání CSV do %s", output_file)
			raise

	def preview_results(self, data: List[Union[ExtractResult, InvoiceData, Dict]]) -> pd.DataFrame:
		"""Return a pandas DataFrame for quick preview (for GUI or CLI)."""
		rows = self._results_to_rows(data)
		return pd.DataFrame(rows)

	def _results_to_rows(self, data: List[Union[ExtractResult, InvoiceData, Dict]]) -> List[Dict[str, Any]]:
		"""Normalize supported result types into list of dict rows."""
		rows: List[Dict[str, Any]] = []
		for item in data:
			row = self._result_item_to_row(item)
			if row is not None:
				rows.append(row)
		return rows

	def _result_item_to_row(self, item: Union[ExtractResult, InvoiceData, Dict]) -> Optional[Dict[str, Any]]:
		if isinstance(item, ExtractResult):
			if item.data is None:
				return None
			row = item.data.model_dump()
			row["soubor"] = os.path.basename(item.file_path)
			row["upozorneni"] = " | ".join(item.warnings) if item.warnings else "bez problému"
			return row
		if isinstance(item, InvoiceData):
			row = item.model_dump()
			extra_warning = get_warning(item)
			if extra_warning:
				row["upozorneni"] = extra_warning
			if not row.get("upozorneni"):
				row["upozorneni"] = "bez problému"
			return row
		if isinstance(item, dict):
			row = dict(item)
			if not row.get("upozorneni"):
				row["upozorneni"] = "bez problému"
			return row
		logger.warning("Neznámý typ položky v datech: %s", type(item))
		return None

	async def _pdf_to_images(self, pdf_path: str) -> List[Image.Image]:
		"""Convert a PDF to a list of PIL Images in a background thread."""
		def _convert() -> List[Image.Image]:
			return convert_from_path(
				pdf_path,
				dpi=self.dpi,
				fmt="png",
				poppler_path=self.poppler_path,
			)

		try:
			images: List[Image.Image] = await asyncio.to_thread(_convert)
		except PDFInfoNotInstalledError as exc:
				raise RuntimeError("Pro převod PDF je potřeba nainstalovat Poppler a nastavit cestu v Nastavení.") from exc
		except (PDFPageCountError, PDFSyntaxError, OSError) as exc:
				raise RuntimeError(f"Konverze PDF selhala: {exc}") from exc
		return images

	def _pil_image_to_base64(self, image: Image.Image) -> str:
		# Downscale if necessary and compress to reduce payload size
		try:
			w, h = image.size
			if self.image_max_width and w > self.image_max_width:
				new_h = max(1, int(h * (self.image_max_width / float(w))))
				image = image.resize((self.image_max_width, new_h), Image.LANCZOS)
		except Exception:
			pass
		if image.mode not in ("RGB", "L"):
			image = image.convert("RGB")
		buffer = io.BytesIO()
		try:
			image.save(buffer, format="JPEG", quality=max(50, min(100, self.image_jpeg_quality)), optimize=True)
		except Exception:
			buffer = io.BytesIO()
			image.save(buffer, format="PNG")
		return base64.b64encode(buffer.getvalue()).decode("utf-8")

	async def _run_vision_extraction(self, images_b64: List[str]) -> Tuple[Dict, List[str]]:
		payload = await self._call_openai(images_b64)
		payload = self._postprocess_payload(payload)
		payload, invalid_fields, suspect_fields = self._normalize_dates(payload)
		amounts_ok, amount_issues = self._check_amount_consistency(payload)
		needs_strict_dates = bool(invalid_fields or suspect_fields)
		needs_strict_amounts = not amounts_ok and bool(amount_issues)
		if needs_strict_dates or needs_strict_amounts:
			logger.warning(
				"Ověření extrahovaných dat selhalo (datumy=%s, částky=%s). Provádím opakovaný dotaz.",
				invalid_fields or list(suspect_fields),
				amount_issues,
			)
			payload_retry = await self._call_openai(
				images_b64,
				strict_dates=needs_strict_dates,
				strict_amounts=needs_strict_amounts,
			)
			payload = self._postprocess_payload(payload_retry)
			payload, invalid_fields, suspect_fields = self._normalize_dates(payload)
			amounts_ok, amount_issues = self._check_amount_consistency(payload)
		warnings = self._assemble_date_warnings(invalid_fields, suspect_fields)
		if amount_issues:
			warnings.extend(amount_issues)
		return payload, warnings

	def _postprocess_payload(self, payload: Dict) -> Dict:
		if isinstance(payload, dict) and "polozky" in payload and "položky" not in payload:
			payload["položky"] = payload.pop("polozky")
		self._ensure_zero_amount_defaults(payload)
		return payload

	def _ensure_zero_amount_defaults(self, payload: Dict[str, Any]) -> None:
		for field in ZERO_DEFAULT_FIELDS:
			value = payload.get(field)
			if value in (None, "", "null"):
				payload[field] = 0.0
				continue
			try:
				payload[field] = float(value)
			except (TypeError, ValueError):
				payload[field] = 0.0

	def _normalize_dates(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], Set[str]]:
		invalid_fields: List[str] = []
		parsed: Dict[str, datetime] = {}
		for field in DATE_FIELDS:
			value = payload.get(field)
			normalized, status = self._parse_date_field(value)
			payload[field] = normalized
			if status == "invalid":
				invalid_fields.append(field)
			elif status == "parsed" and normalized:
				parsed[field] = datetime.strptime(normalized, "%Y-%m-%d")
		suspect_fields = self._find_suspect_dates(parsed)
		suspect_fields.difference_update(invalid_fields)
		return payload, invalid_fields, suspect_fields

	def _parse_date_field(self, value: Any) -> Tuple[Optional[str], str]:
		if value is None:
			return None, "empty"
		if isinstance(value, (list, dict)):
			return None, "invalid"
		s = str(value).strip()
		if not s:
			return None, "empty"
		clean = re.sub(r"[\./]", "-", s)
		clean = re.sub(r"\s+", "-", clean)
		clean = clean.strip("-")
		day_first = getattr(self._config, "date_day_first", True)
		if day_first:
			ambiguous_with_sep = ("%d-%m-%Y", "%m-%d-%Y")
			ambiguous_compact = ("%d%m%Y", "%m%d%Y")
		else:
			ambiguous_with_sep = ("%m-%d-%Y", "%d-%m-%Y")
			ambiguous_compact = ("%m%d%Y", "%d%m%Y")
		formats = ("%Y-%m-%d",) + ambiguous_with_sep + ("%Y%m%d",) + ambiguous_compact
		for fmt in formats:
			try:
				dt = datetime.strptime(clean, fmt)
				if not (1990 <= dt.year <= datetime.now().year + 5):
					return None, "invalid"
				return dt.strftime("%Y-%m-%d"), "parsed"
			except ValueError:
				continue
		try:
			dt = datetime.fromisoformat(s)
			if not (1990 <= dt.year <= datetime.now().year + 5):
				return None, "invalid"
			return dt.strftime("%Y-%m-%d"), "parsed"
		except ValueError:
			pass
		return None, "invalid"

	def _find_suspect_dates(self, parsed: Dict[str, datetime]) -> Set[str]:
		suspect: set[str] = set()
		if not parsed:
			return suspect
		issue_dt = parsed.get("datum_vystaveni")
		if issue_dt:
			for field, dt in parsed.items():
				if field == "datum_vystaveni":
					continue
				delta = (dt - issue_dt).days
				if delta < -7 or delta > 370:
					suspect.add(field)
		years = [dt.year for dt in parsed.values()]
		common_year = max(set(years), key=years.count)
		for field, dt in parsed.items():
			if abs(dt.year - common_year) > 1:
				suspect.add(field)
		return suspect

	def _assemble_date_warnings(self, invalid_fields: List[str], suspect_fields: Set[str]) -> List[str]:
		warnings: List[str] = []
		if invalid_fields:
			friendly = ", ".join(DATE_FRIENDLY[f] for f in invalid_fields)
			warnings.append(f"{friendly} se nepodařilo spolehlivě přečíst – zkontrolujte datum ručně.")
		if suspect_fields:
			friendly = ", ".join(DATE_FRIENDLY[f] for f in suspect_fields)
			warnings.append(f"{friendly} vypadá neobvykle – ověřte hodnotu v originálním PDF.")
		return warnings

	def _check_amount_consistency(self, payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
		"""Validate that extracted monetary values are internally consistent.

		Returns (is_consistent, warnings). If there is not enough data to
		perform a check, the method returns (True, []).
		"""
		warnings: List[str] = []
		def _num(value: Any) -> Optional[float]:
			if value in (None, ""):
				return None
			try:
				return float(value)
			except (TypeError, ValueError):
				return None

		tol = 0.5
		base_total = _num(payload.get("zaklad_dane"))
		base_parts_raw = [
			_num(payload.get("zaklad_dane_0")),
			_num(payload.get("zaklad_dane_12")),
			_num(payload.get("zaklad_dane_21")),
		]
		base_parts = [v for v in base_parts_raw if v is not None]

		vat_total = _num(payload.get("vyse_dph"))
		vat_parts_raw = [
			_num(payload.get("vyse_dph_12")),
			_num(payload.get("vyse_dph_21")),
		]
		vat_parts = [v for v in vat_parts_raw if v is not None]

		grand_total = _num(payload.get("celkova_cena"))

		if base_total is not None and base_parts:
			sum_parts = sum(base_parts)
			if abs(base_total - sum_parts) > tol:
				warnings.append("Součet základů daně (0/12/21) neodpovídá poli 'zaklad_dane'.")

		if vat_total is not None and vat_parts:
			sum_vat = sum(vat_parts)
			if abs(vat_total - sum_vat) > tol:
				warnings.append("Součet DPH (12/21) neodpovídá poli 'vyse_dph'.")

		net_for_total: Optional[float]
		if base_total is not None:
			net_for_total = base_total
		elif base_parts:
			net_for_total = sum(base_parts)
		else:
			net_for_total = None

		vat_for_total: Optional[float]
		if vat_total is not None:
			vat_for_total = vat_total
		elif vat_parts:
			vat_for_total = sum(vat_parts)
		else:
			vat_for_total = None

		if grand_total is not None and net_for_total is not None and vat_for_total is not None:
			expected_total = net_for_total + vat_for_total
			if abs(grand_total - expected_total) > tol:
				warnings.append("Součet základů a DPH neodpovídá poli 'celkova_cena'.")

		return len(warnings) == 0, warnings

	def _build_json_schema(self) -> Dict:
		properties = {
			# Původní pole pro zpětnou kompatibilitu
			"cislo_dokladu": {"type": ["string", "null"]},
			"variabilni_symbol": {"type": ["string", "null"]},
			"dodavatel_jmeno": {"type": ["string", "null"]},
			"dodavatel_adresa": {"type": ["string", "null"]},
			"dodavatel_stat": {"type": ["string", "null"]},
			"dodavatel_ic": {"type": ["string", "null"]},
			"dodavatel_dic": {"type": ["string", "null"]},
			"odberatel_jmeno": {"type": ["string", "null"]},
			"odberatel_adresa": {"type": ["string", "null"]},
			"odberatel_stat": {"type": ["string", "null"]},
			"odberatel_ic": {"type": ["string", "null"]},
			"odberatel_dic": {"type": ["string", "null"]},
			"datum_vystaveni": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
			"datum_duzp": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
			"datum_splatnosti": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
			"zaklad_dane": {"type": ["number", "null"]},
			"zaklad_dane_0": {"type": ["number", "null"]},
			"zaklad_dane_12": {"type": ["number", "null"]},
			"zaklad_dane_21": {"type": ["number", "null"]},
			"vyse_dph": {"type": ["number", "null"]},
			"vyse_dph_12": {"type": ["number", "null"]},
			"vyse_dph_21": {"type": ["number", "null"]},
			"celkova_cena": {"type": ["number", "null"]},
			# Nová pole pro rozšířenou funkcionalnost
			# Položky jsou mimo hru – chceme vždy null, aby se schema zjednodušilo
			"položky": {"type": ["null"]},
			"mena": {"type": ["string", "null"]},
		}
		# OpenAI structured JSON vyžaduje, aby required obsahovalo všechny klíče z properties
		required = list(properties.keys())

		schema = {
			"type": "object",
			"additionalProperties": False,
			"properties": properties,
			"required": required,
		}
		return schema

	def _try_parse_json_payload(self, content: Optional[str]) -> Dict:
		"""Try to parse JSON payload from assistant content with several fallbacks."""
		if content is None:
			raise json.JSONDecodeError("Empty content", "", 0)
		# 1) Direct parse
		try:
			return json.loads(content)
		except json.JSONDecodeError:
			pass
		# 2) Fenced ```json ... ```
		m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", content, re.IGNORECASE)
		if m:
			return json.loads(m.group(1))
		# 3) Any fenced code
		m = re.search(r"```[a-zA-Z]*\s*(\{[\s\S]*?\})\s*```", content)
		if m:
			return json.loads(m.group(1))
		# 4) Extract first balanced-looking JSON object
		start = content.find("{")
		end = content.rfind("}")
		if start != -1 and end != -1 and end > start:
			candidate = content[start : end + 1]
			return json.loads(candidate)
		# No luck
		raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)

	async def _call_openai(
		self,
		images_b64: List[str],
		strict_dates: bool = False,
		strict_amounts: bool = False,
	) -> Dict:
		"""Call OpenAI with images and return parsed JSON dict with robust retry logic and timeout."""
		client = self._get_client()
		system_prompt = (
			"Jsi extraktor českých/slovenských faktur. Vždy vrať jediný validní JSON dle schématu InvoiceData a nic jiného. "
			"Pravidla: (1) čti jen údaje jasně vytištěné na faktuře, nic nepočítej a neodvozuj; (2) částky převeď na čísla s tečkou, "
			"bez měnových symbolů a tisícových oddělovačů; (3) datumy vrať jako YYYY-MM-DD, pokud nejdou bezpečně přečíst, dej null; "
			"(4) pole 'položky' vždy null; (5) oprav drobné OCR záměny (0/O, I/1, čárka/tečka) jen pokud je jistota, při nejistotě vrať null; "
			"(6) pokud zjistíš rozpory nebo nejasné částky, ponech sporná pole null; (7) nepřidávej žádné další klíče ani text. "
			"Extrahuj číslo dokladu, variabilní symbol, dodavatel/odběratel (název, adresa, stát, IČ, DIČ), datumy vystavení/DUZP/splatnosti, měnu, "
			"a částky zaklad_dane_0/12/21, vyse_dph_12/21, celkova_cena."
		)
		if strict_dates:
			system_prompt += (
				" DŮSLEDNĚ ověř správnost všech datumů. Pokud datum není na faktuře jasně uvedené, vrať null "
				"a nikdy ho neodhaduj ani nepozměňuj."
			)
		if strict_amounts:
			system_prompt += (
				" Ověř, že součty základů (0/12/21) a DPH (12/21) dávají celkovou částku; pokud čísla nesedí nebo chybí, nech sporná pole null "
				"a nic nedopočítávej."
			)
		messages = [
			{
				"role": "system",
				"content": system_prompt,
			},
			{
				"role": "user",
				"content": [
					{"type": "text", "text": "Zde jsou snímky faktury. Vrať JSON dle schématu."},
					*
					[
						{
							"type": "image_url",
							"image_url": {"url": f"data:image/png;base64,{b64}"},
						}
						for b64 in images_b64
					],
				],
			},
		]

		response_format = {
			"type": "json_schema",
			"json_schema": {
				"name": "InvoiceData",
				"schema": self._build_json_schema(),
				"strict": True,
			},
		}

		# Rate limiting: ensure minimum delay between requests
		await self._throttle_request()

		# Retry logic with exponential backoff and timeout
		last_exception = None
		current_model = self.model or DEFAULT_OPENAI_MODEL
		fallback_model = DEFAULT_OPENAI_MODEL
		current_max_tokens = self.max_tokens
		used_fallback_model = False
		for attempt in range(self.max_retries + 1):
			try:
				token_param = "max_completion_tokens" if "gpt-5" in str(current_model) else "max_tokens"
				request_kwargs = {
					"model": current_model,
					"messages": messages,
					"response_format": response_format,
					"timeout": self.timeout_s,
				}
				if model_supports_sampling_params(current_model):
					request_kwargs["temperature"] = self.temperature
					request_kwargs["top_p"] = self.top_p
				if self.reasoning_effort and "gpt-5" in str(current_model):
					request_kwargs["reasoning_effort"] = self.reasoning_effort
				request_kwargs[token_param] = current_max_tokens
				# Use asyncio.wait_for to implement timeout
				resp = await asyncio.wait_for(
					asyncio.to_thread(
						lambda: client.chat.completions.create(**request_kwargs)
					),
					timeout=self.timeout_s + 5  # Add buffer for asyncio overhead
				)
				choice = resp.choices[0]
				finish_reason = getattr(choice, "finish_reason", None)
				# Prefer parsed field when SDK provides it for structured outputs
				parsed = getattr(getattr(choice, "message", None), "parsed", None)
				if parsed is not None:
					return parsed  # type: ignore[return-value]
				content = choice.message.content  # type: ignore[attr-defined]
				try:
					return self._try_parse_json_payload(content)
				except json.JSONDecodeError as exc:
					logger.error("Nepodařilo se zpracovat JSON výstup: %s", exc)
					# If output was cut due to token limit, increase and retry
					if finish_reason == "length" and current_max_tokens < 4096:
						current_max_tokens = min(current_max_tokens * 2, 4096)
						delay = self._calculate_backoff_delay(attempt)
						logger.warning(
							"Výstup utnut kvůli limitu tokenů (attempt %d/%d), zvyšuji na %s a čekám %.1fs",
							attempt + 1,
							self.max_retries + 1,
							current_max_tokens,
							delay,
						)
						await asyncio.sleep(delay)
						continue
					# Fallback to a model with the most reliable structured JSON
					if not used_fallback_model and current_model != fallback_model:
						logger.warning("Přepínám na stabilní model %s kvůli problémům s JSON výstupem", fallback_model)
						current_model = fallback_model
						used_fallback_model = True
						delay = self._calculate_backoff_delay(attempt)
						await asyncio.sleep(delay)
						continue
					# No more fallbacks -> propagate
					raise

			except asyncio.TimeoutError:
				last_exception = asyncio.TimeoutError("Request timeout")
				if attempt < self.max_retries:
					delay = self._calculate_backoff_delay(attempt)
					logger.warning("Request timeout (attempt %d/%d), čekám %.1fs", 
						attempt + 1, self.max_retries + 1, delay)
					await asyncio.sleep(delay)
					continue
				else:
					logger.error("Request timeout po %d pokusech", self.max_retries + 1)
					raise

			except RateLimitError as e:
				last_exception = e
				if attempt < self.max_retries:
					# Extract retry-after from error if available
					retry_after = self._extract_retry_after(str(e))
					delay = max(retry_after, self._calculate_backoff_delay(attempt))
					logger.warning("Rate limit hit (attempt %d/%d), čekám %.1fs: %s", 
						attempt + 1, self.max_retries + 1, delay, str(e))
					await asyncio.sleep(delay)
					continue
				else:
					logger.error("Rate limit exceeded after %d pokusů: %s", self.max_retries + 1, str(e))
					raise

			except (APITimeoutError, APIConnectionError) as e:
				last_exception = e
				if attempt < self.max_retries:
					delay = self._calculate_backoff_delay(attempt)
					logger.warning("API chyba (attempt %d/%d), čekám %.1fs: %s", 
						attempt + 1, self.max_retries + 1, delay, str(e))
					await asyncio.sleep(delay)
					continue
				else:
					logger.error("API chyba po %d pokusech: %s", self.max_retries + 1, str(e))
					raise

			except Exception as e:  # noqa: BLE001
				# Non-retryable errors (auth, invalid request, etc.)
				logger.error("Nepředvídatelná chyba OpenAI API: %s", str(e))
				raise

		# This should never be reached, but just in case
		if last_exception:
			raise last_exception

	def _extract_retry_after(self, error_message: str) -> float:
		"""Extract retry-after value from OpenAI error message."""
		import re
		# Look for patterns like "Please try again in 272ms" or "retry after 1.5s"
		patterns = [
			r"try again in (\d+(?:\.\d+)?)ms",
			r"retry after (\d+(?:\.\d+)?)s",
			r"wait (\d+(?:\.\d+)?) seconds",
		]
		
		for pattern in patterns:
			match = re.search(pattern, error_message, re.IGNORECASE)
			if match:
				value = float(match.group(1))
				# Convert ms to seconds if needed
				if "ms" in pattern:
					value = value / 1000.0
				return max(value, 1.0)  # Minimum 1 second
		
		return 0.0  # No retry-after found

	async def _throttle_request(self) -> None:
		"""Ensure minimum delay between requests (per-instance + global)."""
		# Instance-level spacing
		now = time.time()
		delta_local = now - self._last_request_time
		if delta_local < self.request_delay:
			await asyncio.sleep(self.request_delay - delta_local)
		self._last_request_time = time.time()

		# Global spacing across all instances (thread-safe)
		global _global_last_request_time
		while True:
			with _global_rate_lock:
				now2 = time.time()
				delta_global = now2 - _global_last_request_time
				if delta_global >= self.request_delay:
					_global_last_request_time = now2
					break
				wait_s = self.request_delay - delta_global
			# Sleep outside lock
			await asyncio.sleep(wait_s)

	def _calculate_backoff_delay(self, attempt: int) -> float:
		"""Calculate exponential backoff delay with jitter."""
		base_delay = self.retry_delay * (self.backoff_factor ** attempt)
		# Add jitter to prevent thundering herd
		jitter = random.uniform(0.1, 0.3) * base_delay
		delay = min(base_delay + jitter, self.max_delay)
		return delay
