# Anonymization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to implement this plan task by task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace personal data in copied tables with deterministic fake values, driven by rules on
column names that the user can edit.

**Architecture:** A pure module (`anonymize.py`) answers three questions without touching a database:
which columns a rule set hits, why a column was skipped, and what the `UPDATE` statement for one
table looks like per dialect. `storage.py` persists the rules, `secrets.py` holds the per-installation
salt, `transfer.py` reads destination columns and runs the statement, and `app.py` wires the switch,
the rule list and the reporting. The statement runs on the MySQL temp table before the swap, and on
the PostgreSQL destination table after the copy.

**Tech Stack:** Python 3.12+, Flet 1.0, pymysql, psycopg 3, SQLite, pytest. No new dependencies.

**Spec:** `docs/design/2026-10-01-anonymization.md` — read it first; this plan implements it and does
not repeat its reasoning.

## Global Constraints

- Code comments in English; UI strings in English; commit messages in German, lowercase after the
  colon, format `typ: beschreibung`.
- No new runtime dependency.
- Only text columns are rewritten: `char`, `varchar`, `text`, `tinytext`, `mediumtext`, `longtext`,
  `character varying`, `character`, `citext`.
- Fake e-mail addresses end in `@example.invalid` (RFC 2606).
- `NULL` stays `NULL`, `''` stays `''`, in every kind.
- Salt is read from the Keychain under the key `anonymization:salt`, never written to a file or log.
- The salt reaches SQL as a query parameter, never as string interpolation.
- Identifiers are quoted with the existing helpers (`` ` `` for MySQL, `"` for PostgreSQL).
- Every task ends with a commit; tests pass before each commit.

## Review Focus

Five things the spec implies that no single task's happy path exercises. Each has a test in the task
that owns the code.

1. **A matching column of the wrong type** (`telefon BIGINT`) must be skipped and reported, not
   rewritten into a type error → Task 1.
2. **A table with no matching column** must produce no statement at all, rather than `UPDATE t SET`
   with an empty list → Task 2.
3. **Identifiers that need quoting** (`order`, `Straße`, a column called `from`) must survive in both
   dialects → Task 2.
4. **A missing salt on first use** must be created once and reused, and two calls must not produce
   two different salts → Task 4.
5. **A failing `UPDATE` in the MySQL path** must leave the destination table untouched: no swap, the
   run fails → Task 6.
6. **Numbers derived from the hash must stay non-negative.** PostgreSQL's `bit(32)::int` is signed,
   so a postcode could come out as `-12345` and a phone number with a minus in it. Pinned by the
   format assertions in Task 5.

---

### Task 1: Rules and what they hit

**Files:**
- Create: `src/sqltransfer_app/anonymize.py`
- Test: `tests/test_anonymize_rules.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Rule(id, pattern, kind, enabled)`, `Column(name, data_type)`,
  `TablePlan(table, targets, skipped)` where `targets` is `tuple[tuple[str, str], ...]` of
  (column name, kind) and `skipped` is `tuple[tuple[str, str], ...]` of (column name, reason);
  `KINDS: tuple[tuple[str, str], ...]`, `DEFAULT_RULES: tuple[tuple[str, str], ...]`,
  `TEXT_TYPES: frozenset[str]`, `looks_personal(name) -> bool`,
  `match_rule(name, rules) -> Rule | None`, `plan_table(table, columns, rules) -> TablePlan`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_rules.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'sqltransfer_app.anonymize'`

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_rules.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/anonymize.py tests/test_anonymize_rules.py
git commit -m "feat: regeln, welche spalten personendaten enthalten"
```

---

### Task 2: The UPDATE statement, per dialect

**Files:**
- Modify: `src/sqltransfer_app/anonymize.py`
- Test: `tests/test_anonymize_sql.py`

**Interfaces:**
- Consumes: `TablePlan` from Task 1.
- Produces: `update_statement(plan, db_type, salt) -> tuple[str, list[str]] | None` returning the
  statement and its parameters (the salt, once per column), or `None` when nothing is to be done.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_anonymize_sql.py
from __future__ import annotations

import pytest

from sqltransfer_app.anonymize import TablePlan, update_statement


def _plan(*targets: tuple[str, str], table: str = "kunde") -> TablePlan:
    return TablePlan(table=table, targets=targets, skipped=())


def test_nothing_to_do_yields_no_statement():
    assert update_statement(_plan(), "mysql", "s") is None


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_the_salt_travels_as_a_parameter_once_per_column(db_type):
    sql, params = update_statement(_plan(("email", "email"), ("ort", "city")), db_type, "pepper")
    assert params == ["pepper", "pepper"]
    assert "pepper" not in sql


def test_mysql_statement_shape():
    sql, _ = update_statement(_plan(("email", "email")), "mysql", "s")
    assert sql.startswith("UPDATE `kunde` SET `email` = CASE")
    assert "WHEN `email` IS NULL THEN NULL" in sql
    assert "WHEN `email` = '' THEN ''" in sql
    assert "SHA2(CONCAT(%s, `email`), 256)" in sql
    assert "@example.invalid" in sql


def test_postgres_statement_shape():
    sql, _ = update_statement(_plan(("email", "email")), "postgres", "s")
    assert sql.startswith('UPDATE "kunde" SET "email" = CASE')
    assert "sha256(convert_to(%s || \"email\", 'UTF8'))" in sql
    assert "@example.invalid" in sql


@pytest.mark.parametrize("db_type,quoted", [("mysql", "`from`"), ("postgres", '"from"')])
def test_identifiers_that_need_quoting_survive(db_type, quoted):
    sql, _ = update_statement(_plan(("from", "text"), table="order"), db_type, "s")
    assert quoted in sql


def test_a_schema_qualified_table_keeps_both_parts():
    sql, _ = update_statement(_plan(("email", "email"), table="public.kunde"), "postgres", "s")
    assert sql.startswith('UPDATE "public"."kunde" SET')


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
@pytest.mark.parametrize("kind", ["email", "firstname", "lastname", "fullname", "phone", "street",
                                  "city", "postcode", "text", "empty"])
def test_every_kind_keeps_null_and_empty(db_type, kind):
    sql, _ = update_statement(_plan(("spalte", kind)), db_type, "s")
    assert "IS NULL THEN NULL" in sql
    assert "= '' THEN ''" in sql


def test_an_unknown_kind_is_refused_loudly():
    with pytest.raises(ValueError, match="unknown kind"):
        update_statement(_plan(("spalte", "nonsense")), "mysql", "s")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_sql.py`
Expected: FAIL with `ImportError: cannot import name 'update_statement'`

- [ ] **Step 3: Write the implementation**

Append to `src/sqltransfer_app/anonymize.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_sql.py`
Expected: PASS. Fix the `_replacement` branches until every kind keeps the `NULL` and `''` arms.

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/anonymize.py tests/test_anonymize_sql.py
git commit -m "feat: update-anweisung für beide datenbanken"
```

---

### Task 3: Storing the rules

**Files:**
- Modify: `src/sqltransfer_app/storage.py`
- Test: `tests/test_storage_anonymization.py`

**Interfaces:**
- Consumes: `Rule`, `DEFAULT_RULES` from Task 1.
- Produces: `Storage.list_anonymization_rules() -> list[Rule]`,
  `Storage.save_anonymization_rule(rule) -> int` (insert without id, update with id, raising
  `DuplicateProfileName` on a repeated pattern), `Storage.delete_anonymization_rule(rule_id)`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv314/bin/python -m pytest -q tests/test_storage_anonymization.py`
Expected: FAIL with `AttributeError: 'Storage' object has no attribute 'list_anonymization_rules'`

- [ ] **Step 3: Write the implementation**

In `_init_schema`, add to the `executescript` block:

```sql
CREATE TABLE IF NOT EXISTS anonymization_rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT    NOT NULL UNIQUE,
    kind    TEXT    NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
```

At the end of `_migrate_schema`, seed once, marking the seeding in a one-row table so a user who
deletes every rule does not get them back on the next start:

```python
        seeded = conn.execute("SELECT COUNT(*) AS n FROM schema_marks WHERE mark = 'rules_seeded'").fetchone()
        if not seeded["n"]:
            conn.executemany(
                "INSERT OR IGNORE INTO anonymization_rules(pattern, kind) VALUES (?, ?)", list(DEFAULT_RULES)
            )
            conn.execute("INSERT INTO schema_marks(mark) VALUES ('rules_seeded')")
```

with `CREATE TABLE IF NOT EXISTS schema_marks (mark TEXT PRIMARY KEY)` next to the other tables, and
`from .anonymize import DEFAULT_RULES, Rule` at the top.

Then the three methods, following `save_db_profile` exactly: insert when `rule.id is None`, update by
id otherwise, `sqlite3.IntegrityError` becomes `DuplicateProfileName(rule.pattern)`, `rowcount == 0`
becomes `ProfileGone`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv314/bin/python -m pytest -q tests/test_storage_anonymization.py && .venv314/bin/python -m pytest -q`
Expected: PASS, and the existing suite stays green.

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/storage.py tests/test_storage_anonymization.py
git commit -m "feat: anonymisierungsregeln speichern"
```

---

### Task 4: The salt

**Files:**
- Modify: `src/sqltransfer_app/secrets.py`
- Test: `tests/test_secrets_salt.py`

**Interfaces:**
- Consumes: `SecretStore`.
- Produces: `SecretStore.anonymization_salt() -> str`, creating a 32-character hex salt on first use
  and returning the same one afterwards.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_secrets_salt.py
from __future__ import annotations

from sqltransfer_app.secrets import SALT_KEY, SecretStore


class _Memory(SecretStore):
    """The real class with the Keychain swapped for a dict."""

    def __init__(self):
        super().__init__()
        self.values: dict[str, str] = {}
        self.writes = 0

    def set_secret(self, key, value):
        self.writes += 1
        self.values[key] = value

    def get_secret(self, key):
        return self.values.get(key)


def test_the_salt_is_created_once_and_reused():
    store = _Memory()
    first = store.anonymization_salt()
    assert first == store.anonymization_salt()
    assert store.writes == 1
    assert store.values[SALT_KEY] == first


def test_the_salt_is_long_random_hex():
    store = _Memory()
    salt = store.anonymization_salt()
    assert len(salt) == 32 and int(salt, 16) >= 0
    assert salt != _Memory().anonymization_salt()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv314/bin/python -m pytest -q tests/test_secrets_salt.py`
Expected: FAIL with `ImportError: cannot import name 'SALT_KEY'`

- [ ] **Step 3: Write the implementation**

```python
# src/sqltransfer_app/secrets.py
import secrets as _secrets

SALT_KEY = "anonymization:salt"


# inside SecretStore
    def anonymization_salt(self) -> str:
        """The per-installation salt, created on first use.

        Without it, anyone holding a list of real values could hash them and check
        which ones occur in the copied data. It never leaves the Keychain.
        """
        existing = self.get_secret(SALT_KEY)
        if existing:
            return existing
        salt = _secrets.token_hex(16)
        self.set_secret(SALT_KEY, salt)
        return salt
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv314/bin/python -m pytest -q tests/test_secrets_salt.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/secrets.py tests/test_secrets_salt.py
git commit -m "feat: salt für die anonymisierung im schlüsselbund"
```

---

### Task 5: Reading columns and running the statement

**Files:**
- Modify: `src/sqltransfer_app/transfer.py`
- Test: `tests/test_mysql_integration.py`, `tests/test_postgres_integration.py`

**Interfaces:**
- Consumes: `TablePlan`, `update_statement`, `SecretStore.anonymization_salt`.
- Produces: `TransferService.table_columns(profile, table) -> tuple[bool, list[Column], str]` and
  `TransferService.anonymize_table(profile, plan) -> tuple[bool, int, str]` returning
  (ok, rows affected, error).

- [ ] **Step 1: Write the failing integration tests**

The file already has `_exec(database, *statements)`, `_profile(role, database)`, `_service()` and the
`databases` fixture; add one reader next to them:

```python
# tests/test_mysql_integration.py  (append)
def _rows(database: str, sql: str) -> list[tuple]:
    import pymysql

    args = _conn_args()
    conn = pymysql.connect(host=args["host"], port=args["port"], user=args["user"],
                           password=args["password"], database=database, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())
    finally:
        conn.close()


def _anonymize(dst, table: str, rules: list[Rule]) -> tuple[bool, int, str]:
    service = _service()
    ok, columns, message = asyncio.run(service.table_columns(dst, table))
    assert ok, message
    return asyncio.run(service.anonymize_table(dst, plan_table(table, columns, rules)))


def test_anonymizing_replaces_only_the_matched_columns(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS kunde",
          "CREATE TABLE kunde (id INT PRIMARY KEY, email VARCHAR(190) UNIQUE, ort VARCHAR(80), plz VARCHAR(5), umsatz INT)",
          "INSERT INTO kunde VALUES (1,'a@axro.de','Hamburg','20095',10),"
          " (2,'b@axro.de',NULL,'10115',20), (3,'','Köln','',30)")

    ok, affected, message = _anonymize(dst, "kunde", [Rule(id=1, pattern="*mail*", kind="email"),
                                                      Rule(id=2, pattern="ort", kind="city"),
                                                      Rule(id=3, pattern="plz", kind="postcode")])
    assert ok, message
    assert affected == 3

    rows = _rows(DST_DB, "SELECT id, email, ort, plz, umsatz FROM kunde ORDER BY id")
    assert [r[4] for r in rows] == [10, 20, 30]                      # untouched column
    assert all(r[1].endswith("@example.invalid") or r[1] == "" for r in rows)
    assert "axro.de" not in str(rows)                                # nothing real left
    assert rows[1][2] is None                                        # NULL stays NULL
    assert rows[2][1] == ""                                          # empty stays empty
    assert all(re.fullmatch(r"\d{5}", r[3]) for r in rows if r[3])   # Review Focus 6: no minus


def test_the_same_value_yields_the_same_replacement(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS a", "DROP TABLE IF EXISTS b",
          "CREATE TABLE a (id INT PRIMARY KEY, email VARCHAR(190))",
          "CREATE TABLE b (id INT PRIMARY KEY, email VARCHAR(190))",
          "INSERT INTO a VALUES (1,'same@axro.de')", "INSERT INTO b VALUES (1,'same@axro.de')")
    rules = [Rule(id=1, pattern="email", kind="email")]
    for table in ("a", "b"):
        assert _anonymize(dst, table, rules)[0]
    assert _rows(DST_DB, "SELECT email FROM a")[0][0] == _rows(DST_DB, "SELECT email FROM b")[0][0]


def test_a_unique_column_stays_unique(databases):
    dst = _profile("local", DST_DB)
    values = ",".join(f"({i},'kunde{i}@axro.de')" for i in range(1, 201))
    _exec(DST_DB, "DROP TABLE IF EXISTS viele",
          "CREATE TABLE viele (id INT PRIMARY KEY, email VARCHAR(190) UNIQUE)",
          f"INSERT INTO viele VALUES {values}")

    ok, _affected, message = _anonymize(dst, "viele", [Rule(id=1, pattern="email", kind="email")])
    assert ok, message  # a collision would surface here as a duplicate key error
    assert _rows(DST_DB, "SELECT COUNT(DISTINCT email) FROM viele")[0][0] == 200


def test_a_matching_column_of_the_wrong_type_is_left_alone(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS numerisch",
          "CREATE TABLE numerisch (id INT PRIMARY KEY, telefon BIGINT)",
          "INSERT INTO numerisch VALUES (1, 491234567)")
    ok, affected, message = _anonymize(dst, "numerisch", [Rule(id=1, pattern="telefon", kind="phone")])
    assert ok and affected == 0, message
    assert _rows(DST_DB, "SELECT telefon FROM numerisch")[0][0] == 491234567
```

Mirror the first three tests in `tests/test_postgres_integration.py` against `public.kunde`, using
that file's `_admin(database, *statements)` helper and its own `databases` fixture, and assert the
phone format there as well: `re.fullmatch(r"\+49 30 \d{7}", row)`.

- [ ] **Step 2: Run them to verify they fail**

Run: `SQLTRANSFER_TEST_MYSQL=127.0.0.1:3398:root:test .venv314/bin/python -m pytest -q tests/test_mysql_integration.py -k anonymiz`
Expected: FAIL with `AttributeError: 'TransferService' object has no attribute 'table_columns'`

- [ ] **Step 3: Write the implementation**

Two `_sync` helpers plus their service methods, following `mysql_empty_table` line for line
(resolve the profile, take host and port from the tunnel when there is one, read the password from
the secret store, `asyncio.to_thread`, `finally: close_tunnel`):

```python
_COLUMNS_SQL = (
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
)


def _split_table(table: str, default_schema: str) -> tuple[str, str]:
    """'public.kunde' -> ('public', 'kunde'); a bare name uses the connected database."""
    parts = [part.strip('`"') for part in table.split(".")]
    return (parts[0], parts[1]) if len(parts) == 2 else (default_schema, parts[0])


def _table_columns_sync(
    host: str, port: int, database: str, username: str, password: str,
    db_type: str, table: str, *, tls: TlsSettings,
) -> list[Column]:
    schema, name = _split_table(table, "public" if db_type == "postgres" else database)
    if db_type == "mysql":
        conn = _mysql_connect(tls, host=host, port=port, user=username, password=password,
                              database=database, connect_timeout=5, autocommit=True)
    else:
        conn = _pg_connect(tls, host=host, port=port, user=username, password=password,
                           dbname=database, connect_timeout=5, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL, (schema, name))
            return [Column(str(row[0]), str(row[1])) for row in cur.fetchall()]
    finally:
        conn.close()


def _anonymize_table_sync(
    host: str, port: int, database: str, username: str, password: str,
    db_type: str, sql: str, params: list[str], *, tls: TlsSettings,
) -> int:
    if db_type == "mysql":
        conn = _mysql_connect(tls, host=host, port=port, user=username, password=password,
                              database=database, connect_timeout=5, read_timeout=600,
                              write_timeout=600, autocommit=True)
    else:
        conn = _pg_connect(tls, host=host, port=port, user=username, password=password,
                           dbname=database, connect_timeout=5, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.rowcount)
    finally:
        conn.close()
```

and in the service, both methods sharing the endpoint handling of `mysql_empty_table`:

```python
    async def table_columns(self, profile: DBProfile, table: str) -> tuple[bool, list[Column], str]:
        """Column names and types of one destination table: (ok, columns, error)."""
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(profile)
            host = endpoint.tunnel.local_host if endpoint.tunnel else profile.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else profile.port
            password = self.secret_store.get_secret(profile.password_secret_key) or ""
            columns = await asyncio.to_thread(
                _table_columns_sync, host, port, profile.database, profile.username, password,
                profile.db_type, table, tls=endpoint.tls,
            )
            return True, columns, ""
        except Exception as exc:  # noqa: BLE001
            return False, [], str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def anonymize_table(self, profile: DBProfile, plan: TablePlan) -> tuple[bool, int, str]:
        """Rewrite the planned columns in place: (ok, rows affected, error).

        An empty plan is a success with nothing done, so callers need no special case.
        """
        statement = update_statement(plan, profile.db_type, self.secret_store.anonymization_salt())
        if statement is None:
            return True, 0, ""
        sql, params = statement
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(profile)
            host = endpoint.tunnel.local_host if endpoint.tunnel else profile.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else profile.port
            password = self.secret_store.get_secret(profile.password_secret_key) or ""
            affected = await asyncio.to_thread(
                _anonymize_table_sync, host, port, profile.database, profile.username, password,
                profile.db_type, sql, params, tls=endpoint.tls,
            )
            return True, affected, ""
        except Exception as exc:  # noqa: BLE001
            return False, 0, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `SQLTRANSFER_TEST_MYSQL=… .venv314/bin/python -m pytest -q tests/test_mysql_integration.py`
and the PostgreSQL file with its own variable.
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/transfer.py tests/test_mysql_integration.py tests/test_postgres_integration.py
git commit -m "feat: spalten lesen und anonymisieren im ziel"
```

---

### Task 6: Wiring it into the app

**Files:**
- Modify: `src/sqltransfer_app/app.py`, `src/sqltransfer_app/ui.py`
- Test: the click-through driver, extended (see Step 1)

**Interfaces:**
- Consumes: everything above.
- Produces: no new public API; a switch, a rule list with its dialog, preview output, log output.

- [ ] **Step 1: Move the driver into the repo and extend it**

The click-through driver written for the profile work lives in a scratch directory that gets wiped.
Copy it to `tests/driver_app.py` first (it is run by hand, not by pytest, because it drives the whole
app against a temporary data directory), then add the three cases below. The service is stubbed, so
no database is involved and the ordering guarantee is what gets tested.

```python
print("\nA. The switch is on when rules exist")
switch = find(root, ft.Switch, label="Anonymize personal data")
check(switch.value is True, "on by default")

print("\nB. A rule can be added through the dialog")
click(button(root, "New rule"))
dialog = page.dialog
find(dialog, ft.TextField, label="Column pattern").value = "kundennummer"
find(dialog, ft.Dropdown, label="Replace with").value = "text"
click(button(dialog, "Save"))
check(any("kundennummer" in row for row in rows_of(rules_rows)), "the rule is listed")

print("\nC. A failing anonymization stops the swap")
calls: list[str] = []

async def ok_transfer(*_a, **_k):
    calls.append("transfer")
    return TransferResult(status="success", rows=1, elapsed_ms=1, parallel=1, message="ok")

async def failing_anonymize(*_a, **_k):
    calls.append("anonymize")
    return False, 0, "Data too long for column 'email'"

async def never_swap(*_a, **_k):
    calls.append("swap")
    return True, ""

app_module.TransferService.transfer_single_table = ok_transfer
app_module.TransferService.anonymize_table = failing_anonymize
app_module.TransferService.mysql_swap_temp_to_final = never_swap
app_module.TransferService.mysql_table_exists = lambda *_a, **_k: _async((True, ""))
app_module.TransferService.table_columns = lambda *_a, **_k: _async((True, [Column("email", "varchar")], ""))

click(button(root, "Run transfer"))
await asyncio.sleep(0.2)
click(button(page.dialog, "Overwrite on db.example.com")) if page.dialog else None
await asyncio.sleep(0.2)
check("anonymize" in calls and "swap" not in calls, f"no swap after a failed rewrite: {calls}")
check("failed" in status_text(root).lower(), "the run reports failure")
```

with a two-line helper next to the other driver helpers:

```python
def _async(value):
    async def call(*_a, **_k):
        return value
    return call
```

- [ ] **Step 2: Run the driver to verify it fails**

Run: `.venv314/bin/python tests/driver_app.py`
Expected: FAIL with `not found: Switch {'label': 'Anonymize personal data'}`

- [ ] **Step 3: Implement the wiring**

1. Controls: `anonymize_switch = ft.Switch(label="Anonymize personal data", value=True)`, placed next
   to the scope choice; `rules_rows`, `rules_search`, `rules_new_button`, `rule_pattern`,
   `rule_kind`, `rule_enabled`, `rule_save_button`, `rule_dialog_title`, `rule_form_state`.
2. A third `profile_tile("Anonymization rules", …)` in the Profiles section, with
   `open_rule_dialog(rule_id)` / `save_rule` / `ask_delete_rule` written exactly like the database
   profile ones.
3. `async def anonymize_plan(profile, table)` helper: `table_columns` → `plan_table` → log the
   targets, log every `skipped` entry as WARN.
4. In the MySQL loop: in the swap branch, call it on `temp_name` **before**
   `apply_source_indexes(temp_name, final_name)`; in the in-place branch, on `final_name` after the
   replace. On failure call `fail(...)` and `return`, so no swap happens.
5. In the PostgreSQL branch: after `transfer_scope`, for every table of the scope.
6. In `preview_plan_task`: read the source columns for the scope's tables and log what would be
   replaced, before anything moves.
7. The switch gates all of it; when off, log one line `anonymization off` so the log stays honest.

- [ ] **Step 4: Run the driver and the suite**

Run: `.venv314/bin/python tests/driver_app.py && .venv314/bin/python -m pytest -q`
Expected: all checks pass, suite green.

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/app.py src/sqltransfer_app/ui.py tests/driver_app.py
git commit -m "feat: schalter, regelliste und bericht für die anonymisierung"
```

---

### Task 7: Documentation and version

**Files:**
- Modify: `docs/benutzerhandbuch.md`, `docs/user-guide.md`, `README.md`,
  `docs/design/2026-10-01-anonymization.md`, `pyproject.toml`, `src/sqltransfer_app/__init__.py`

- [ ] **Step 1: Write the user-facing documentation**

A section in both guides, after "Eine Übertragung durchführen" / "Running a transfer": what the
switch does, where the rules live, how to add one, what the WARN lines mean, and the honest limit
that real data exists briefly and that a free-text column is not covered.

- [ ] **Step 2: Update the README feature list**

One bullet: deterministic anonymization of personal columns, rules editable in the app.

- [ ] **Step 3: Mark the design document as implemented**

Change its status line to `Status: implemented in 1.3.0.`

- [ ] **Step 4: Raise the version**

```bash
.venv314/bin/python scripts/bump_version.py 1.3.0
.venv314/bin/python -m pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add docs README.md pyproject.toml src/sqltransfer_app/__init__.py
git commit -m "docs: anonymisierung beschrieben"
git commit -m "chore: version 1.3.0"
```

---

## After the plan

Build and verify the app by hand once (`scripts/release.sh` without `--publish` is overkill here;
`flet build macos` plus a run against a throwaway data directory is enough), then the usual path:
PR into `develop`, PR into `main`, build, `gh release create`.
