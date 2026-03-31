#!/usr/bin/env python3
"""Migrate EasyFlex application data from an old PostgreSQL database."""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Iterable

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, execute_values

TABLES_IN_ORDER = [
    "user",
    "user_settings",
    "company",
    "doc_type",
    "invoice_batch",
    "invoice_row",
]


@dataclass
class TablePlan:
    table_name: str
    columns: list[str]
    row_count: int


def _connect(url: str):
    return psycopg2.connect(url)


def _normalize_postgres_url(url: str) -> str:
    cleaned = (url or "").strip()
    if cleaned.startswith("postgres://"):
        return "postgresql://" + cleaned[len("postgres://"):]
    return cleaned


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            )
            """,
            (table_name,),
        )
        return bool(cur.fetchone()[0])


def _common_columns(source_conn, target_conn, table_name: str) -> list[str]:
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """
    with source_conn.cursor() as cur:
        cur.execute(query, (table_name,))
        source_cols = [row[0] for row in cur.fetchall()]
    with target_conn.cursor() as cur:
        cur.execute(query, (table_name,))
        target_cols = {row[0] for row in cur.fetchall()}
    return [column for column in source_cols if column in target_cols]


def _fetch_rows(conn, table_name: str, columns: list[str]) -> list[dict]:
    if not columns:
        return []
    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.Identifier(table_name),
    )
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query)
        return list(cur.fetchall())


def _truncate_target_tables(conn, tables: Iterable[str]) -> None:
    table_idents = sql.SQL(", ").join(sql.Identifier(name) for name in tables)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(table_idents))


def _insert_rows(conn, table_name: str, columns: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    values = [[row.get(column) for column in columns] for row in rows]
    insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    with conn.cursor() as cur:
        execute_values(cur, insert_sql.as_string(conn), values, page_size=500)


def _reset_sequences(conn, table_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_default LIKE 'nextval%%'
            """,
            (table_name,),
        )
        serial_columns = [row[0] for row in cur.fetchall()]
        for column_name in serial_columns:
            quoted_table_name = 'public."user"' if table_name == "user" else f"public.{table_name}"
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (quoted_table_name, column_name))
            sequence_name = cur.fetchone()[0]
            if not sequence_name:
                continue
            cur.execute(
                sql.SQL(
                    "SELECT setval(%s, COALESCE((SELECT MAX({column}) FROM {table}), 1), true)"
                ).format(
                    column=sql.Identifier(column_name),
                    table=sql.Identifier(table_name),
                ),
                (sequence_name,),
            )


def _build_plan(source_conn, target_conn) -> list[TablePlan]:
    plan: list[TablePlan] = []
    for table_name in TABLES_IN_ORDER:
        if not _table_exists(source_conn, table_name):
            continue
        if not _table_exists(target_conn, table_name):
            continue
        columns = _common_columns(source_conn, target_conn, table_name)
        rows = _fetch_rows(source_conn, table_name, columns)
        plan.append(TablePlan(table_name=table_name, columns=columns, row_count=len(rows)))
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate EasyFlex data from old PostgreSQL to local PostgreSQL.")
    parser.add_argument("--source-url", default=os.getenv("SOURCE_DATABASE_URL"), help="Old PostgreSQL URL")
    parser.add_argument("--target-url", default=os.getenv("DATABASE_URL"), help="Target PostgreSQL URL")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be migrated")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    source_url = _normalize_postgres_url(args.source_url or "")
    target_url = _normalize_postgres_url(args.target_url or "")
    if not source_url:
        raise SystemExit("Missing source database URL. Use --source-url or SOURCE_DATABASE_URL.")
    if not target_url:
        raise SystemExit("Missing target database URL. Use --target-url or DATABASE_URL.")

    with _connect(source_url) as source_conn, _connect(target_url) as target_conn:
        plan = _build_plan(source_conn, target_conn)
        if not plan:
            raise SystemExit("No matching EasyFlex tables found to migrate.")

        print("Migration plan:")
        for item in plan:
            print(f"  - {item.table_name}: {item.row_count} rows, columns={', '.join(item.columns)}")

        if args.dry_run:
            print("Dry run only. No data written.")
            return

        if not args.yes:
            response = input("This will replace data in the target database. Continue? [y/N]: ").strip().lower()
            if response not in {"y", "yes"}:
                print("Cancelled.")
                return

        table_names = [item.table_name for item in plan]
        target_conn.autocommit = False
        try:
            _truncate_target_tables(target_conn, reversed(table_names))
            for item in plan:
                rows = _fetch_rows(source_conn, item.table_name, item.columns)
                _insert_rows(target_conn, item.table_name, item.columns, rows)
                _reset_sequences(target_conn, item.table_name)
                print(f"Migrated {len(rows)} rows into {item.table_name}")
            target_conn.commit()
        except Exception:
            target_conn.rollback()
            raise

    print("Migration completed successfully.")


if __name__ == "__main__":
    main()
