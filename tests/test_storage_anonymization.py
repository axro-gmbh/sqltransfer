# tests/test_storage_anonymization.py
from __future__ import annotations

from pathlib import Path

import pytest

from sqltransfer_app.anonymize import DEFAULT_RULES, Rule
from sqltransfer_app.storage import DuplicateProfileName, ProfileGone, Storage


def test_a_new_database_comes_with_the_default_rules(tmp_path: Path):
    rules = Storage(tmp_path / "profiles.db").list_anonymization_rules()
    assert [(r.pattern, r.kind) for r in rules] == list(DEFAULT_RULES)
    assert all(r.enabled for r in rules)


def test_rules_survive_reopening_and_are_not_seeded_twice(tmp_path: Path):
    path = tmp_path / "profiles.db"
    storage = Storage(path)
    first = storage.list_anonymization_rules()
    storage.delete_anonymization_rule(first[0].id)
    again = Storage(path).list_anonymization_rules()
    assert len(again) == len(first) - 1


def test_adding_editing_and_disabling(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    rule_id = storage.save_anonymization_rule(Rule(id=None, pattern="kunden_nr", kind="text"))
    storage.save_anonymization_rule(Rule(id=rule_id, pattern="kunden_nr", kind="empty", enabled=False))
    stored = {r.id: r for r in storage.list_anonymization_rules()}[rule_id]
    assert (stored.pattern, stored.kind, stored.enabled) == ("kunden_nr", "empty", False)


def test_a_repeated_pattern_is_refused(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    storage.save_anonymization_rule(Rule(id=None, pattern="kunden_nr", kind="text"))
    with pytest.raises(DuplicateProfileName):
        storage.save_anonymization_rule(Rule(id=None, pattern="kunden_nr", kind="email"))


def test_editing_a_deleted_rule_says_so(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    rule_id = storage.save_anonymization_rule(Rule(id=None, pattern="x", kind="text"))
    storage.delete_anonymization_rule(rule_id)
    with pytest.raises(ProfileGone):
        storage.save_anonymization_rule(Rule(id=rule_id, pattern="x", kind="text"))


def test_a_rule_keeps_its_exception(tmp_path):
    """The exception is a regular expression over the value; it has to survive a restart."""
    store = Storage(tmp_path / "p.db")
    rule_id = store.save_anonymization_rule(
        Rule(id=None, pattern="*konto*", kind="email", exception=r"^[0-9]+@axro\.")
    )

    wieder = Storage(tmp_path / "p.db")
    rules = {r.id: r for r in wieder.list_anonymization_rules()}
    assert rules[rule_id].exception == r"^[0-9]+@axro\."

    wieder.save_anonymization_rule(Rule(id=rule_id, pattern="*konto*", kind="email", exception=None))
    assert {r.id: r for r in wieder.list_anonymization_rules()}[rule_id].exception is None


def test_an_old_database_gets_the_column(tmp_path):
    """A profiles.db written before this feature must open without losing its rules."""
    import sqlite3

    path = tmp_path / "alt.db"
    Storage(path)  # schema anlegen
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE anonymization_rules DROP COLUMN exception")
        conn.execute("INSERT INTO anonymization_rules(pattern, kind) VALUES ('*konto*', 'email')")

    rules = Storage(path).list_anonymization_rules()
    assert [r.pattern for r in rules if r.pattern == "*konto*"], rules
    assert all(r.exception is None for r in rules)
