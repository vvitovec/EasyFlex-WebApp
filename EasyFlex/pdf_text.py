"""Deterministic reader for the Czech PrestaShop invoice layout.

Only use this path when every page has the expected header/footer and every
invoice has a complete tax summary. Unknown layouts fall back to vision.
No amount is inferred from a product price or a payment-method amount.
"""
from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal
from pathlib import Path
import re
import subprocess

MONEY = r'(-?\s*\d[\d \u00a0]*,\d{2})\s*(Kč|€)'


def _amount(value: str) -> Decimal:
    return Decimal(re.sub(r'\s', '', value).replace(',', '.'))


def _label_amount(text: str, label: str):
    match = re.search(label + r'\s+' + MONEY + r'\s*$', text, re.MULTILINE)
    return (_amount(match[1]), match[2]) if match else None


def _billing_address(page: str) -> dict:
    lines = page.splitlines()
    header = next((i for i, line in enumerate(lines) if 'Fakturační adresa' in line), None)
    if header is None:
        raise ValueError('Chybí fakturační adresa.')
    column = lines[header].index('Fakturační adresa')
    block = []
    for line in lines[header + 1:]:
        if 'Číslo faktury' in line:
            break
        value = line[column:].strip()
        if value:
            block.append(value)
    if len(block) < 3:
        raise ValueError('Neúplná fakturační adresa.')
    postal_index = next((i for i, line in enumerate(block) if re.match(r'^\d{3}\s?\d{2}\s+\D', line)), None)
    if postal_index is None or postal_index < 2:
        raise ValueError('Neznámý formát PSČ/adresy.')
    state = block[postal_index + 1] if postal_index + 1 < len(block) else None
    before_postal = block[:postal_index]
    # Street/house number may occupy separate lines. Identity lines precede it.
    street_parts = [before_postal.pop()]
    if re.fullmatch(r'\d+[\w/-]*', street_parts[0]) and len(before_postal) > 1:
        street_parts.insert(0, before_postal.pop())
    identities = before_postal
    if not identities:
        raise ValueError('Chybí jméno odběratele.')
    name_parts, dic, ic = [], None, None
    for value in identities:
        compact = re.sub(r'\s', '', value)
        if re.fullmatch(r'(?:CZ|SK)\d{8,10}', compact):
            dic = compact
        elif re.fullmatch(r'\d{8,10}', compact):
            # This template prints DIČ in the address and in a labelled table.
            # A bare number is not evidence of IČ.
            dic = compact
        else:
            name_parts.append(value)
    if not name_parts:
        raise ValueError('Chybí název odběratele.')
    # Preserve the company name and contact person, rather than silently choosing
    # the contact as the legal entity. No phone is considered an identifier.
    name = ', '.join(name_parts)
    return dict(odberatel_jmeno=name, odberatel_adresa=', '.join([' '.join(street_parts), block[postal_index]]),
                odberatel_stat=state, odberatel_ic=ic, odberatel_dic=dic)


def parse_prestashop_text(text: str, *, infer_dates: bool = False, doc_number_as_vs: bool = False) -> list[dict] | None:
    # Some PDFs wrap the final č onto the next line in a narrow table cell.
    text = re.sub(r'(\d,\d{2}) K(?=\s|$)', r'\1 Kč', text)
    pages = [p for p in text.split('\f') if p.strip()]
    if not pages:
        return None
    grouped = OrderedDict()
    for page_no, page in enumerate(pages, 1):
        key = re.search(r'#(IN\d+/\d{4})\b', page)
        ic = re.search(r'\bIČ:\s*(\d{8})\b', page)
        dic = re.search(r'\bDIČ:\s*([A-Z]{2}\d+)\b', page)
        if not key or not ic or not dic or 'FAKTURA' not in page:
            return None
        group = grouped.setdefault(key[1], [])
        # Do not combine non-adjacent occurrences of the same number.
        if group and group[-1][0] != page_no - 1:
            return None
        group.append((page_no, page, ic[1], dic[1]))
    invoices = []
    try:
        for number, group in grouped.items():
            first = group[0][1]
            if 'Doručovací adresa' not in first or 'Fakturační adresa' not in first:
                return None
            combined = '\n'.join(p[1] for p in group)
            if len({(p[2], p[3]) for p in group}) != 1:
                return None
            total = _label_amount(combined, r'\bCelkem')
            net = _label_amount(combined, r'Celkem \(bez DPH\)')
            if total is None or net is None:
                return None
            currency = 'EUR' if total[1] == '€' else 'CZK'
            if net[1] != total[1]:
                return None
            taxes = {0: [Decimal(0), Decimal(0)], 12: [Decimal(0), Decimal(0)], 21: [Decimal(0), Decimal(0)]}
            matched = 0
            for line in combined.splitlines():
                match = re.search(r'(?:Produkty|Doručení)\s+(\d+)\.\d+\s*%', line)
                if not match:
                    continue
                rate = int(match[1])
                values = re.findall(MONEY, line[match.end():])
                if rate not in taxes or len(values) < 2 or any(v[1] != total[1] for v in values[:2]):
                    return None
                taxes[rate][0] += _amount(values[0][0])
                taxes[rate][1] += _amount(values[1][0])
                matched += 1
            if not matched:
                return None
            vat = _label_amount(combined, r'Celkem DPH')
            vat_total = vat[0] if vat else sum(t[1] for t in taxes.values())
            # A source can differ by a cent due to displayed rounding. Preserve
            # all printed amounts and surface that difference in the warning.
            delta_net = net[0] - sum(t[0] for t in taxes.values())
            delta_vat = vat_total - sum(t[1] for t in taxes.values())
            invalid_summary = max(abs(delta_net), abs(delta_vat), abs(net[0] + vat_total - total[0])) > Decimal('.02')
            date = re.search(r'\b\d{4}-\d{2}-\d{2}\b', first)
            if not date:
                return None
            footer = first.split('IČ:')[0].splitlines()
            nonempty = [line.strip() for line in footer if line.strip()]
            if len(nonempty) < 3:
                return None
            supplier = nonempty[-3:]
            if not re.match(r'^\d{3}\s?\d{2}\s+', supplier[-1]):
                return None
            inv = dict(document_type='invoice', cislo_dokladu=number,
                       variabilni_symbol=number if doc_number_as_vs else None,
                       datum_vystaveni=date[0], datum_duzp=date[0] if infer_dates else None,
                       datum_splatnosti=date[0] if infer_dates else None,
                       dodavatel_jmeno=supplier[0], dodavatel_adresa=', '.join(supplier[1:]),
                       dodavatel_ic=group[0][2], dodavatel_dic=group[0][3], dodavatel_stat='CZ',
                       mena=currency, zaklad_dane=float(net[0]), vyse_dph=float(vat_total), celkova_cena=float(total[0]),
                       source_pages=[p[0] for p in group], extraction_method='pdf_text_prestashop',
                       source_amounts_verified=True)
            inv.update(_billing_address(first))
            if invalid_summary:
                inv['validation_errors'] = ['Rozpis sazeb DPH nesouhlasí se závěrečným souhrnem původního PDF.']
            for rate in [0, 12, 21]:
                inv['zaklad_dane_' + str(rate)] = float(taxes[rate][0])
                if rate:
                    inv['vyse_dph_' + str(rate)] = float(taxes[rate][1])
            warnings = []
            if infer_dates:
                warnings.append('DUZP a splatnost nejsou uvedeny; doplněny podle data vystavení.')
            if delta_net or delta_vat:
                warnings.append('Tištěný souhrn a rozpis DPH se liší o zaokrouhlení; zachovány hodnoty z PDF.')
            inv['upozorneni'] = ' | '.join(warnings) or None
            invoices.append(inv)
    except (ValueError, IndexError, ArithmeticError):
        return None
    return invoices


def extract_text_invoices(path: str, *, poppler_path=None, infer_dates=False, doc_number_as_vs=False):
    command = str(Path(poppler_path) / 'pdftotext') if poppler_path else 'pdftotext'
    try:
        result = subprocess.run([command, '-layout', str(path), '-'], capture_output=True, timeout=30, check=True)
        text = result.stdout.decode('utf-8')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return parse_prestashop_text(text, infer_dates=infer_dates, doc_number_as_vs=doc_number_as_vs)
