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
        Column("email", "varchar"),
        Column("vorname", "text"),
        Column("umsatz", "decimal"),
    ]
    plan = plan_table("kunde", columns, _rules())
    assert plan == TablePlan(table="kunde", targets=(("email", "email"), ("vorname", "firstname")), skipped=())


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
