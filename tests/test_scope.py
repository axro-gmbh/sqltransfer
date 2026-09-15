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
