# tests/test_anonymize_sql.py
from __future__ import annotations

import pytest

from sqltransfer_app.anonymize import TablePlan, update_statement


def _plan(*targets: tuple[str, str], table: str = "kunde") -> TablePlan:
    return TablePlan(table=table, targets=targets, skipped=())


def test_nothing_to_do_yields_no_statement():
    assert update_statement(_plan(), "mysql", "s") is None


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_the_salt_travels_as_a_parameter_for_every_placeholder(db_type):
    sql, params = update_statement(_plan(("email", "email"), ("ort", "city")), db_type, "pepper")
    assert params == ["pepper"] * sql.count("%s")
    assert len(params) >= 2
    assert "pepper" not in sql


def test_mysql_statement_shape():
    sql, _ = update_statement(_plan(("email", "email")), "mysql", "s")
    assert sql.startswith("UPDATE `kunde` SET `email` = CASE")
    assert "WHEN `email` IS NULL THEN NULL" in sql
    assert "WHEN `email` = '' THEN ''" in sql
    assert "SHA2(CONCAT(%s, `email`), 256)" in sql
    assert "@example.invalid" in sql


def test_postgres_statement_shape():
    sql, _ = update_statement(_plan(("email", "email")), "postgres", "s")
    assert sql.startswith('UPDATE "kunde" SET "email" = CASE')
    assert "sha256(convert_to(%s || \"email\", 'UTF8'))" in sql
    assert "@example.invalid" in sql


@pytest.mark.parametrize("db_type,quoted", [("mysql", "`from`"), ("postgres", '"from"')])
def test_identifiers_that_need_quoting_survive(db_type, quoted):
    sql, _ = update_statement(_plan(("from", "text"), table="order"), db_type, "s")
    assert quoted in sql


def test_a_schema_qualified_table_keeps_both_parts():
    sql, _ = update_statement(_plan(("email", "email"), table="public.kunde"), "postgres", "s")
    assert sql.startswith('UPDATE "public"."kunde" SET')


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
@pytest.mark.parametrize("kind", ["email", "firstname", "lastname", "fullname", "phone", "street",
                                  "city", "postcode", "text", "empty"])
def test_every_kind_keeps_null_and_empty(db_type, kind):
    sql, _ = update_statement(_plan(("spalte", kind)), db_type, "s")
    assert "IS NULL THEN NULL" in sql
    assert "= '' THEN ''" in sql


def test_an_unknown_kind_is_refused_loudly():
    with pytest.raises(ValueError, match="unknown kind"):
        update_statement(_plan(("spalte", "nonsense")), "mysql", "s")


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_the_only_percent_signs_are_placeholders(db_type):
    # A literal % (from a modulo) makes pymysql and psycopg read the statement as
    # a format string and refuse it: "unsupported format character".
    sql, params = update_statement(
        _plan(("a", "postcode"), ("b", "phone"), ("c", "city"), ("d", "street")), db_type, "s"
    )
    assert sql.count("%") == len(params)
    assert "%s" in sql
