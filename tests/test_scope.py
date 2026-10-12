from __future__ import annotations

import pytest

from sqltransfer_app.scope import ScopeError, resolve_scope, summarize_notes


def test_single_table_maps_to_table_mode():
    assert resolve_scope("single", single_table="public.events") == ("table", "public.events")


def test_single_table_requires_a_value():
    with pytest.raises(ScopeError):
        resolve_scope("single", single_table="   ")


def test_multi_tables_are_normalized_to_a_comma_list():
    mode, value = resolve_scope("multi", multi_tables=["  a ", "b", "", "a"])
    assert (mode, value) == ("tables", "a, b")


def test_multi_tables_require_at_least_one_entry():
    with pytest.raises(ScopeError):
        resolve_scope("multi", multi_tables=[])


def test_all_uses_explicit_scope_when_given():
    assert resolve_scope("all", all_scope=" sw6 ", db_type="mysql", database="ignored") == ("schema", "sw6")


def test_all_falls_back_to_database_name_for_mysql():
    assert resolve_scope("all", db_type="mysql", database="sw6") == ("schema", "sw6")


def test_all_falls_back_to_schema_hint_for_postgres():
    assert resolve_scope("all", db_type="postgres", schema_hint="reporting") == ("schema", "reporting")


def test_all_defaults_to_public_for_postgres_without_hint():
    assert resolve_scope("all", db_type="postgres", schema_hint="") == ("schema", "public")


def test_unknown_choice_is_rejected():
    with pytest.raises(ScopeError):
        resolve_scope("everything")


def test_summarize_notes_counts_instead_of_listing_every_table():
    notes = ["tables_total=361"] + ["swapped=t%d" % i for i in range(3)] + ["fallback_skip_swap=x", "fk_deferred=y"]
    assert summarize_notes(notes) == "tables_total=361, swapped=3, fallback_skip_swap=1, fk_deferred=1"


def test_summarize_notes_handles_empty_input():
    assert summarize_notes([]) == ""


# --- which schema a listing reads ------------------------------------------


def test_the_schema_field_wins_over_the_profile():
    """Real confusion: the field still said 'shopware' from a local run while the
    profile pointed at the production server, whose schema is 'sw6'. The listing came
    back empty and said nothing about where it had looked."""
    from sqltransfer_app.transfer import resolve_table_schema

    assert resolve_table_schema("mysql", "shopware", "sw6") == "shopware"
    assert resolve_table_schema("mysql", "  shopware  ", "sw6") == "shopware"
    assert resolve_table_schema("mysql", "`sw6`", "sw6") == "sw6"


def test_without_a_field_the_profile_decides():
    from sqltransfer_app.transfer import resolve_table_schema

    assert resolve_table_schema("mysql", None, "sw6") == "sw6"
    assert resolve_table_schema("mysql", "", "sw6") == "sw6"


def test_postgresql_falls_back_to_public():
    from sqltransfer_app.transfer import resolve_table_schema

    assert resolve_table_schema("postgres", None, "ediapi") == "public"
    assert resolve_table_schema("postgres", "sales", "ediapi") == "sales"


# --- text columns that hold JSON but have no rule ---------------------------


def test_a_text_column_without_a_rule_is_named_as_a_json_candidate():
    """Real case: axro_rma.rma_data is a LONGTEXT full of JSON. Without a rule of kind
    "Look inside the JSON" the app never looked into it and said nothing either, because
    the column name does not look personal."""
    from sqltransfer_app.anonymize import Column, Rule
    from sqltransfer_app.anonymize import json_candidates

    spalten = [Column("id", "int"), Column("rma_data", "longtext"), Column("notiz", "text"),
               Column("custom_fields", "json")]
    regeln = [Rule(id=1, pattern="*mail*", kind="email")]

    # Nothing is sampled here; the function only says which columns are worth a look.
    assert json_candidates(spalten, regeln) == ["rma_data", "notiz"]


def test_a_column_that_already_has_a_json_rule_is_no_candidate():
    from sqltransfer_app.anonymize import Column, Rule, json_candidates

    spalten = [Column("rma_data", "longtext")]
    assert json_candidates(spalten, [Rule(id=1, pattern="rma_data", kind="json")]) == []


def test_a_column_with_another_rule_is_no_candidate():
    from sqltransfer_app.anonymize import Column, Rule, json_candidates

    spalten = [Column("email", "varchar")]
    assert json_candidates(spalten, [Rule(id=1, pattern="*mail*", kind="email")]) == []
