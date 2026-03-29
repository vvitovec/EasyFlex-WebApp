import logging
import os
import json
import configparser
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from datetime import datetime

try:
	from dotenv import load_dotenv
except ImportError:  # Fallback when python-dotenv is not installed
	def load_dotenv(*_args, **_kwargs):  # type: ignore[return-type]
		return False
from pathlib import Path
import sys


DEFAULT_OPENAI_MODEL = "gpt-5"

_CONFIG_ENV_KEYS: Tuple[str, ...] = (
	"OPENAI_API_KEY",
	"OPENAI_MODEL",
	"OPENAI_TEMPERATURE",
	"OPENAI_TOP_P",
	"OPENAI_REASONING_EFFORT",
	"POPPLER_PATH",
	"CONCURRENCY",
	"PDF_DPI",
	"MAX_PAGES",
	"MAX_TOKENS",
	"IMAGE_MAX_WIDTH",
	"IMAGE_JPEG_QUALITY",
	"OPENAI_MAX_RETRIES",
	"OPENAI_RETRY_DELAY",
	"OPENAI_MAX_DELAY",
	"OPENAI_BACKOFF_FACTOR",
	"OPENAI_REQUEST_DELAY",
	"OPENAI_TIMEOUT_S",
	"OPENAI_CONNECTION_TIMEOUT_S",
	"ABRA_SERVER",
	"ABRA_PORT",
	"ABRA_COMPANY",
	"ABRA_USERNAME",
	"ABRA_PASSWORD",
	"ABRA_TIMEOUT_S",
	"ABRA_SERIES_MAP",
	"ABRA_DOC_ENDPOINT",
	"ABRA_DOC_TYPE_CODE",
	"ABRA_PARTNER_REL_CODE",
	"ABRA_VERIFY_TLS",
	"ABRA_USE_KOD",
	"ABRA_DUPLICATE_KOD_STRATEGY",
	"EXTRACTION_DATE_ORDER",
	"CSV_ENABLE_LLM_MAPPING",
)

_CONFIG_CACHE: Optional["AppConfig"] = None
_CONFIG_CACHE_STATE: Optional[Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Tuple[Tuple[str, str], ...]]] = None


def _parse_date_order(value: Optional[str]) -> bool:
	"""Interpret user-friendly date order strings (default day-first)."""
	if not value:
		return True
	normalized = str(value).strip().lower().replace("_", "-").replace(" ", "")
	if not normalized:
		return True
	if normalized in {"dd-mm", "day-month", "dayfirst", "day-first", "dm", "ddmm"}:
		return True
	if normalized in {"mm-dd", "month-day", "monthfirst", "month-first", "md", "mmdd"}:
		return False
	# Fallback to Boolean-like parsing (true → day-first)
	if normalized in {"true", "1", "yes", "ano"}:
		return True
	if normalized in {"false", "0", "no", "ne"}:
		return False
	return True


def model_supports_sampling_params(model_name: Optional[str]) -> bool:
	"""Return True if the model allows custom sampling settings (temperature/top_p)."""
	if not model_name:
		return True
	model_lower = str(model_name).lower()
	# Reasoning-style models (including GPT-5 family) currently force default sampling params.
	if (
		model_lower.startswith("o1")
		or model_lower.startswith("o3")
		or model_lower.startswith("gpt-5")
		or "reasoning" in model_lower
	):
		return False
	return True


@dataclass
class AppConfig:
	openai_api_key: Optional[str]
	openai_model: str
	openai_temperature: float
	openai_top_p: float
	openai_reasoning_effort: str
	poppler_path: Optional[str]
	concurrency: int
	dpi: int
	max_pages: int
	max_tokens: int
	# Image optimization
	image_max_width: int
	image_jpeg_quality: int
	# Rate limiting and retry settings
	openai_max_retries: int
	openai_retry_delay: float
	openai_max_delay: float
	openai_backoff_factor: float
	openai_request_delay: float
	openai_timeout_s: int
	openai_connection_timeout_s: int
	# ABRA Flexi
	abra_server: Optional[str]
	abra_port: Optional[int]
	abra_company: Optional[str]
	abra_username: Optional[str]
	abra_password: Optional[str]
	abra_timeout_s: int
	abra_series_map: Optional[str]
	abra_doc_endpoint: str
	abra_doc_type_code: Optional[str]
	abra_partner_rel_code: Optional[str]
	abra_verify_tls: bool
	abra_use_kod: bool
	abra_duplicate_kod_strategy: str
	# Extraction options
	use_doc_number_as_variable_symbol: bool
	infer_missing_dates: bool
	enable_multi_invoice_segmentation: bool
	date_day_first: bool
	# CSV processing
	csv_enable_llm_mapping: bool


def _default_settings_path() -> Path:
	"""Path to bundled default settings shipped with the application."""
	return Path(__file__).resolve().parent / "settings.ini"


def _user_config_dir() -> Path:
	"""Return a per-user configuration directory suitable for persistence."""
	if os.name == "nt":
		base = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
	elif sys.platform == "darwin":
		base = Path.home() / "Library" / "Application Support"
	else:
		base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
	cfg_dir = base / "EasyFlex"
	cfg_dir.mkdir(parents=True, exist_ok=True)
	return cfg_dir


def _settings_path() -> Path:
	"""Return path to the user-writable settings.ini file."""
	return _user_config_dir() / "settings.ini"


def _snapshot_env() -> Tuple[Tuple[str, str], ...]:
	"""Capture relevant environment variables for config caching."""
	return tuple(sorted((key, os.getenv(key) or "") for key in _CONFIG_ENV_KEYS))


def _snapshot_config_state() -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Tuple[Tuple[str, str], ...]]:
	"""Return current state tuple used to detect config invalidation."""
	user_path = _settings_path()
	user_mtime = user_path.stat().st_mtime if user_path.exists() else None
	default_path = _default_settings_path()
	default_mtime = default_path.stat().st_mtime if default_path.exists() else None
	package_env_path = Path(__file__).resolve().parent / ".env"
	package_env_mtime = package_env_path.stat().st_mtime if package_env_path.exists() else None
	cwd_env_path = Path.cwd() / ".env"
	cwd_env_mtime = cwd_env_path.stat().st_mtime if cwd_env_path.exists() else None
	return (user_mtime, default_mtime, package_env_mtime, cwd_env_mtime, _snapshot_env())


def invalidate_config_cache() -> None:
	"""Reset cached AppConfig so next load_config reads fresh values."""
	global _CONFIG_CACHE, _CONFIG_CACHE_STATE
	_CONFIG_CACHE = None
	_CONFIG_CACHE_STATE = None


def get_errors_dir() -> Path:
	"""Return a user-writable directory for error JSON dumps."""
	project_errors = Path(__file__).resolve().parent.parent / "errors"
	err_dir = project_errors if project_errors.exists() else _settings_path().parent / "errors"
	err_dir.mkdir(parents=True, exist_ok=True)
	return err_dir


def get_cache_dir() -> Path:
	"""Directory for persistent extraction cache entries."""
	cache_dir = _user_config_dir() / "cache"
	cache_dir.mkdir(parents=True, exist_ok=True)
	return cache_dir


def read_settings() -> configparser.ConfigParser:
	"""Read layered settings, combining bundled defaults and user overrides."""
	cp = configparser.ConfigParser()
	default_path = _default_settings_path()
	if default_path.exists():
		template = configparser.ConfigParser()
		template.read(default_path, encoding="utf-8")
		for section in template.sections():
			if section.lower() in {"companies", "doc_types"}:
				continue
			if not cp.has_section(section):
				cp.add_section(section)
			for key, value in template.items(section):
				cp.set(section, key, value)
	user_path = _settings_path()
	if user_path.exists():
		try:
			cp.read(user_path, encoding="utf-8")
		except configparser.Error as exc:
			logging.getLogger(__name__).warning("Neplatný uživatelský settings.ini (%s). Soubor bude přejmenován a znovu vytvořen.", exc)
			backup_suffix = datetime.now().strftime('%Y%m%d-%H%M%S')
			backup_path = user_path.with_suffix(user_path.suffix + f".invalid-{backup_suffix}")
			try:
				user_path.replace(backup_path)
			except OSError:
				try:
					user_path.unlink(missing_ok=True)
				except OSError:
					pass
	return cp



def write_settings(cp: configparser.ConfigParser) -> None:
	"""Write user settings to the persistent INI file."""
	path = _settings_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		cp.write(f)
	invalidate_config_cache()


def get_companies_from_settings() -> List[Tuple[str, str]]:
	"""Return list of (code, name) from [companies] section."""
	cp = read_settings()
	if not cp.has_section("companies"):
		return []
	items = list(cp.items("companies"))  # (code, name)
	# Ensure stable order by name then code
	items.sort(key=lambda kv: (kv[1].lower(), kv[0].lower()))
	return items


def add_company_to_settings(code: str, name: str) -> None:
	"""Insert/update company code->name in [companies]."""
	cp = read_settings()
	if not cp.has_section("companies"):
		cp.add_section("companies")
	cp.set("companies", code.strip(), name.strip())
	write_settings(cp)


def _doc_types_key(company_code: str, direction: str) -> str:
	return f"{company_code.strip()}|{direction.strip()}"


def get_doc_types(company_code: str, direction: str) -> List[Dict[str, str]]:
	"""Return list of {name, code} dicts for given (company, direction) from [doc_types]."""
	cp = read_settings()
	if not cp.has_section("doc_types"):
		return []
	key = _doc_types_key(company_code, direction)
	value = cp.get("doc_types", key, fallback="[]")
	try:
		arr = json.loads(value)
		# Normalize items
		res: List[Dict[str, str]] = []
		for it in arr:
			if not isinstance(it, dict):
				continue
			name = str(it.get("name") or it.get("nazev") or "").strip()
			code = str(it.get("code") or it.get("kod") or "").strip()
			if name and code:
				res.append({"name": name, "code": code})
		return res
	except Exception:
		return []


def add_doc_type(company_code: str, direction: str, name: str, code: str) -> None:
	"""Append/update a doc type entry to [doc_types] JSON list for key company|direction."""
	cp = read_settings()
	if not cp.has_section("doc_types"):
		cp.add_section("doc_types")
	key = _doc_types_key(company_code, direction)
	current = get_doc_types(company_code, direction)
	# Deduplicate by code
	filtered = [it for it in current if it.get("code") != code]
	filtered.append({"name": name.strip(), "code": code.strip()})
	cp.set("doc_types", key, json.dumps(filtered, ensure_ascii=False))
	write_settings(cp)



def _load_config_uncached() -> AppConfig:
	# Load from current working directory first
	load_dotenv()
	# Also try to load from the package directory (invoice_extractor/.env)
	package_env_path = Path(__file__).resolve().parent / ".env"
	if package_env_path.exists():
		load_dotenv(dotenv_path=package_env_path, override=False)

	# Read persistent settings from INI if present
	cp = read_settings()

	# Extractor settings with INI overrides
	api_key = (
		(cp.get("extractor", "api_key", fallback=None) if cp.has_section("extractor") else None)
		or os.getenv("OPENAI_API_KEY")
	)
	model = (
		(cp.get("extractor", "model", fallback=None) if cp.has_section("extractor") else None)
		or os.getenv("OPENAI_MODEL")
		or DEFAULT_OPENAI_MODEL
	)
	temperature = float(
		(cp.get("extractor", "temperature", fallback="0.0") if cp.has_section("extractor") else "0.0")
		or os.getenv("OPENAI_TEMPERATURE", "0.0")
	)
	top_p = float(
		(cp.get("extractor", "top_p", fallback="0.15") if cp.has_section("extractor") else "0.15")
		or os.getenv("OPENAI_TOP_P", "0.15")
	)
	reasoning_effort = (
		(cp.get("extractor", "reasoning_effort", fallback="medium") if cp.has_section("extractor") else "medium")
		or os.getenv("OPENAI_REASONING_EFFORT", "medium")
	)
	poppler = (
		(cp.get("extractor", "poppler_path", fallback=None) if cp.has_section("extractor") else None)
		or os.getenv("POPPLER_PATH")
	)
	concurrency = int(
		(cp.get("extractor", "concurrency", fallback="2") if cp.has_section("extractor") else "2")
		or os.getenv("CONCURRENCY", "2")
	)
	dpi = int(
		(cp.get("extractor", "pdf_dpi", fallback="200") if cp.has_section("extractor") else "200")
		or os.getenv("PDF_DPI", "200")
	)
	max_pages = int(
		(cp.get("extractor", "max_pages", fallback="2") if cp.has_section("extractor") else "2")
		or os.getenv("MAX_PAGES", "2")
	)
	max_tokens = int(
		(cp.get("extractor", "max_tokens", fallback="1024") if cp.has_section("extractor") else "1024")
		or os.getenv("MAX_TOKENS", "1024")
	)
	# Image optimization
	image_max_width = int(
		(cp.get("extractor", "image_max_width", fallback="1600") if cp.has_section("extractor") else "1600")
		or os.getenv("IMAGE_MAX_WIDTH", "1600")
	)
	image_jpeg_quality = int(
		(cp.get("extractor", "image_jpeg_quality", fallback="85") if cp.has_section("extractor") else "85")
		or os.getenv("IMAGE_JPEG_QUALITY", "85")
	)
	
	# Rate limiting and retry settings
	openai_max_retries = int(
		(cp.get("extractor", "max_retries", fallback="5") if cp.has_section("extractor") else "5")
		or os.getenv("OPENAI_MAX_RETRIES", "5")
	)
	openai_retry_delay = float(
		(cp.get("extractor", "retry_delay", fallback="1.0") if cp.has_section("extractor") else "1.0")
		or os.getenv("OPENAI_RETRY_DELAY", "1.0")
	)
	openai_max_delay = float(os.getenv("OPENAI_MAX_DELAY", "60.0"))
	openai_backoff_factor = float(os.getenv("OPENAI_BACKOFF_FACTOR", "2.0"))
	openai_request_delay = float(
		(cp.get("extractor", "request_delay", fallback="0.5") if cp.has_section("extractor") else "0.5")
		or os.getenv("OPENAI_REQUEST_DELAY", "0.5")
	)
	openai_timeout_s = int(
		(cp.get("extractor", "timeout_s", fallback="") if cp.has_section("extractor") else "")
		or os.getenv("OPENAI_TIMEOUT_S", "90")
	)
	openai_connection_timeout_s = int(
		(cp.get("extractor", "connection_timeout_s", fallback="") if cp.has_section("extractor") else "")
		or os.getenv("OPENAI_CONNECTION_TIMEOUT_S", "10")
	)

	# ABRA Flexi
	abra_server = (
		(cp.get("abra", "server", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_SERVER")
	)
	abra_port_env = (
		(cp.get("abra", "port", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_PORT")
	)
	abra_port = int(abra_port_env) if abra_port_env else None
	abra_company = (
		(cp.get("abra", "company", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_COMPANY")
	)
	abra_username = (
		(cp.get("abra", "username", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_USERNAME")
	)
	abra_password = (
		(cp.get("abra", "password", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_PASSWORD")
	)
	abra_timeout_s = int(os.getenv("ABRA_TIMEOUT_S", "10"))
	# Optional mapping for series/types, JSON string like {"default":"PRIJATA"}
	abra_series_map = os.getenv("ABRA_SERIES_MAP")
	abra_doc_endpoint = (
		(cp.get("abra", "doc_endpoint", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_DOC_ENDPOINT")
		or "faktura-prijata"
	)
	abra_doc_type_code = (
		(cp.get("abra", "doc_type_code", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_DOC_TYPE_CODE")
	)
	abra_partner_rel_code = os.getenv("ABRA_PARTNER_REL_CODE")
	abra_use_kod = (
		(cp.get("abra", "use_kod", fallback="true").lower() == "true" if cp.has_section("abra") else True)
		if os.getenv("ABRA_USE_KOD") is None
		else os.getenv("ABRA_USE_KOD", "true").lower() == "true"
	)
	abra_duplicate_kod_strategy = (
		(cp.get("abra", "duplicate_kod_strategy", fallback=None) if cp.has_section("abra") else None)
		or os.getenv("ABRA_DUPLICATE_KOD_STRATEGY")
		or "safe_update"
	).strip().lower()
	if abra_duplicate_kod_strategy not in {"safe_update", "skip"}:
		abra_duplicate_kod_strategy = "safe_update"
	abra_verify_tls = True
	if cp.has_section("abra"):
		abra_verify_tls = cp.get("abra", "verify_tls", fallback="true").lower() == "true"
	else:
		# allow env override
		abra_verify_tls = os.getenv("ABRA_VERIFY_TLS", "true").lower() == "true"
	
	# Extraction options
	use_doc_number_as_variable_symbol = (
		cp.get("extraction", "use_doc_number_as_variable_symbol", fallback="false").lower() == "true"
		if cp.has_section("extraction") else False
	)
	infer_missing_dates = False
	if cp.has_section("extraction"):
		if cp.has_option("extraction", "infer_missing_dates"):
			infer_missing_dates = cp.get("extraction", "infer_missing_dates", fallback="false").lower() == "true"
		else:
			infer_missing_dates = cp.get("extraction", "use_issue_date_as_due_date", fallback="false").lower() == "true"
	enable_multi_invoice_segmentation = (
		cp.get("extraction", "enable_multi_invoice_segmentation", fallback="false").lower() == "true"
		if cp.has_section("extraction") else False
	)
	date_order_raw = "dd-mm"
	if cp.has_section("extraction"):
		date_order_raw = cp.get("extraction", "date_order", fallback=date_order_raw)
	env_date_order = os.getenv("EXTRACTION_DATE_ORDER")
	if env_date_order:
		date_order_raw = env_date_order
	date_day_first = _parse_date_order(date_order_raw)
	# CSV options
	csv_enable_llm_mapping = (
		cp.get("csv", "enable_llm_mapping", fallback="false").lower() == "true"
		if cp.has_section("csv") else (os.getenv("CSV_ENABLE_LLM_MAPPING", "false").lower() == "true")
	)
	
	config = AppConfig(
		openai_api_key=api_key,
		openai_model=model,
		openai_temperature=temperature,
		openai_top_p=top_p,
		openai_reasoning_effort=reasoning_effort,
		poppler_path=poppler,
		concurrency=concurrency,
		dpi=dpi,
		max_pages=max_pages,
		max_tokens=max_tokens,
		image_max_width=image_max_width,
		image_jpeg_quality=image_jpeg_quality,
		openai_max_retries=openai_max_retries,
		openai_retry_delay=openai_retry_delay,
		openai_max_delay=openai_max_delay,
		openai_backoff_factor=openai_backoff_factor,
		openai_request_delay=openai_request_delay,
		openai_timeout_s=openai_timeout_s,
		openai_connection_timeout_s=openai_connection_timeout_s,
		abra_server=abra_server,
		abra_port=abra_port,
		abra_company=abra_company,
		abra_username=abra_username,
		abra_password=abra_password,
		abra_timeout_s=abra_timeout_s,
		abra_series_map=abra_series_map,
		abra_doc_endpoint=abra_doc_endpoint,
		abra_doc_type_code=abra_doc_type_code,
		abra_partner_rel_code=abra_partner_rel_code,
		abra_verify_tls=abra_verify_tls,
		abra_use_kod=abra_use_kod,
		abra_duplicate_kod_strategy=abra_duplicate_kod_strategy,
		use_doc_number_as_variable_symbol=use_doc_number_as_variable_symbol,
		infer_missing_dates=infer_missing_dates,
		enable_multi_invoice_segmentation=enable_multi_invoice_segmentation,
		date_day_first=date_day_first,
		csv_enable_llm_mapping=csv_enable_llm_mapping,
	)
	setattr(config, "use_issue_date_as_due_date", config.infer_missing_dates)
	return config


def load_config(force_reload: bool = False) -> AppConfig:
	"""Cache-aware wrapper around _load_config_uncached."""
	global _CONFIG_CACHE, _CONFIG_CACHE_STATE
	if not force_reload and _CONFIG_CACHE is not None and _CONFIG_CACHE_STATE is not None:
		current_state = _snapshot_config_state()
		if current_state == _CONFIG_CACHE_STATE:
			return _CONFIG_CACHE
	config = _load_config_uncached()
	_CONFIG_CACHE = config
	_CONFIG_CACHE_STATE = _snapshot_config_state()
	return config
