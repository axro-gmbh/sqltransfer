# tests/test_anonymize_rules.py
from __future__ import annotations

import pytest

from sqltransfer_app.anonymize import Column, Rule, TablePlan, looks_personal, match_rule, plan_table


def _rules() -> list[Rule]:
    return [
        Rule(id=1, pattern="*mail*", kind="email"),
        Rule(id=2, pattern="vorname", kind="firstname"),
        Rule(id=3, pattern="telefon", kind="phone", enabled=False),
    ]


@pytest.mark.parametrize("name", ["email", "Email", "kunde_email", "billing_mail"])
def test_wildcards_and_case_do_not_matter(name):
    assert match_rule(name, _rules()).kind == "email"


def test_a_disabled_rule_does_not_match():
    assert match_rule("telefon", _rules()) is None


def test_the_first_matching_rule_wins():
    rules = [Rule(id=1, pattern="*name*", kind="text"), Rule(id=2, pattern="vorname", kind="firstname")]
    assert match_rule("vorname", rules).kind == "text"


def test_plan_lists_targets_and_leaves_the_rest_alone():
    columns = [
        Column("id", "int"),
        Column("email", "varchar", 190),
        Column("vorname", "text"),
        Column("umsatz", "decimal"),
    ]
    plan = plan_table("kunde", columns, _rules())
    assert plan == TablePlan(
        table="kunde",
        targets=(("email", "email", 190, None), ("vorname", "firstname", None, None)),
        skipped=(),
    )


def test_a_matching_column_of_the_wrong_type_is_skipped_with_a_reason():
    plan = plan_table("kunde", [Column("kunde_email", "bigint")], _rules())
    assert plan.targets == ()
    assert plan.skipped == (("kunde_email", "not a text column (bigint)"),)


def test_a_personal_looking_column_without_a_rule_is_reported():
    plan = plan_table("kunde", [Column("telefon", "varchar"), Column("iban", "varchar")], _rules())
    assert plan.targets == ()
    assert dict(plan.skipped) == {"telefon": "rule disabled", "iban": "no rule"}


def test_an_unremarkable_column_is_not_reported():
    plan = plan_table("kunde", [Column("umsatz", "decimal")], _rules())
    assert plan.skipped == ()


@pytest.mark.parametrize("name,expected", [("email", True), ("Geburtsdatum", True), ("strasse", True),
                                           ("umsatz", False), ("id", False)])
def test_looks_personal_catches_the_usual_suspects(name, expected):
    assert looks_personal(name) is expected


# --- findings from the branch review ---------------------------------------


@pytest.mark.parametrize("name", ["sort_order", "sortorder", "username", "filename", "tablename",
                                  "import_id", "export_date", "report_id", "transport", "gzip_level"])
def test_ordinary_columns_are_not_reported_as_personal(name):
    # Substring matching drowned the real finding in dozens of false positives:
    # "ort" hit sort_order and transport, "name" hit filename and username.
    assert looks_personal(name) is False


@pytest.mark.parametrize("name", ["ort", "liefer_ort", "Ort", "kunde_name", "name", "e_mail",
                                  "telefon_privat", "billingAddress", "zip_code"])
def test_personal_columns_are_still_reported(name):
    assert looks_personal(name) is True


def test_no_default_rule_renames_every_name_column():
    # A bare "name" rule turns country.name and payment_method.name into person
    # names on the first run, and the copy looks fine.
    from sqltransfer_app.anonymize import DEFAULT_RULES

    assert "name" not in [pattern for pattern, _kind in DEFAULT_RULES]
    assert any(pattern == "firstname" for pattern, _kind in DEFAULT_RULES)


def test_the_prefill_is_english_only():
    """The Axro databases name their columns in English, so German patterns only
    lengthen a list every developer has to read."""
    from sqltransfer_app.anonymize import DEFAULT_RULES

    muster = [pattern for pattern, _kind in DEFAULT_RULES]
    assert not [p for p in muster if p in {"vorname", "nachname", "*vorname*", "*nachname*",
                                           "adresse", "ort", "plz", "strasse", "*telefon*"}], muster
    for erwartet in ("firstname", "lastname", "company", "department"):
        assert erwartet in muster, muster


def test_a_json_rule_marks_a_column_for_looking_into_it():
    from sqltransfer_app.anonymize import Column, Rule, plan_table

    columns = [Column("custom_fields", "longtext"), Column("payload", "json"), Column("notiz", "text")]
    plan = plan_table("kunde", columns, [Rule(id=1, pattern="custom_fields", kind="json")])
    # the rule, plus the column whose type says JSON on its own
    assert plan.json_columns == (("custom_fields", "longtext"), ("payload", "json"))
    assert plan.targets == ()  # a JSON column is never replaced as a whole


def test_a_json_column_without_a_rule_is_still_looked_into():
    from sqltransfer_app.anonymize import Column, Rule, plan_table

    plan = plan_table("kunde", [Column("payload", "jsonb")], [Rule(id=1, pattern="*mail*", kind="email")])
    assert plan.json_columns == (("payload", "jsonb"),)


def test_the_plan_carries_the_exception_of_its_rule():
    from sqltransfer_app.anonymize import Column, Rule, plan_table

    plan = plan_table(
        "kunde",
        [Column("email", "varchar", 190), Column("ort", "varchar", 60)],
        [Rule(id=1, pattern="*mail*", kind="email", exception=r"^[0-9]+@axro\."),
         Rule(id=2, pattern="ort", kind="city")],
    )
    nach_spalte = {t[0]: t for t in plan.targets}
    assert nach_spalte["email"][3] == r"^[0-9]+@axro\."
    assert nach_spalte["ort"][3] is None
