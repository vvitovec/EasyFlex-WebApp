from copy import deepcopy
from zipfile import ZipFile

import pandas as pd

from EasyFlex.csv_processor import CSVProcessor
from EasyFlex.config import load_config
from EasyFlex.invoice_warnings import get_warning


def _sample_dataframe() -> pd.DataFrame:
	return pd.DataFrame(
		{
			"Číslo faktury": ["F-2024-002"],
			"Číslo Objednávky": ["PO-991"],
			"Datum vystavení": ["1.5.2024"],
			"Datum objednávky": ["1.5.2024"],
			"Datum splatnosti": ["15.5.2024"],
			"Jméno": ["Test s.r.o."],
			"Adresa": ["Hlavní 1"],
			"PSČ": ["11000"],
			"Město": ["Praha"],
			"Částka celkem bez DPH v sazbě 12%": ["100"],
			"Částka celkem bez DPH v sazbě 21%": ["200"],
		},
	)


def _assert_sample_invoice(invoice) -> None:
	assert invoice.cislo_dokladu == "F-2024-002"
	assert invoice.variabilni_symbol == "PO-991"
	assert invoice.datum_vystaveni == "2024-05-01"
	assert invoice.datum_splatnosti == "2024-05-15"
	assert invoice.odberatel_jmeno == "Test s.r.o."
	assert "11000" in (invoice.odberatel_adresa or "")
	assert invoice.zaklad_dane_12 == 100.0
	assert invoice.zaklad_dane_21 == 200.0


def test_csv_row_mapping_infers_due_date_and_warnings() -> None:
	processor = CSVProcessor(config=_config_with(infer_missing_dates=True))
	df = pd.DataFrame(
		{
			"Číslo faktury": ["F-2024-001"],
			"Číslo Objednávky": ["PO-1"],
			"Datum vystavení": ["1.5.2024"],
			"Datum objednávky": [""],
			"Datum splatnosti": [""],
			"Jméno": ["ACME"],
			"Adresa": ["Testovací 123"],
			"PSČ": ["11000"],
			"Město": ["Praha"],
			"Částka celkem bez DPH v sazbě 12%": ["1000"],
			"Částka celkem bez DPH v sazbě 21%": ["2000"],
		}
	)
	processor._column_map = processor._infer_column_map(df)
	invoice = processor._map_row_to_invoice_data(df.iloc[0], 1)
	assert invoice is not None
	assert invoice.datum_vystaveni == "2024-05-01"
	assert invoice.datum_splatnosti == "2024-05-01"
	assert invoice.zaklad_dane_12 == 1000.0
	assert invoice.zaklad_dane_21 == 2000.0
	warning_text = get_warning(invoice, "") or ""
	assert "splatnosti" in warning_text.lower()


def test_csv_row_mapping_skips_inference_when_disabled() -> None:
	processor = CSVProcessor(config=_config_with(infer_missing_dates=False))
	df = pd.DataFrame(
		{
			"Číslo faktury": ["F-2024-002"],
			"Číslo Objednávky": ["PO-2"],
			"Datum vystavení": ["1.5.2024"],
			"Datum objednávky": [""],
			"Datum splatnosti": [""],
			"Jméno": ["ACME"],
			"Adresa": ["Testovací 123"],
		}
	)
	processor._column_map = processor._infer_column_map(df)
	invoice = processor._map_row_to_invoice_data(df.iloc[0], 1)
	assert invoice is not None
	assert invoice.datum_vystaveni == "2024-05-01"
	assert invoice.datum_splatnosti is None
	assert get_warning(invoice) is None


def test_csv_processor_uses_doc_number_for_variable_symbol_when_enabled() -> None:
	processor = CSVProcessor(config=_config_with(use_doc_number_as_variable_symbol=True, infer_missing_dates=False))
	df = pd.DataFrame(
		{
			"Číslo faktury": ["INV-10"],
			"Číslo Objednávky": [""],
			"Datum vystavení": ["1.5.2024"],
			"Jméno": ["Test s.r.o."],
		}
	)
	processor._column_map = processor._infer_column_map(df)
	invoice = processor._map_row_to_invoice_data(df.iloc[0], 1)
	assert invoice is not None
	assert invoice.variabilni_symbol == "INV-10"


def test_csv_processor_respects_date_order_setting() -> None:
	processor = CSVProcessor(config=_config_with(date_day_first=False, infer_missing_dates=False))
	df = pd.DataFrame(
		{
			"Číslo faktury": ["A-1"],
			"Číslo Objednávky": ["PO-3"],
			"Datum vystavení": ["09/01/2024"],
			"Datum splatnosti": ["09/30/2024"],
			"Jméno": ["Test"],
		}
	)
	processor._column_map = processor._infer_column_map(df)
	invoice = processor._map_row_to_invoice_data(df.iloc[0], 1)
	assert invoice is not None
	assert invoice.datum_vystaveni == "2024-09-01"
	assert invoice.datum_splatnosti == "2024-09-30"


def test_process_table_file_reads_csv(tmp_path) -> None:
	processor = CSVProcessor()
	csv_path = tmp_path / "sample.csv"
	_sample_dataframe().to_csv(csv_path, index=False)
	invoices = processor.process_table_file(str(csv_path))
	assert len(invoices) == 1
	_assert_sample_invoice(invoices[0])


def test_process_table_file_reads_excel(tmp_path) -> None:
	processor = CSVProcessor()
	xlsx_path = tmp_path / "sample.xlsx"
	_write_sample_xlsx(xlsx_path)
	invoices = processor.process_table_file(str(xlsx_path))
	assert len(invoices) == 1
	_assert_sample_invoice(invoices[0])


def test_process_table_file_reads_xml(tmp_path) -> None:
	processor = CSVProcessor()
	xml_path = tmp_path / "sample.xml"
	xml_content = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Invoices>
	<Invoice>
		<Číslo_faktury>F-2024-002</Číslo_faktury>
		<Číslo_Objednávky>PO-991</Číslo_Objednávky>
		<Datum_vystavení>1.5.2024</Datum_vystavení>
		<Datum_objednávky>1.5.2024</Datum_objednávky>
		<Datum_splatnosti>15.5.2024</Datum_splatnosti>
		<Jméno>Test s.r.o.</Jméno>
		<Adresa>Hlavní 1</Adresa>
		<PSČ>11000</PSČ>
		<Město>Praha</Město>
		<Částka_celkem_bez_DPH_v_sazbě_12_percent>100</Částka_celkem_bez_DPH_v_sazbě_12_percent>
		<Částka_celkem_bez_DPH_v_sazbě_21_percent>200</Částka_celkem_bez_DPH_v_sazbě_21_percent>
	</Invoice>
</Invoices>
"""
	xml_path.write_text(xml_content, encoding="utf-8")
	invoices = processor.process_table_file(str(xml_path))
	assert len(invoices) == 1
	_assert_sample_invoice(invoices[0])


def test_column_mapping_handles_supplier_customer_synonyms() -> None:
	processor = CSVProcessor()
	df = pd.DataFrame(
		{
			"Invoice Number": ["INV-2024-10"],
			"PO Reference": ["PO-2024-01"],
			"Supplier Name": ["Dodavatel s.r.o."],
			"Supplier Street": ["Dodavatelská 1"],
			"Supplier Country": ["SK"],
			"Supplier Company ID": ["12345678"],
			"Supplier VAT": ["SK1234567890"],
			"Customer Name": ["Odběratel a.s."],
			"Customer Street": ["Odběratelská 9"],
			"Customer Country": ["CZ"],
			"Customer Company ID": ["87654321"],
			"Customer VAT": ["CZ87654321"],
			"Issue Date": ["2024-05-01"],
			"Tax Date": ["2024-05-02"],
			"Due Date": ["2024-05-20"],
			"Net 0%": ["0"],
			"Net 12%": ["100"],
			"Net 21%": ["200"],
			"VAT 12%": ["12"],
			"VAT 21%": ["42"],
			"Total Amount": ["354"],
			"Extra Notes": ["Ignore me"],
		},
	)
	processor._column_map = processor._infer_column_map(df)
	invoice = processor._map_row_to_invoice_data(df.iloc[0], 1)
	assert invoice is not None
	assert invoice.cislo_dokladu == "INV-2024-10"
	assert invoice.variabilni_symbol == "PO-2024-01"
	assert invoice.dodavatel_jmeno == "Dodavatel s.r.o."
	assert invoice.dodavatel_adresa == "Dodavatelská 1"
	assert invoice.dodavatel_stat == "SK"
	assert invoice.dodavatel_ic == "12345678"
	assert invoice.dodavatel_dic == "SK1234567890"
	assert invoice.odberatel_jmeno == "Odběratel a.s."
	assert "Odběratelská" in (invoice.odberatel_adresa or "")
	assert invoice.odberatel_stat == "CZ"
	assert invoice.odberatel_ic == "87654321"
	assert invoice.odberatel_dic == "CZ87654321"
	assert invoice.datum_vystaveni == "2024-05-01"
	assert invoice.datum_duzp == "2024-05-02"
	assert invoice.datum_splatnosti == "2024-05-20"
	assert invoice.zaklad_dane_0 == 0.0
	assert invoice.zaklad_dane_12 == 100.0
	assert invoice.zaklad_dane_21 == 200.0
	assert invoice.vyse_dph_12 == 12.0
	assert invoice.vyse_dph_21 == 42.0
	assert invoice.celkova_cena == 354.0


def _write_sample_xlsx(path) -> None:
	shared_strings = [
		"Číslo faktury",
		"Číslo Objednávky",
		"Datum vystavení",
		"Datum objednávky",
		"Datum splatnosti",
		"Jméno",
		"Adresa",
		"PSČ",
		"Město",
		"Částka celkem bez DPH v sazbě 12%",
		"Částka celkem bez DPH v sazbě 21%",
		"F-2024-002",
		"PO-991",
		"1.5.2024",
		"15.5.2024",
		"Test s.r.o.",
		"Hlavní 1",
		"11000",
		"Praha",
		"100",
		"200",
	]
	shared_xml_rows = "".join(
		f"<si><t>{value}</t></si>" for value in shared_strings
	)
	shared_xml = (
		"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
		"<sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
		"count=\"{count}\" uniqueCount=\"{count}\">{rows}</sst>"
	).format(count=len(shared_strings), rows=shared_xml_rows)

	sheet_xml = (
		"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
		"<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
		"xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
		"<sheetData>"
		"<row r=\"1\">"
		"<c r=\"A1\" t=\"s\"><v>0</v></c>"
		"<c r=\"B1\" t=\"s\"><v>1</v></c>"
		"<c r=\"C1\" t=\"s\"><v>2</v></c>"
		"<c r=\"D1\" t=\"s\"><v>3</v></c>"
		"<c r=\"E1\" t=\"s\"><v>4</v></c>"
		"<c r=\"F1\" t=\"s\"><v>5</v></c>"
		"<c r=\"G1\" t=\"s\"><v>6</v></c>"
		"<c r=\"H1\" t=\"s\"><v>7</v></c>"
		"<c r=\"I1\" t=\"s\"><v>8</v></c>"
		"<c r=\"J1\" t=\"s\"><v>9</v></c>"
		"<c r=\"K1\" t=\"s\"><v>10</v></c>"
		"</row>"
		"<row r=\"2\">"
		"<c r=\"A2\" t=\"s\"><v>11</v></c>"
		"<c r=\"B2\" t=\"s\"><v>12</v></c>"
		"<c r=\"C2\" t=\"s\"><v>13</v></c>"
		"<c r=\"D2\" t=\"s\"><v>13</v></c>"
		"<c r=\"E2\" t=\"s\"><v>14</v></c>"
		"<c r=\"F2\" t=\"s\"><v>15</v></c>"
		"<c r=\"G2\" t=\"s\"><v>16</v></c>"
		"<c r=\"H2\" t=\"s\"><v>17</v></c>"
		"<c r=\"I2\" t=\"s\"><v>18</v></c>"
		"<c r=\"J2\" t=\"s\"><v>19</v></c>"
		"<c r=\"K2\" t=\"s\"><v>20</v></c>"
		"</row>"
		"</sheetData>"
		"</worksheet>"
	)

	content_types_xml = (
		"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
		"<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
		"<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
		"<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
		"<Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>"
		"<Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>"
		"<Override PartName=\"/xl/sharedStrings.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml\"/>"
		"</Types>"
	)

	workbook_xml = (
		"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
		"<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
		"xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
		"<sheets>"
		"<sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rId1\"/>"
		"</sheets>"
		"</workbook>"
	)

	workbook_rels_xml = (
		"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
		"<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
		"<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/>"
		"<Relationship Id=\"rId2\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings\" Target=\"sharedStrings.xml\"/>"
		"</Relationships>"
	)

	root_rels_xml = (
		"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
		"<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
		"<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/>"
		"</Relationships>"
	)

	with ZipFile(path, "w") as archive:
		archive.writestr("[Content_Types].xml", content_types_xml)
		archive.writestr("_rels/.rels", root_rels_xml)
		archive.writestr("xl/workbook.xml", workbook_xml)
		archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
		archive.writestr("xl/sharedStrings.xml", shared_xml)
		archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
def _config_with(**overrides):
	cfg = deepcopy(load_config())
	for key, value in overrides.items():
		setattr(cfg, key, value)
	if "infer_missing_dates" in overrides:
		setattr(cfg, "use_issue_date_as_due_date", cfg.infer_missing_dates)
	return cfg
