# tests/test_anonymize_sql.py
from __future__ import annotations

import re

import pytest

from sqltransfer_app.anonymize import TablePlan, update_statement


def _plan(*targets: tuple, table: str = "kunde") -> TablePlan:
    """Targets may be given short; the missing max length and exception default to None."""
    full = tuple(t + (None,) * (4 - len(t)) for t in targets)
    return TablePlan(table=table, targets=full, skipped=())


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


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_first_and_last_name_come_from_different_parts_of_the_digest(db_type):
    # Same slice for both lists means first and last name always move together:
    # 8 names instead of 64, and a UNIQUE name column collides within a few rows.
    sql, _ = update_statement(_plan(("name", "fullname")), db_type, "s")
    offsets = sorted(set(re.findall(r", (\d+), 4\)", sql)))
    assert len(offsets) >= 2, sql


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_a_short_column_cuts_the_replacement_instead_of_overflowing(db_type):
    # "data too long for column" aborted the whole transfer; on PostgreSQL after
    # the real rows had already landed.
    sql, _ = update_statement(_plan(("ort", "city", 6)), db_type, "s")
    assert "LEFT(" in sql and ", 6)" in sql


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_a_column_without_a_length_is_not_cut(db_type):
    sql, _ = update_statement(_plan(("beschreibung", "text")), db_type, "s")
    assert "LEFT(" not in sql



# An exception keeps values whose shape an application reads meaning from. Real case:
# AxroCustomer decides whether a customer is a debtor by matching the e-mail against
# ^[0-9]+@axro\. (customer number at the company domain). Replacing it took the
# contacts tab, the debtor header and "login as customer" with it, without any error.
AUSNAHME = r"^[0-9]+@axro\."


@pytest.mark.parametrize("db_type,operator", [("mysql", "REGEXP"), ("postgres", "~")])
def test_an_exception_keeps_the_matching_value(db_type, operator):
    sql, params = update_statement(
        _plan(("email", "email", 190, AUSNAHME)), db_type, "pepper"
    )
    assert f"THEN `email` " in sql or f'THEN "email" ' in sql
    assert operator in sql
    assert AUSNAHME in params, params
    assert AUSNAHME not in sql, "the pattern is a parameter, never statement text"


def test_the_exception_is_checked_before_the_replacement():
    sql, _ = update_statement(_plan(("email", "email", None, AUSNAHME)), "mysql", "s")
    assert sql.index("REGEXP") < sql.index("ELSE"), sql


def test_a_column_without_an_exception_is_unchanged():
    mit, _ = update_statement(_plan(("email", "email")), "mysql", "s")
    assert "REGEXP" not in mit


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_salt_and_exception_keep_their_order_as_parameters(db_type):
    sql, params = update_statement(
        _plan(("email", "email", None, AUSNAHME), ("ort", "city")), db_type, "pepper"
    )
    # One parameter per placeholder, in the order the statement reads them.
    assert len(params) == sql.count("%s")
    assert params.count(AUSNAHME) == 1
    assert params[0] == AUSNAHME or params[1] == AUSNAHME, params
