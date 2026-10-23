import asyncio
from types import SimpleNamespace
import pytest

from EasyFlex.pdf_text import parse_prestashop_text
from EasyFlex.extractor import InvoiceExtractor, SegmentInfo
from EasyFlex.abra import _build_invoice_payload, validate_import_context
from webapp.abra_context import apply_context_to_config, persist_context
from test_extractor_segmentation import _make_config
from test_batch_jobs import _load_app_module_with_db, _create_user


def _page(number='IN0001/2026', summary=True, header=True, net='7 975,32', tax='973,68', total='8 949,00'):
    lines = ['FAKTURA 2026-09-12 #' + number]
    if header:
        lines += ['Doručovací adresa'.ljust(35) + 'Fakturační adresa']
        lines += [left.ljust(35) + right for left, right in [
            ('Kontakt', 'Odběratel'), ('Ulice 1', 'Jiná 2'), ('11000 Praha', '370 01 České Budějovice'),
            ('Česká republika', 'Česká republika'), ('777123456', '777123456')]]
        lines += ['Číslo faktury    Datum vystavení', '#' + number + ' 2026-09-12']
    if summary:
        lines += ['Detaily DPH', 'Produkty 12.000 % 7 790,19 Kč 934,82 Kč',
                  'Doručení 21.000 % 185,13 Kč 38,86 Kč',
                  'Celkem (bez DPH) ' + net + ' Kč', 'Celkem DPH ' + tax + ' Kč', 'Celkem ' + total + ' Kč']
    return '\n'.join(lines + ['Obchod.cz', 'Litvínovická 1478', '370 01 České Budějovice', 'IČ: 04868307', 'DIČ: CZ04868307'])


def test_text_reader_keeps_thousands_and_merges_continuation_without_phone_ico():
    invoices = parse_prestashop_text(_page(summary=False) + '\f' + _page(header=False))
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice['source_pages'] == [1, 2]
    assert invoice['celkova_cena'] == 8949
    assert invoice['zaklad_dane_12'] == 7790.19
    assert invoice['odberatel_ic'] is None
    assert invoice['odberatel_dic'] is None
    assert invoice['odberatel_adresa'] == 'Jiná 2, 370 01 České Budějovice'
    assert invoice['dodavatel_ic'] == '04868307'


def test_text_reader_preserves_zero_and_flags_source_contradiction():
    text = _page().replace('7 790,19', '0,00').replace('934,82', '0,00').replace('185,13', '0,00').replace('38,86', '0,00')
    zero = parse_prestashop_text(text.replace('7 975,32', '0,00').replace('973,68', '0,00').replace('8 949,00', '0,00'))[0]
    assert zero['celkova_cena'] == 0
    assert not zero.get('validation_errors')
    bad = parse_prestashop_text(_page(net='1,00', tax='1,00', total='2,00'))[0]
    assert bad['validation_errors']
    with pytest.raises(ValueError, match='souhrnem'):
        _build_invoice_payload(bad, _make_config(), None, 'faktura-vydana')


def test_text_reader_rejects_unknown_layout_and_nonadjacent_duplicate():
    assert parse_prestashop_text('a random document') is None
    assert parse_prestashop_text(_page() + '\f' + _page('IN0002/2026') + '\f' + _page()) is None


def test_text_reader_is_used_before_vision_and_streams_once(monkeypatch):
    extractor = InvoiceExtractor(config=_make_config())
    invoice = parse_prestashop_text(_page())[0]
    monkeypatch.setattr('EasyFlex.pdf_text.extract_text_invoices', lambda *a, **k: [invoice])
    async def fail(*a, **k):
        pytest.fail('Text-readable PDF must not make vision calls')
    monkeypatch.setattr(extractor, '_pdf_to_images', fail)
    streamed = []
    results = asyncio.run(extractor.extract_auto('sample.pdf', on_result=streamed.append))
    assert len(results) == len(streamed) == 1
    assert results[0].data.celkova_cena == 8949


def test_adjacent_single_segments_same_number_are_one_invoice():
    extractor = InvoiceExtractor(config=_make_config())
    segments = [SegmentInfo(i, True, 'single', key, .95) for i, key in enumerate(['#IN0001/2026', 'IN0001/2026', 'IN0002/2026'])]
    assert extractor._build_groups_from_segments(segments) == [(0, 1, '#IN0001/2026'), (2, 2, 'IN0002/2026')]


def test_unknown_amount_stays_unknown_not_zero():
    extractor = InvoiceExtractor(config=_make_config())
    data = extractor._postprocess_payload({'zaklad_dane_12': None, 'zaklad_dane_21': 0})
    assert data['zaklad_dane_12'] is None
    assert data['zaklad_dane_21'] == 0


def test_empty_job_type_does_not_inherit_another_company():
    cfg = _make_config(abra_doc_type_code='telco')
    apply_context_to_config(cfg, 'another', 'faktura-vydana', None)
    assert cfg.abra_doc_type_code is None


def test_preflight_rejects_invalid_type_without_sending_documents(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append(method)
        return SimpleNamespace(status_code=200, json=lambda: {'winstrom': {'typ-faktury-vydane': [{'kod': 'ZASAĎ-TO.CZ'}]}})
    monkeypatch.setattr('EasyFlex.abra._request_with_retry', request)
    with pytest.raises(ValueError, match='telco'):
        validate_import_context(_make_config(abra_doc_type_code='telco', abra_doc_endpoint='faktura-vydana'))
    assert calls == ['GET']


def test_verified_totals_preserve_tax_and_authorised_rounding():
    invoice = parse_prestashop_text(_page())[0]
    body = _build_invoice_payload(invoice, _make_config(), None, 'faktura-vydana')['winstrom']['faktura-vydana'][0]
    assert body['bezPolozek'] is True
    assert body['sumDphSniz'] == 934.82
    assert body['sumCelkem'] == 8949
    assert 'polozkyDokladu' not in body
    invoice['celkova_cena'] += .01
    with pytest.raises(ValueError, match='zaokrouhlení'):
        _build_invoice_payload(invoice, _make_config(), None, 'faktura-vydana')
    invoice['source_rounding_authorized'] = True
    body = _build_invoice_payload(invoice, _make_config(), None, 'faktura-vydana')['winstrom']['faktura-vydana'][0]
    assert body['polozkyDokladu'][-1]['sumZkl'] == .01
    assert body['polozkyDokladu'][-1]['sumDph'] == 0


def test_failed_import_can_be_selected_but_extraction_failure_and_merged_cannot(monkeypatch, tmp_path):
    module = _load_app_module_with_db(monkeypatch, tmp_path)
    app = module.create_app()
    app.config['TESTING'] = True
    user_id = _create_user(module, app)
    monkeypatch.setattr(module, 'ensure_seed_data', lambda *a, **k: None)
    from webapp.invoice_batches import count_selected_rows, rows_for_display
    with app.app_context():
        batch = module.InvoiceBatch(user_id=user_id, processing_status='completed_with_errors')
        module.db.session.add(batch)
        module.db.session.flush()
        ids = []
        for i, (error, last_error, status) in enumerate([('old import error', 'old import error', 'failed'), ('Unreadable PDF', None, 'failed'), (None, None, 'merged')]):
            row = module.InvoiceRow(batch_id=batch.id, row_index=i, invoice_data={'cislo_dokladu': str(i)}, error=error, last_import_error=last_error, import_status=status, marked_for_import=False)
            module.db.session.add(row)
            module.db.session.flush()
            ids.append(row.id)
        settings = module.db.session.query(module.UserSettings).filter_by(user_id=user_id).one()
        settings.config_overrides = {'abra_doc_type_code': 'telco'}
        persist_context(settings, 'another', 'faktura-vydana', None)
        assert 'abra_doc_type_code' not in settings.config_overrides
        batch_id = batch.id
        module.db.session.commit()
    client = app.test_client()
    client.post('/login', data={'username':'admin','password':'secret'})
    response = client.post(f'/results/{batch_id}/selection/bulk', json={'scope':'filtered','marked':True})
    assert response.status_code == 200
    assert response.get_json()['updated'] == 1
    assert response.get_json()['selected_count'] == 1
    with app.app_context():
        batch = module.db.session.get(module.InvoiceBatch, batch_id)
        assert count_selected_rows(batch_id) == 1
        assert rows_for_display(batch)[0]['selectable'] is True
        rows, total = module.query_rows_for_batch(batch)
        assert total == 2


def test_specific_abra_error_is_retained_without_becoming_extraction_error():
    from webapp.worker import _resolve_import_outcome
    status, error, _ = _resolve_import_outcome({'__status':'failed','error':'ABRA: invalid typDokl'})
    assert status == 'failed'
    assert error == 'ABRA: invalid typDokl'


@pytest.mark.parametrize('endpoint,primary,other', [('faktura-vydana','odberatel','dodavatel'), ('faktura-prijata','dodavatel','odberatel')])
def test_partner_identifiers_never_fall_back_to_other_party(endpoint, primary, other):
    from EasyFlex.abra import _prepare_partner_section
    invoice = {primary + '_jmeno':'Customer', other + '_ic':'04868307', other + '_dic':'CZ04868307'}
    body = _prepare_partner_section(invoice, None, endpoint)
    assert body['nazFirmy'] == 'Customer'
    assert 'ic' not in body
    assert 'dic' not in body
