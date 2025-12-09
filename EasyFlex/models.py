
from typing import Optional, List, Any, get_origin, get_args, Union
from enum import Enum

ConfigDict = None  # type: ignore[assignment]

try:
	from pydantic import BaseModel  # type: ignore
	try:
		from pydantic import ConfigDict as _PydanticConfigDict  # type: ignore
	except ImportError:
		ConfigDict = None  # type: ignore
	else:
		ConfigDict = _PydanticConfigDict  # type: ignore
except ImportError:  # Lightweight fallback without runtime dependency
	class BaseModel:
		"""Minimal subset of Pydantic BaseModel used by the app."""

		__slots__ = ("__dict__",)

		def __init__(self, **data: Any) -> None:
			annotations = getattr(self, "__annotations__", {})
			for name, anno in annotations.items():
				if name in data:
					raw_value = data[name]
				else:
					raw_value = self._clone_default(getattr(self.__class__, name, None))
				setattr(self, name, self._convert_field(raw_value, anno))
			for key, value in data.items():
				if key not in annotations:
					setattr(self, key, value)

		@staticmethod
		def _clone_default(value: Any) -> Any:
			if isinstance(value, (list, dict, set)):
				return value.copy()
			return value

		@classmethod
		def _convert_field(cls, value: Any, annotation: Any) -> Any:
			if value is None:
				return None
			origin = get_origin(annotation)
			if origin in (list, List):
				(inner_type,) = get_args(annotation) or (Any,)
				return [cls._convert_nested(item, inner_type) for item in list(value)]
			if origin is Union:
				for arg in get_args(annotation):
					if arg is type(None):  # noqa: E721
						continue
					return cls._convert_field(value, arg)
			return cls._convert_nested(value, annotation)

		@classmethod
		def _convert_nested(cls, value: Any, annotation: Any) -> Any:
			try:
				if isinstance(annotation, type) and issubclass(annotation, BaseModel):
					return annotation.model_validate(value)
			except Exception:
				pass
			try:
				if isinstance(annotation, type) and issubclass(annotation, Enum) and not isinstance(value, annotation):
					return annotation(value)
			except Exception:
				pass
			return value

		@classmethod
		def _dump_value(cls, value: Any) -> Any:
			if isinstance(value, BaseModel):
				return value.model_dump()
			if isinstance(value, Enum):
				return value.value
			if isinstance(value, list):
				return [cls._dump_value(item) for item in value]
			return value

		def model_dump(self) -> dict:
			result: dict = {}
			for name in getattr(self, "__annotations__", {}):
				result[name] = self._dump_value(getattr(self, name, None))
			return result

		@classmethod
		def model_validate(cls, data: Any) -> "BaseModel":
			if isinstance(data, cls):
				return data
			if not isinstance(data, dict):
				raise TypeError(f"Expected dict for {cls.__name__}.model_validate, got {type(data).__name__}")
			return cls(**data)

		def model_copy(self) -> "BaseModel":
			return self.__class__.model_validate(self.model_dump())


class VATRate(int, Enum):
	"""Sazby DPH podle instrukcí"""
	ZERO = 0
	LOW = 12
	HIGH = 21


class InvoiceItem(BaseModel):
	"""Položka faktury"""
	nazev: Optional[str] = None
	mnozstvi: Optional[float] = 1.0
	cena_mj: Optional[float] = None  # Cena za měrnou jednotku
	celkova_cena: Optional[float] = None  # Celková cena řádku
	sazba_dph: Optional[VATRate] = None
	zaklad_dane: Optional[float] = None
	vyse_dph: Optional[float] = None
	mena: Optional[str] = None


class VATSummary(BaseModel):
	"""Souhrn po sazbě DPH"""
	sazba_dph: VATRate
	zaklad_dane: float = 0.0
	vyse_dph: float = 0.0
	celkova_cena: float = 0.0
	pocet_polozek: int = 0


class InvoiceData(BaseModel):
	if ConfigDict is not None:
		model_config = ConfigDict(extra="allow")  # type: ignore[misc]
	else:
		class Config:
			extra = "allow"

	# Původní pole pro zpětnou kompatibilitu
	cislo_dokladu: Optional[str] = None
	variabilni_symbol: Optional[str] = None
	dodavatel_jmeno: Optional[str] = None
	dodavatel_adresa: Optional[str] = None
	dodavatel_stat: Optional[str] = None
	dodavatel_ic: Optional[str] = None
	dodavatel_dic: Optional[str] = None
	odberatel_jmeno: Optional[str] = None
	odberatel_adresa: Optional[str] = None
	odberatel_stat: Optional[str] = None
	odberatel_ic: Optional[str] = None
	odberatel_dic: Optional[str] = None
	datum_vystaveni: Optional[str] = None
	datum_duzp: Optional[str] = None
	datum_splatnosti: Optional[str] = None
	zaklad_dane: Optional[float] = None
	vyse_dph: Optional[float] = None
	celkova_cena: Optional[float] = None
	# Rozšířená pole pro různé sazby DPH (z PDF/CSV)
	zaklad_dane_0: Optional[float] = None
	zaklad_dane_12: Optional[float] = None
	zaklad_dane_21: Optional[float] = None
	vyse_dph_12: Optional[float] = None
	vyse_dph_21: Optional[float] = None

	# Nová pole pro rozšířenou funkcionalnost
	položky: Optional[List[InvoiceItem]] = None
	souhrny_dph: Optional[List[VATSummary]] = None
	mena: Optional[str] = None
	upozorneni: Optional[str] = None
