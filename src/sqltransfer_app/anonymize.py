# src/sqltransfer_app/anonymize.py
"""Which columns hold personal data, and what happens to them.

Everything here is a plain function over names and types: no database, no Flet,
so the rules can be tested without either.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
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
    ("*vorname*", "firstname"),
    ("*nachname*", "lastname"),
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

# Words that make a column suspicious even when no rule matches it. Matched against
# the column's words, not as substrings: "ort" must not fire on sort_order or
# transport, and "name" must not fire on filename or username, or the one column
# that really was forgotten drowns in the noise.
_PERSONAL_WORDS: frozenset[str] = frozenset(
    {
        "mail", "email", "e", "name", "vorname", "nachname", "firstname", "lastname",
        "phone", "telefon", "tel", "mobil", "mobile", "strasse", "street", "address",
        "adresse", "plz", "zip", "postcode", "city", "ort", "iban", "geburt", "geburtstag",
        "birth", "birthday", "birthdate", "ssn",
    }
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
    max_length: int | None = None  # characters, as information_schema reports it


@dataclass(frozen=True, slots=True)
class TablePlan:
    """What happens to one table: what gets rewritten, and what was left alone and why."""

    table: str
    targets: tuple[tuple[str, str, int | None], ...]  # (column, kind, max length)
    skipped: tuple[tuple[str, str], ...]  # (column, reason)

    def __bool__(self) -> bool:
        return bool(self.targets)


def _words(column_name: str) -> list[str]:
    """Split a column name into words: snake_case, camelCase and plain runs."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", column_name)
    return [word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if word]


# Stems that stay unambiguous inside a German compound, so "Geburtsdatum" and
# "lieferadresse" are caught. Deliberately not "name", "ort" or "zip": those would
# bring back filename, sort_order and gzip.
_PERSONAL_STEMS: tuple[str, ...] = ("geburt", "telefon", "mobil", "strasse", "adresse", "mail", "iban")


def looks_personal(column_name: str) -> bool:
    words = _words(column_name)
    if any(word in _PERSONAL_WORDS for word in words):
        return True
    return any(word.startswith(stem) or word.endswith(stem) for word in words for stem in _PERSONAL_STEMS)


def match_rule(column_name: str, rules: Sequence[Rule]) -> Rule | None:
    """The first enabled rule whose pattern matches, ignoring case."""
    name = column_name.lower()
    for rule in rules:
        if rule.enabled and fnmatchcase(name, rule.pattern.lower()):
            return rule
    return None


def plan_table(table: str, columns: Sequence[Column], rules: Sequence[Rule]) -> TablePlan:
    """Split a table's columns into what gets rewritten and what is reported."""
    targets: list[tuple[str, str, int | None]] = []
    skipped: list[tuple[str, str]] = []
    for column in columns:
        rule = match_rule(column.name, rules)
        data_type = column.data_type.lower()
        if rule and data_type in TEXT_TYPES:
            targets.append((column.name, rule.kind, column.max_length))
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


def _number(digest: str, db_type: str, hex_digits: int, modulo: int, offset: int = 1) -> str:
    """A non-negative integer derived from the first hex digits of the digest.

    At most seven hex digits: PostgreSQL's bit(32)::int is signed, so eight digits
    turn into a negative number and a postcode would come out as '-12345'. abs()
    is the belt to that suspenders.

    MOD() rather than the % operator: both drivers read a literal percent sign in
    a parameterised statement as a placeholder and refuse the query.
    """
    hex_digits = min(hex_digits, 7)
    if db_type == "mysql":
        return f"MOD(CONV(SUBSTRING({digest}, {offset}, {hex_digits}), 16, 10), {modulo})"
    bits = hex_digits * 4
    return f"mod(abs(('x' || substr({digest}, {offset}, {hex_digits}))::bit({bits})::int), {modulo})"


def _pick(values: tuple[str, ...], digest: str, db_type: str, offset: int = 1) -> str:
    """Pick from a list by hash. `offset` picks a different slice of the digest, so
    two lists in one value (first and last name) vary independently."""
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    index = _number(digest, db_type, 4, len(values), offset)
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
        return _pick(_LAST_NAMES, digest, db_type, offset=9)
    if kind == "fullname":
        return _concat(
            [_pick(_FIRST_NAMES, digest, db_type), "' '", _pick(_LAST_NAMES, digest, db_type, offset=9)], db_type
        )
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
    for column, kind, max_length in plan.targets:
        quoted = _quote(column, db_type)
        replacement = _replacement(kind, quoted, db_type)
        if max_length:
            # A fake e-mail address needs 31 characters; a real ort VARCHAR(10)
            # would otherwise abort the whole transfer with "data too long".
            replacement = f"LEFT({replacement}, {int(max_length)})"
        assignments.append(
            f"{quoted} = CASE WHEN {quoted} IS NULL THEN NULL WHEN {quoted} = '' THEN '' "
            f"ELSE {replacement} END"
        )
    sql = f"UPDATE {_quote_table(plan.table, db_type)} SET " + ", ".join(assignments)
    # One salt per placeholder, not per column: a kind like street or fullname
    # embeds the digest twice and therefore carries two.
    return sql, [salt] * sql.count("%s")
