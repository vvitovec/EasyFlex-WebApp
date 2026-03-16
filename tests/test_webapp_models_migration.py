import webapp.models as models


def test_extract_alter_table_target_simple():
	sql = "ALTER TABLE invoice_batch ADD COLUMN IF NOT EXISTS processed_invoices INTEGER"
	assert models._extract_alter_table_target(sql) == "invoice_batch"


def test_extract_alter_table_target_quoted_schema():
	sql = 'ALTER TABLE "public"."invoice_batch" ADD COLUMN "x" INTEGER'
	assert models._extract_alter_table_target(sql) == '"public"."invoice_batch"'


def test_extract_add_column_if_not_exists_target():
	sql = 'ALTER TABLE "public"."invoice_batch" ADD COLUMN IF NOT EXISTS "processed_invoices" INTEGER'
	assert models._extract_add_column_if_not_exists_target(sql) == (
		'"public"."invoice_batch"',
		'"processed_invoices"',
	)


def test_split_table_ref_handles_quotes():
	assert models._split_table_ref('"public"."invoice_batch"') == ("public", "invoice_batch")
	assert models._split_table_ref("invoice_batch") == (None, "invoice_batch")
