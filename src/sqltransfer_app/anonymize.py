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


# Invented values, picked by hash. Short lists keep the generated SQL readable.
_FIRST_NAMES = ("Alina", "Ben", "Clara", "David", "Emma", "Finn", "Greta", "Hannes")
_LAST_NAMES = ("Brandt", "Cordes", "Dittmer", "Evers", "Falk", "Gerds", "Hoffmann", "Iversen")
_CITIES = ("Musterstadt", "Beispielheim", "Neudorf", "Althausen", "Westerbach", "Ostfelde")
_STREETS = ("Lindenweg", "Birkenallee", "Hauptstraße", "Feldweg", "Ringstraße", "Am Hang")


def _quote(identifier: str, db_type: str) -> str:
    mark = "`" if db_type == "mysql" else '"'
    return mark + identifier.replace(mark, mark * 2) + mark


def _quote_table(table: str, db_type: str) -> str:
    return ".".join(_quote(part.strip('`"'), db_type) for part in table.split("."))


def _hash(column: str, db_type: str) -> str:
    """A hex digest of salt plus value; the salt arrives as a parameter."""
    if db_type == "mysql":
        return f"SHA2(CONCAT(%s, {column}), 256)"
    return f"encode(sha256(convert_to(%s || {column}, 'UTF8')), 'hex')"


def _number(digest: str, db_type: str, hex_digits: int, modulo: int) -> str:
    """A non-negative integer derived from the first hex digits of the digest.

    At most seven hex digits: PostgreSQL's bit(32)::int is signed, so eight digits
    turn into a negative number and a postcode would come out as '-12345'. abs()
    is the belt to that suspenders.
    """
    hex_digits = min(hex_digits, 7)
    if db_type == "mysql":
        return f"(CONV(SUBSTRING({digest}, 1, {hex_digits}), 16, 10) % {modulo})"
    bits = hex_digits * 4
    return f"(abs(('x' || substr({digest}, 1, {hex_digits}))::bit({bits})::int) % {modulo})"


def _pick(values: tuple[str, ...], digest: str, db_type: str) -> str:
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    index = _number(digest, db_type, 4, len(values))
    if db_type == "mysql":
        return f"ELT(1 + {index}, {quoted})"
    return f"(ARRAY[{quoted}])[1 + {index}]"


def _concat(parts: list[str], db_type: str) -> str:
    return f"CONCAT({', '.join(parts)})" if db_type == "mysql" else " || ".join(parts)


def _digits(digest: str, db_type: str, count: int) -> str:
    modulo = 10**count
    number = _number(digest, db_type, count + 1, modulo)
    if db_type == "mysql":
        return f"LPAD({number}, {count}, '0')"
    return f"lpad(({number})::text, {count}, '0')"


def _replacement(kind: str, column: str, db_type: str) -> str:
    digest = _hash(column, db_type)
    if kind == "email":
        return _concat([f"'user-'", f"SUBSTRING({digest}, 1, 10)" if db_type == "mysql"
                        else f"substr({digest}, 1, 10)", "'@example.invalid'"], db_type)
    if kind == "firstname":
        return _pick(_FIRST_NAMES, digest, db_type)
    if kind == "lastname":
        return _pick(_LAST_NAMES, digest, db_type)
    if kind == "fullname":
        return _concat([_pick(_FIRST_NAMES, digest, db_type), "' '", _pick(_LAST_NAMES, digest, db_type)], db_type)
    if kind == "phone":
        return _concat(["'+49 30 '", _digits(digest, db_type, 7)], db_type)
    if kind == "street":
        return _concat([_pick(_STREETS, digest, db_type), "' '",
                        f"({_number(digest, db_type, 3, 199)} + 1)" if db_type == "mysql"
                        else f"({_number(digest, db_type, 3, 199)} + 1)::text"], db_type)
    if kind == "city":
        return _pick(_CITIES, digest, db_type)
    if kind == "postcode":
        return _digits(digest, db_type, 5)
    if kind == "text":
        return _concat(["'text-'", f"SUBSTRING({digest}, 1, 8)" if db_type == "mysql"
                        else f"substr({digest}, 1, 8)"], db_type)
    if kind == "empty":
        return "''"
    raise ValueError(f"unknown kind: {kind}")


def update_statement(plan: TablePlan, db_type: str, salt: str) -> tuple[str, list[str]] | None:
    """One UPDATE for the whole table, or None when there is nothing to rewrite.

    The salt is a query parameter, once per column, so it never reaches the SQL text.
    """
    if not plan.targets:
        return None
    assignments = []
    for column, kind in plan.targets:
        quoted = _quote(column, db_type)
        assignments.append(
            f"{quoted} = CASE WHEN {quoted} IS NULL THEN NULL WHEN {quoted} = '' THEN '' "
            f"ELSE {_replacement(kind, quoted, db_type)} END"
        )
    sql = f"UPDATE {_quote_table(plan.table, db_type)} SET " + ", ".join(assignments)
    return sql, [salt] * len(plan.targets)
