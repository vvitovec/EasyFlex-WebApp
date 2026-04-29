"""
Modul pro zjednodušené zpracování faktur.

Požadavek: program nemá pracovat s jednotlivými položkami/položkami faktury,
ale pouze s celkovými částkami podle sazeb DPH (0 %, 12 %, 21 %), tj. s poli,
která jsou viditelná v GUI. Neprovádí se žádné dopočty ani dorovnání.
"""
import logging
from typing import List

from .models import InvoiceData, VATSummary, VATRate

logger = logging.getLogger(__name__)


def process_invoice_data(invoice_data: InvoiceData) -> InvoiceData:
    """
    Zpracování bez položek: respektujeme pouze již vyextrahované celkové částky
    podle sazeb DPH (0/12/21) a z nich vytvoříme souhrny_dph. Nebudeme dělat žádné
    výpočty ani dorovnání. Položky se ignorují a nastavují na None.
    """
    logger.info("Zpracovávám fakturu (zjednodušeně, bez položek): %s", invoice_data.cislo_dokladu)

    updated_invoice = invoice_data.model_copy()
    updated_invoice.položky = None
    updated_invoice.souhrny_dph = _build_vat_summaries_from_totals(updated_invoice)
    return updated_invoice


def _build_vat_summaries_from_totals(inv: InvoiceData) -> List[VATSummary]:
    """Sestaví souhrny DPH pouze z horních (GUI) polí faktury.

    Nevykonává žádné výpočty, pouze přebírá opsané hodnoty:
    - zaklad_dane_{0,12,21}
    - vyse_dph_{12,21}
    Pro 0 % DPH je vyse_dph vždy 0.0. celkova_cena v souhrnu ponecháme 0.0.
    """
    summaries: List[VATSummary] = []

    try:
        if inv.zaklad_dane_21 is not None:
            summaries.append(VATSummary(
                sazba_dph=VATRate.HIGH,
                zaklad_dane=float(inv.zaklad_dane_21 or 0.0),
                vyse_dph=float(inv.vyse_dph_21 or 0.0),
                celkova_cena=0.0,
                pocet_polozek=0,
            ))
        if inv.zaklad_dane_12 is not None:
            summaries.append(VATSummary(
                sazba_dph=VATRate.LOW,
                zaklad_dane=float(inv.zaklad_dane_12 or 0.0),
                vyse_dph=float(inv.vyse_dph_12 or 0.0),
                celkova_cena=0.0,
                pocet_polozek=0,
            ))
        if inv.zaklad_dane_0 is not None:
            summaries.append(VATSummary(
                sazba_dph=VATRate.ZERO,
                zaklad_dane=float(inv.zaklad_dane_0 or 0.0),
                vyse_dph=0.0,
                celkova_cena=0.0,
                pocet_polozek=0,
            ))
    except Exception:
        # Obranně vrať prázdné souhrny, pokud by se cokoliv pokazilo
        return []

    return summaries


def should_use_items_logic(invoice_data: InvoiceData) -> bool:
    """Nově vždy vrací False: položky se již nepoužívají."""
    return False
