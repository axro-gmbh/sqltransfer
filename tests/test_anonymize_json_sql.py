# tests/test_anonymize_json_sql.py
from __future__ import annotations

import pytest

from sqltransfer_app.anonymize import JsonTarget, TablePlan, update_statement


def _plan(*json_targets: JsonTarget, table: str = "kunde") -> TablePlan:
    return TablePlan(table=table, targets=(), skipped=(), json_targets=json_targets)


def test_mysql_rewrites_one_path_in_place():
    sql, params = update_statement(_plan(JsonTarget("custom_fields", ("email",), "email")), "mysql", "s")
    assert sql.startswith("UPDATE `kunde` SET `custom_fields` = JSON_REPLACE(`custom_fields`, '$.\"email\"'")
    assert "JSON_UNQUOTE(JSON_EXTRACT(`custom_fields`, '$.\"email\"'))" in sql
    assert params == ["s"] * sql.count("%s")


def test_mysql_nests_several_paths_of_one_column():
    sql, _ = update_statement(
        _plan(JsonTarget("custom_fields", ("email",), "email"),
              JsonTarget("custom_fields", ("adresse", "ort"), "city")), "mysql", "s")
    assert sql.count("JSON_REPLACE(") == 2
    assert '$."adresse"."ort"' in sql
    assert sql.count("SET `custom_fields` =") == 1  # one assignment, not two


def test_postgres_uses_jsonb_set_and_keeps_rows_without_the_key():
    sql, _ = update_statement(_plan(JsonTarget("custom_fields", ("email",), "email")), "postgres", "s")
    assert 'jsonb_set(' in sql and "'{email}'" in sql
    assert "#>> '{email}'" in sql                           # the original value, as text
    assert 'IS NULL THEN "custom_fields"' in sql            # a row without the key keeps its value


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_a_quote_or_dot_in_a_key_survives(db_type):
    sql, _ = update_statement(_plan(JsonTarget("f", ('we"ird.key',), "text")), db_type, "s")
    assert 'we""ird.key' in sql if db_type == "mysql" else 'we"ird.key' in sql


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_columns_and_json_paths_live_in_one_statement(db_type):
    plan = TablePlan(table="kunde", targets=(("email", "email", None),), skipped=(),
                     json_targets=(JsonTarget("custom_fields", ("email",), "email"),))
    sql, params = update_statement(plan, db_type, "s")
    assert sql.count("SET ") == 1 and "`email` = CASE" in sql if db_type == "mysql" else True
    assert "custom_fields" in sql and params == ["s"] * sql.count("%s")


@pytest.mark.parametrize("column_type,expected", [("jsonb", None), ("json", "::json"), ("longtext", "::text")])
def test_postgres_casts_back_to_the_column_type(column_type, expected):
    # jsonb_set only edits jsonb, so a json or text column has to be cast both ways.
    sql, _ = update_statement(
        _plan(JsonTarget("custom_fields", ("email",), "email", column_type)), "postgres", "s"
    )
    assert '"custom_fields"::jsonb' in sql          # read through jsonb in every case
    if expected:
        assert sql.rstrip().endswith(expected)      # and cast back for json and text
    else:
        assert not sql.rstrip().endswith("::text") and not sql.rstrip().endswith("::json")


def test_mysql_needs_no_cast():
    sql, _ = update_statement(
        _plan(JsonTarget("custom_fields", ("email",), "email", "longtext")), "mysql", "s"
    )
    assert "::" not in sql
