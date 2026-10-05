# Table Definition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:executing-plans` (inline).
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** A created or swapped destination table carries the source's generated columns,
defaults, check constraints and `AUTO_INCREMENT`.

**Architecture:** `transfer.py` reads the definition of every table in scope in one round
trip and turns it into ordered `ALTER TABLE` clauses; `app.py` applies them to the temp
table after the indexes and before the swap, failing the run if one does not apply.

**Tech Stack:** Python 3.12+, pymysql, psycopg 3, MySQL 8 and PostgreSQL
`information_schema`. No new dependency.

**Spec:** `docs/design/2026-10-02-table-definition.md`.

## Global Constraints

- Comments in English, commit messages in German, lowercase after the colon.
- Identifiers quoted with the existing helpers; expressions from `information_schema` are
  server-generated SQL and are inserted as they come, never built from user input.
- The order is fixed: indexes, generated columns, defaults, `AUTO_INCREMENT`, checks.
- A failed clause fails the run. No swap, no half-restored table.
- MySQL carries all four properties; PostgreSQL carries defaults and checks, and reports
  generated and identity columns.
- Every task ends with a commit, suite green before each.

## Review Focus

1. A table with none of the four properties must produce no statements at all.
2. A default that is a function (`CURRENT_TIMESTAMP`) must not be quoted like a string.
3. A check constraint referring to a column that is not in the copy must be reported, not
   abort the run.
4. `AUTO_INCREMENT` on a column without a key must not be attempted before the index step.
5. A generated column whose expression references another generated column must still work.

---

### Task 1: Building the clauses

**Files:** Create `tests/test_table_definition_ddl.py`; modify `src/sqltransfer_app/transfer.py`.

**Interfaces:** `build_definition_clauses(columns, checks, auto_increment, db_type) -> list[str]`
where `columns` is a list of dicts with `name`, `type`, `default`, `extra`,
`generation_expression`, `is_nullable`; `checks` is a list of `(name, expression)`;
`auto_increment` is an int or None. Returns the `ALTER TABLE` fragments in application
order, without the `ALTER TABLE <table>` prefix.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_table_definition_ddl.py
from __future__ import annotations

import pytest

from sqltransfer_app.transfer import build_definition_clauses


def _column(name, type_="varchar(20)", default=None, extra="", expression=None, nullable="YES"):
    return {"name": name, "type": type_, "default": default, "extra": extra,
            "generation_expression": expression, "is_nullable": nullable}


def test_a_plain_table_needs_nothing():
    assert build_definition_clauses([_column("name")], [], None, "mysql") == []


def test_a_generated_column_comes_before_a_default():
    clauses = build_definition_clauses(
        [_column("tag", "date", extra="STORED GENERATED", expression="cast(`zeit` as date)"),
         _column("betrag", "decimal(10,2)", default="1.00")],
        [], None, "mysql")
    assert clauses == [
        "MODIFY COLUMN `tag` date GENERATED ALWAYS AS (cast(`zeit` as date)) STORED",
        "ALTER COLUMN `betrag` SET DEFAULT '1.00'",
    ]


def test_a_virtual_generated_column_keeps_being_virtual():
    clauses = build_definition_clauses(
        [_column("k", "varchar(32)", extra="VIRTUAL GENERATED", expression="concat('k-',`id`)")],
        [], None, "mysql")
    assert clauses == ["MODIFY COLUMN `k` varchar(32) GENERATED ALWAYS AS (concat('k-',`id`)) VIRTUAL"]


def test_a_function_default_is_not_quoted():
    clauses = build_definition_clauses(
        [_column("angelegt", "datetime", default="CURRENT_TIMESTAMP", extra="DEFAULT_GENERATED")],
        [], None, "mysql")
    assert clauses == ["ALTER COLUMN `angelegt` SET DEFAULT CURRENT_TIMESTAMP"]


def test_auto_increment_carries_its_counter_and_comes_after_the_defaults():
    clauses = build_definition_clauses(
        [_column("lauf", "bigint unsigned", extra="auto_increment", nullable="NO"),
         _column("betrag", "decimal(10,2)", default="1.00")],
        [], 42, "mysql")
    assert clauses == [
        "ALTER COLUMN `betrag` SET DEFAULT '1.00'",
        "MODIFY COLUMN `lauf` bigint unsigned NOT NULL AUTO_INCREMENT",
        "AUTO_INCREMENT = 42",
    ]


def test_checks_come_last():
    clauses = build_definition_clauses([_column("betrag", "decimal(10,2)", default="1.00")],
                                       [("probe_chk_1", "(`betrag` > 0)")], None, "mysql")
    assert clauses[-1] == "ADD CONSTRAINT `probe_chk_1` CHECK ((`betrag` > 0))"


def test_postgres_takes_defaults_and_checks_only():
    clauses = build_definition_clauses(
        [_column("tag", "date", extra="", expression="(zeit)::date"),
         _column("betrag", "numeric(10,2)", default="1.00")],
        [("probe_chk_1", "(betrag > 0)")], None, "postgres")
    assert clauses == [
        'ALTER COLUMN "betrag" SET DEFAULT \'1.00\'',
        'ADD CONSTRAINT "probe_chk_1" CHECK ((betrag > 0))',
    ]
```

- [ ] **Step 2: Run it** — `.venv314/bin/python -m pytest -q tests/test_table_definition_ddl.py`;
  expected FAIL with `ImportError: cannot import name 'build_definition_clauses'`.
- [ ] **Step 3: Implement** `build_definition_clauses` in `transfer.py`, next to
  `build_mysql_index_clauses`: generated columns first (MySQL only, `MODIFY COLUMN` with the
  expression as the server reports it), then defaults (quoted unless `extra` says
  `DEFAULT_GENERATED` or the value is a bare keyword), then `AUTO_INCREMENT` plus the counter,
  then checks. PostgreSQL skips the first and third group.
- [ ] **Step 4: Run it again** — expected PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: ddl-bausteine für die tabellendefinition"`.

---

### Task 2: Reading the definition from the source

**Files:** modify `src/sqltransfer_app/transfer.py`; test in `tests/test_mysql_integration.py`
and `tests/test_postgres_integration.py`.

**Interfaces:** `TransferService.source_table_definitions(source, tables) -> tuple[bool, dict[str, list[str]], str]`,
clauses keyed by bare table name, one round trip for the whole run.

- [ ] **Step 1: Write the failing integration test**

```python
def test_table_definitions_are_read_for_every_table_in_scope(databases):
    service = _service()
    src = _profile("remote", SRC_DB)
    _exec(SRC_DB, "DROP TABLE IF EXISTS def_a", "DROP TABLE IF EXISTS def_b",
          """CREATE TABLE def_a (
               id INT PRIMARY KEY,
               lauf BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE,
               zeit DATETIME NOT NULL,
               tag DATE AS (CAST(zeit AS DATE)) STORED,
               betrag DECIMAL(10,2) DEFAULT 1.00,
               geprueft INT CHECK (geprueft > 0))""",
          "CREATE TABLE def_b (id INT PRIMARY KEY, name VARCHAR(20))",
          "INSERT INTO def_a (id, pflicht_los, zeit) VALUES (1,'x','2026-01-01 10:00:00')"
          .replace("pflicht_los, ", ""))

    ok, clauses, message = asyncio.run(service.source_table_definitions(src, [f"{SRC_DB}.def_a", f"{SRC_DB}.def_b"]))
    assert ok, message
    assert "def_b" not in clauses                      # nothing to restore there
    joined = " | ".join(clauses["def_a"])
    assert "GENERATED ALWAYS AS" in joined and "SET DEFAULT" in joined
    assert "AUTO_INCREMENT" in joined and "CHECK" in joined
    assert joined.index("GENERATED") < joined.index("AUTO_INCREMENT") < joined.index("CHECK")
```

- [ ] **Step 2: Run it** — expected FAIL with `AttributeError: … 'source_table_definitions'`.
- [ ] **Step 3: Implement** the sync helper and the service method in the established shape
  (resolve profile, tunnel host and port, `asyncio.to_thread`, close the tunnel), querying
  `information_schema.columns`, `information_schema.check_constraints` joined with
  `table_constraints`, and `information_schema.tables` for the counter, for all schemas in scope.
- [ ] **Step 4: Run it again** — expected PASS, plus the PostgreSQL mirror asserting defaults
  and checks only.
- [ ] **Step 5: Commit** — `git commit -m "feat: tabellendefinition der quelle lesen"`.

---

### Task 3: Applying it before the swap

**Files:** modify `src/sqltransfer_app/transfer.py`, `src/sqltransfer_app/app.py`; test in
`tests/driver_app.py`.

**Interfaces:** `TransferService.apply_definition_clauses(destination, table, clauses) -> tuple[bool, str]`.

- [ ] **Step 1: Write the failing driver check**

```python
print("\n9j. The table definition is restored before the swap")
order: list[str] = []

async def record_definition(_self, _dst, table, clauses):
    order.append(f"definition:{table}")
    return True, ""

async def record_swap(_self, _dst, temp_table, final_table):
    order.append("swap")
    return True, ""

app_module.TransferService.source_table_definitions = _async((True, {"kunde": ["AUTO_INCREMENT = 7"]}, ""))
app_module.TransferService.apply_definition_clauses = record_definition
app_module.TransferService.mysql_swap_temp_to_final = record_swap
click(button(root, "Run transfer"))
await asyncio.sleep(0.2)
check(order and order[0].startswith("definition:") and order[-1] == "swap",
      f"definition first, then the swap: {order}")

async def failing_definition(_self, _dst, _table, _clauses):
    return False, "Unknown column 'weg' in generated column"

app_module.TransferService.apply_definition_clauses = failing_definition
order.clear()
click(button(root, "Run transfer"))
await asyncio.sleep(0.2)
check("swap" not in order, f"a failed restore stops the swap: {order}")
```

- [ ] **Step 2: Run the driver** — expected FAIL with `not found: Switch`-style
  `AttributeError` on `source_table_definitions`.
- [ ] **Step 3: Implement**: the service method running the clauses in order against one
  table; in `app.py`, read the definitions once per run next to the index read, and in the
  swap branch apply them to the temp table **after** `apply_source_indexes` and before
  `mysql_swap_temp_to_final`, failing the run on error. The in-place branch skips it.
- [ ] **Step 4: Run the driver and the suite** — expected all green.
- [ ] **Step 5: Commit** — `git commit -m "feat: tabellendefinition vor dem tausch wiederherstellen"`.

---

### Task 4: End to end

**Files:** `tests/test_mysql_integration.py`, `tests/test_postgres_integration.py`.

- [ ] **Step 1: Write the failing test**

```python
def _normalised_ddl(database: str, table: str) -> str:
    ddl = _rows(database, f"SHOW CREATE TABLE {table}")[0][1]
    ddl = re.sub(r"AUTO_INCREMENT=\d+", "AUTO_INCREMENT=N", ddl)
    return re.sub(r"CONSTRAINT `[^`]+` CHECK", "CONSTRAINT `chk` CHECK", ddl)


def test_the_copy_carries_the_source_definition(databases):
    _exec(SRC_DB, "DROP TABLE IF EXISTS voll", """CREATE TABLE voll (
            id INT PRIMARY KEY,
            lauf BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE,
            zeit DATETIME NOT NULL,
            tag DATE AS (CAST(zeit AS DATE)) STORED,
            betrag DECIMAL(10,2) DEFAULT 1.00,
            geprueft INT CHECK (geprueft > 0),
            KEY idx_tag (tag, betrag))""",
          "INSERT INTO voll (id, zeit, geprueft) VALUES (1,'2026-01-01 10:00:00',3)")
    _exec(DST_DB, "DROP TABLE IF EXISTS voll")

    _run_table_like_the_app("voll")   # the app's flow: transfer, indexes, definition, swap

    assert _normalised_ddl(DST_DB, "voll") == _normalised_ddl(SRC_DB, "voll").replace(SRC_DB, DST_DB)

    # and it behaves like the original
    _exec(DST_DB, "INSERT INTO voll (id, zeit, geprueft) VALUES (2,'2026-02-02 11:00:00',5)")
    rows = _rows(DST_DB, "SELECT tag, betrag FROM voll WHERE id = 2")
    assert str(rows[0][0]) == "2026-02-02" and float(rows[0][1]) == 1.00
    with pytest.raises(Exception):
        _exec(DST_DB, "INSERT INTO voll (id, zeit, geprueft) VALUES (3,'2026-03-03 12:00:00',0)")
```

- [ ] **Step 2: Run it**, extend `_run_table_like_the_app` with the definition step so the
  helper mirrors what `app.py` does, and implement whatever is missing.
- [ ] **Step 3: Run both integration suites** — expected green except the five known
  PostgreSQL TLS tests.
- [ ] **Step 4: Commit** — `git commit -m "test: tabellendefinition gegen echte datenbanken"`.

---

### Task 5: Documentation and version

- [ ] **Step 1:** Both user guides: the paragraph about what a swap loses gets rewritten to
  say what is restored now, and what still is not (triggers, views, partitioning, comments,
  PostgreSQL generated columns).
- [ ] **Step 2:** `README.md` feature bullet.
- [ ] **Step 3:** The design document's status becomes `implemented in 1.5.0`.
- [ ] **Step 4:** `python scripts/bump_version.py 1.5.0`, suite green.
- [ ] **Step 5:** Commit documentation and version separately.
