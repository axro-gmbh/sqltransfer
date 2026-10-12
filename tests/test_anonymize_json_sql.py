from __future__ import annotations

import pytest

from sqltransfer_app.anonymize import JsonTarget, TablePlan, update_statement


def _plan(*json_targets: JsonTarget, table: str = "kunde") -> TablePlan:
    return TablePlan(table=table, targets=(), skipped=(), json_targets=json_targets)


def test_mysql_passes_the_path_as_a_parameter():
    sql, params = update_statement(_plan(JsonTarget("custom_fields", ("email",), "email")), "mysql", "s")
    # The assignment is wrapped in the validity guard, the rewriting itself is unchanged.
    assert sql.startswith("UPDATE `kunde` SET `custom_fields` = CASE WHEN")
    assert "JSON_REPLACE(`custom_fields`, %s," in sql
    assert "JSON_UNQUOTE(JSON_EXTRACT(`custom_fields`, %s))" in sql
    assert '$."email"' not in sql                      # the path is data, not text
    assert params.count('$."email"') == 2
    assert len(params) == sql.count("%s")


def test_postgres_passes_the_path_as_a_list_parameter():
    sql, params = update_statement(_plan(JsonTarget("custom_fields", ("email",), "email")), "postgres", "s")
    assert "jsonb_set(" in sql and "#>> %s" in sql and ", false)" in sql
    assert "{email}" not in sql
    assert params.count(["email"]) == 2                # the path to write, and the value to read
    assert len(params) == sql.count("%s")


def test_several_paths_of_one_column_share_one_assignment():
    sql, params = update_statement(
        _plan(JsonTarget("custom_fields", ("email",), "email"),
              JsonTarget("custom_fields", ("adresse", "ort"), "city")), "mysql", "s")
    assert sql.count("SET `custom_fields` =") == 1
    assert sql.count("JSON_REPLACE(") == 2
    assert '$."adresse"."ort"' in params
    assert len(params) == sql.count("%s")


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
@pytest.mark.parametrize("key", ["a'b", 'a"b', "a\\b", "a,b", "a}b", "a.b", "a b", " mail ", "k" * 250])
def test_a_hostile_key_never_reaches_the_statement(db_type, key):
    # Key names come from customer data: a quote or a brace must not be able to
    # decide how the statement parses.
    sql, params = update_statement(_plan(JsonTarget("f", (key,), "email")), db_type, "s")
    assert key not in sql
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    expected = '$."' + escaped + '"' if db_type == "mysql" else [key]
    assert expected in params, f"the key must travel as a parameter, got {params}"
    assert len(params) == sql.count("%s")


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_columns_and_json_paths_live_in_one_statement(db_type):
    plan = TablePlan(table="kunde", targets=(("email", "email", None, None),), skipped=(),
                     json_targets=(JsonTarget("custom_fields", ("email",), "email"),))
    sql, params = update_statement(plan, db_type, "pepper")
    assert sql.count("SET ") == 1 and "custom_fields" in sql
    assert params.count("pepper") == sql.count("%s") - 2
    assert len(params) == sql.count("%s")


@pytest.mark.parametrize("column_type,expected", [("jsonb", None), ("json", "::json"), ("longtext", "::text")])
def test_postgres_casts_back_to_the_column_type(column_type, expected):
    # jsonb_set only edits jsonb, so a json or text column has to be cast both ways.
    sql, _params = update_statement(
        _plan(JsonTarget("custom_fields", ("email",), "email", column_type)), "postgres", "s"
    )
    assert '"custom_fields"::jsonb' in sql
    # The cast sits on the rewriting branch of the guard, not at the end of the statement.
    zweig = sql.split("THEN ")[-2] if "THEN " in sql else sql
    if expected:
        assert f"{expected} ELSE" in sql, sql
    else:
        assert "::text ELSE" not in sql and "::json ELSE" not in sql, sql


def test_mysql_needs_no_cast():
    sql, _params = update_statement(
        _plan(JsonTarget("custom_fields", ("email",), "email", "longtext")), "mysql", "s"
    )
    assert "::" not in sql


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_a_row_that_is_not_valid_json_keeps_its_value(db_type):
    """A grown table holds legacy rows: an empty string, a truncated payload.

    One of them used to cost the whole column: the discovery refused it and nothing in
    that column was anonymized. The valid rows have to be rewritten and the others left
    exactly as they are.
    """
    plan = TablePlan(table="axro_rma", targets=(), skipped=(),
                     json_targets=(JsonTarget("rma_data", ("Rma", "email"), "email", "longtext"),))
    sql, _params = update_statement(plan, db_type, "pepper")

    pruefung = "JSON_VALID" if db_type == "mysql" else "pg_input_is_valid"
    assert pruefung in sql, sql
    # The column keeps its own value where the check fails.
    assert "CASE WHEN" in sql and "END" in sql, sql
