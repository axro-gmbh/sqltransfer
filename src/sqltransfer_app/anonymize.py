# src/sqltransfer_app/anonymize.py
"""Which columns hold personal data, and what happens to them.

Everything here is a plain function over names and types: no database, no Flet,
so the rules can be tested without either.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Sequence

# (value, label) in the order the rule dialog offers them.
KINDS: tuple[tuple[str, str], ...] = (
    ("email", "E-mail address"),
    ("firstname", "First name"),
    ("lastname", "Last name"),
    ("fullname", "Full name"),
    ("phone", "Phone number"),
    ("street", "Street and number"),
    ("city", "City"),
    ("postcode", "Postcode"),
    ("text", "Generic text"),
    ("empty", "Empty"),
)

# Seeded on first start; German and English spellings of the usual columns.
DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    ("*mail*", "email"),
    ("vorname", "firstname"),
    ("first_name", "firstname"),
    ("nachname", "lastname"),
    ("last_name", "lastname"),
    ("name", "fullname"),
    ("*telefon*", "phone"),
    ("phone*", "phone"),
    ("mobil*", "phone"),
    ("strasse", "street"),
    ("street*", "street"),
    ("adresse", "street"),
    ("address*", "street"),
    ("plz", "postcode"),
    ("zip*", "postcode"),
    ("ort", "city"),
    ("city", "city"),
)

# information_schema data types we dare to overwrite with a string.
TEXT_TYPES: frozenset[str] = frozenset(
    {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "character varying", "character", "citext"}
)

# Fragments that make a column suspicious even when no rule matches it.
_PERSONAL_FRAGMENTS: tuple[str, ...] = (
    "mail", "name", "phone", "telefon", "mobil", "strasse", "street", "address", "adresse",
    "plz", "zip", "city", "ort", "iban", "geburt", "birth", "ssn",
)


@dataclass(frozen=True, slots=True)
class Rule:
    id: int | None
    pattern: str
    kind: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    data_type: str


@dataclass(frozen=True, slots=True)
class TablePlan:
    """What happens to one table: what gets rewritten, and what was left alone and why."""

    table: str
    targets: tuple[tuple[str, str], ...]  # (column, kind)
    skipped: tuple[tuple[str, str], ...]  # (column, reason)

    def __bool__(self) -> bool:
        return bool(self.targets)


def looks_personal(column_name: str) -> bool:
    name = column_name.lower()
    return any(fragment in name for fragment in _PERSONAL_FRAGMENTS)


def match_rule(column_name: str, rules: Sequence[Rule]) -> Rule | None:
    """The first enabled rule whose pattern matches, ignoring case."""
    name = column_name.lower()
    for rule in rules:
        if rule.enabled and fnmatchcase(name, rule.pattern.lower()):
            return rule
    return None


def plan_table(table: str, columns: Sequence[Column], rules: Sequence[Rule]) -> TablePlan:
    """Split a table's columns into what gets rewritten and what is reported."""
    targets: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for column in columns:
        rule = match_rule(column.name, rules)
        data_type = column.data_type.lower()
        if rule and data_type in TEXT_TYPES:
            targets.append((column.name, rule.kind))
        elif rule:
            skipped.append((column.name, f"not a text column ({data_type})"))
        elif looks_personal(column.name):
            disabled = any(fnmatchcase(column.name.lower(), r.pattern.lower()) for r in rules if not r.enabled)
            skipped.append((column.name, "rule disabled" if disabled else "no rule"))
    return TablePlan(table=table, targets=tuple(targets), skipped=tuple(skipped))
