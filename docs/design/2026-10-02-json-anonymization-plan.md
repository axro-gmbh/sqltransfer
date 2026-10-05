# JSON Anonymization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:executing-plans` (inline) or
> `superpowers:subagent-driven-development` to implement this plan task by task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Replace personal values **inside** JSON columns, keeping the structure, using the rules and
the salted hash that already exist for plain columns.

**Architecture:** `anonymize.py` grows a second planning pass: given the key paths a column actually
contains, it decides which to rewrite, which to descend into and which to report. `transfer.py`
discovers those paths level by level with one query per level. `app.py` feeds the discovery with the
columns a rule marks as JSON, plus every native `json`/`jsonb` column, and reports what happened.

**Tech Stack:** Python 3.12+, pymysql, psycopg 3, MySQL 8 `JSON_KEYS`/`JSON_TABLE`/`JSON_REPLACE`,
PostgreSQL `jsonb_object_keys`/`jsonb_set`. No new dependency.

**Spec:** `docs/design/2026-10-02-json-anonymization.md`, which builds on
`docs/design/2026-10-01-anonymization.md`.

## Global Constraints

- Comments in English, UI strings in English, commit messages in German, lowercase after the colon.
- JSON paths are written with every key quoted: `$."email"`, `$."adresse"."ort"`. A key containing a
  quote has it doubled for MySQL; PostgreSQL takes the key list as `text[]` and needs no quoting.
- Only values of JSON type `STRING` are rewritten. `OBJECT` is descended into (to depth 4), `ARRAY` is
  reported and not followed, anything else is reported as a non-string value.
- `NULL` columns, rows without the key and invalid JSON are left alone; the run continues.
- The salt reaches SQL as a parameter, once per placeholder, as in `update_statement` today.
- Every task ends with a commit; the suite is green before each commit.

## Review Focus

1. **A key that is a string in one row and an object in another** must not produce a broken statement
   → Task 1 decides per key from all observed types.
2. **A key name containing a quote, a dot or a space** must survive path building in both dialects
   → Task 2.
3. **A text column a rule marks as JSON but that holds prose** must be reported, not abort the run
   → Task 3.
4. **A column whose JSON is one big array** must be reported once, not descended into → Task 1.
5. **Discovery cost**: one query per level per column, not per row; a table of 100k rows must not
   produce 100k queries → Task 3.

---

### Task 1: Deciding what happens to each path

**Files:**
- Modify: `src/sqltransfer_app/anonymize.py`
- Test: `tests/test_anonymize_json.py`

**Interfaces:**
- Produces: `JsonNode(path: tuple[str, ...], types: frozenset[str])`,
  `JsonTarget(column: str, path: tuple[str, ...], kind: str)`,
  `plan_json_column(column, nodes, rules) -> tuple[tuple[JsonTarget, ...], tuple[tuple[str, str], ...], tuple[tuple[str, ...], ...]]`
  returning (targets, skipped as (path text, reason), paths to descend into).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_anonymize_json.py
from __future__ import annotations

from sqltransfer_app.anonymize import JsonNode, JsonTarget, Rule, plan_json_column

RULES = [Rule(id=1, pattern="*mail*", kind="email"), Rule(id=2, pattern="ort", kind="city")]


def _node(path, *types):
    return JsonNode(path=path, types=frozenset(types))


def test_a_string_under_a_matching_key_is_rewritten():
    targets, skipped, descend = plan_json_column("custom_fields", [_node(("email",), "STRING")], RULES)
    assert targets == (JsonTarget("custom_fields", ("email",), "email"),)
    assert skipped == () and descend == ()


def test_an_object_is_descended_into_whether_or_not_it_matches():
    targets, skipped, descend = plan_json_column(
        "custom_fields", [_node(("adresse",), "OBJECT"), _node(("mail_data",), "OBJECT")], RULES
    )
    assert targets == ()
    assert descend == (("adresse",), ("mail_data",))
    assert skipped == ()


def test_an_array_is_reported_once_and_not_followed():
    targets, skipped, descend = plan_json_column("custom_fields", [_node(("positions",), "ARRAY")], RULES)
    assert targets == () and descend == ()
    assert skipped == (("custom_fields$.\"positions\"", "array, not followed"),)


def test_a_non_string_value_under_a_matching_key_is_reported():
    _t, skipped, _d = plan_json_column("custom_fields", [_node(("email",), "INTEGER")], RULES)
    assert skipped == (("custom_fields$.\"email\"", "not a string value (INTEGER)"),)


def test_a_key_seen_as_string_and_as_null_is_still_rewritten():
    # A row where the key is JSON null must not disqualify the key everywhere.
    targets, skipped, _d = plan_json_column("custom_fields", [_node(("email",), "STRING", "NULL")], RULES)
    assert targets == (JsonTarget("custom_fields", ("email",), "email"),)
    assert skipped == ()


def test_a_key_seen_as_string_and_as_object_is_rewritten_and_descended():
    targets, _s, descend = plan_json_column("custom_fields", [_node(("daten",), "STRING", "OBJECT")], RULES)
    assert descend == (("daten",),)
    assert targets == ()  # no rule matches "daten"


def test_nested_paths_keep_their_parents():
    nodes = [_node(("adresse", "ort"), "STRING"), _node(("adresse", "nummer"), "INTEGER")]
    targets, skipped, _d = plan_json_column("custom_fields", nodes, RULES)
    assert targets == (JsonTarget("custom_fields", ("adresse", "ort"), "city"),)
    assert skipped == ()  # "nummer" matches no rule, so its type is nobody's business
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_json.py`
Expected: FAIL with `ImportError: cannot import name 'JsonNode'`

- [ ] **Step 3: Write the implementation**

```python
@dataclass(frozen=True, slots=True)
class JsonNode:
    """One key path seen in a column, with every JSON type observed for it."""

    path: tuple[str, ...]
    types: frozenset[str]


@dataclass(frozen=True, slots=True)
class JsonTarget:
    column: str
    path: tuple[str, ...]
    kind: str


def json_path_text(column: str, path: tuple[str, ...]) -> str:
    """How a path is written in the log and in MySQL: column$."a"."b"."""
    return column + "$" + "".join('."' + key.replace('"', '""') + '"' for key in path)


def plan_json_column(column: str, nodes, rules):
    targets: list[JsonTarget] = []
    skipped: list[tuple[str, str]] = []
    descend: list[tuple[str, ...]] = []
    for node in nodes:
        text = json_path_text(column, node.path)
        if "OBJECT" in node.types:
            descend.append(node.path)
        if "ARRAY" in node.types:
            skipped.append((text, "array, not followed"))
        rule = match_rule(node.path[-1], rules)
        if not rule:
            continue
        if "STRING" in node.types:
            targets.append(JsonTarget(column, node.path, rule.kind))
        elif not ({"OBJECT", "ARRAY"} & node.types):
            other = sorted(t for t in node.types if t != "NULL") or ["NULL"]
            skipped.append((text, f"not a string value ({other[0]})"))
    return tuple(targets), tuple(skipped), tuple(descend)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_json.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/anonymize.py tests/test_anonymize_json.py
git commit -m "feat: entscheidung, welche json-schlüssel ersetzt werden"
```

---

### Task 2: The statement for JSON paths

**Files:**
- Modify: `src/sqltransfer_app/anonymize.py`
- Test: `tests/test_anonymize_json_sql.py`

**Interfaces:**
- Consumes: `JsonTarget`, the existing `_replacement`, `TablePlan`.
- Produces: `TablePlan` gains `json_targets: tuple[JsonTarget, ...] = ()`; `update_statement` emits the
  JSON assignments alongside the column ones.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_anonymize_json_sql.py
from __future__ import annotations

import pytest

from sqltransfer_app.anonymize import JsonTarget, TablePlan, update_statement


def _plan(*json_targets: JsonTarget, table: str = "kunde") -> TablePlan:
    return TablePlan(table=table, targets=(), skipped=(), json_targets=json_targets)


def test_mysql_rewrites_one_path_in_place():
    sql, params = update_statement(_plan(JsonTarget("custom_fields", ("email",), "email")), "mysql", "s")
    assert sql.startswith("UPDATE `kunde` SET `custom_fields` = JSON_REPLACE(`custom_fields`, '$.\"email\"'")
    assert "JSON_UNQUOTE(JSON_EXTRACT(`custom_fields`, '$.\"email\"'))" in sql
    assert params == ["s"] * sql.count("%s")


def test_mysql_nests_several_paths_of_one_column():
    sql, _ = update_statement(
        _plan(JsonTarget("custom_fields", ("email",), "email"),
              JsonTarget("custom_fields", ("adresse", "ort"), "city")), "mysql", "s")
    assert sql.count("JSON_REPLACE(") == 2
    assert '$."adresse"."ort"' in sql
    assert sql.count("SET `custom_fields` =") == 1  # one assignment, not two


def test_postgres_uses_jsonb_set_and_keeps_rows_without_the_key():
    sql, _ = update_statement(_plan(JsonTarget("custom_fields", ("email",), "email")), "postgres", "s")
    assert 'jsonb_set(' in sql and "'{email}'" in sql
    assert "#>> '{email}'" in sql           # the original value, as text
    assert "? 'email'" in sql or "jsonb_path_exists" in sql  # only where the key exists


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_a_quote_or_dot_in_a_key_survives(db_type):
    sql, _ = update_statement(_plan(JsonTarget("f", ('we"ird.key',), "text")), db_type, "s")
    assert 'we""ird.key' in sql if db_type == "mysql" else 'we"ird.key' in sql


@pytest.mark.parametrize("db_type", ["mysql", "postgres"])
def test_columns_and_json_paths_live_in_one_statement(db_type):
    plan = TablePlan(table="kunde", targets=(("email", "email", None),), skipped=(),
                     json_targets=(JsonTarget("custom_fields", ("email",), "email"),))
    sql, params = update_statement(plan, db_type, "s")
    assert sql.count("SET ") == 1 and "`email` = CASE" in sql if db_type == "mysql" else True
    assert "custom_fields" in sql and params == ["s"] * sql.count("%s")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_json_sql.py`
Expected: FAIL with `TypeError: TablePlan.__init__() got an unexpected keyword argument 'json_targets'`

- [ ] **Step 3: Write the implementation**

Add `json_targets: tuple[JsonTarget, ...] = ()` to `TablePlan`, then in `update_statement` group the
JSON targets by column and wrap them:

```python
def _mysql_json_path(path: tuple[str, ...]) -> str:
    return "$" + "".join('."' + key.replace('"', '""') + '"' for key in path)


def _json_assignment(column: str, targets: list[JsonTarget], db_type: str) -> str:
    quoted = _quote(column, db_type)
    expression = quoted
    for target in targets:
        if db_type == "mysql":
            path = _mysql_json_path(target.path).replace("'", "''")
            current = f"JSON_UNQUOTE(JSON_EXTRACT({quoted}, '{path}'))"
            expression = f"JSON_REPLACE({expression}, '{path}', {_replacement(target.kind, current, db_type)})"
        else:
            keys = "{" + ",".join(key.replace("\\", "\\\\").replace(",", "\\,") for key in target.path) + "}"
            current = f"({quoted} #>> '{keys}')"
            replacement = _replacement(target.kind, current, db_type)
            expression = (
                f"CASE WHEN {quoted} #> '{keys}' IS NULL THEN {expression} "
                f"ELSE jsonb_set({expression}, '{keys}', to_jsonb({replacement})) END"
            )
    return f"{quoted} = {expression}"
```

and append those assignments to the ones `update_statement` already builds, keeping
`params = [salt] * sql.count("%s")`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_json_sql.py tests/test_anonymize_sql.py`
Expected: PASS, including the existing column tests.

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/anonymize.py tests/test_anonymize_json_sql.py
git commit -m "feat: json-pfade im update ersetzen"
```

---

### Task 3: Discovering the paths in the database

**Files:**
- Modify: `src/sqltransfer_app/transfer.py`
- Test: `tests/test_mysql_integration.py`, `tests/test_postgres_integration.py`

**Interfaces:**
- Produces: `TransferService.json_paths(profile, table, column, max_depth=4) -> tuple[bool, list[JsonNode], str]`,
  discovering level by level: one query per level, each returning (key, JSON type) pairs over all rows.

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/test_mysql_integration.py  (append)
def test_json_paths_are_discovered_level_by_level(databases):
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS mitjson",
          "CREATE TABLE mitjson (id INT PRIMARY KEY, custom_fields JSON)",
          """INSERT INTO mitjson VALUES
             (1, '{"email":"a@axro.de","adresse":{"ort":"Hamburg","nummer":7},"positionen":[{"email":"x@y.de"}]}'),
             (2, '{"email":"b@axro.de","notiz":"ohne"}'),
             (3, NULL)""")

    ok, nodes, message = asyncio.run(service.json_paths(dst, "mitjson", "custom_fields"))
    assert ok, message
    found = {node.path: node.types for node in nodes}
    assert found[("email",)] == frozenset({"STRING"})
    assert found[("adresse",)] == frozenset({"OBJECT"})
    assert found[("adresse", "ort")] == frozenset({"STRING"})
    assert found[("adresse", "nummer")] == frozenset({"INTEGER"})
    assert found[("positionen",)] == frozenset({"ARRAY"})
    assert ("positionen", "email") not in found   # arrays are not followed


def test_json_discovery_costs_one_query_per_level(databases):
    # 2000 rows must not mean 2000 queries: the guard is the runtime, measured
    # against a table that would take minutes row by row.
    service = _service()
    dst = _profile("local", DST_DB)
    rows = ",".join(f"({i}, '{{\"email\":\"k{i}@axro.de\"}}')" for i in range(1, 2001))
    _exec(DST_DB, "DROP TABLE IF EXISTS vieljson",
          "CREATE TABLE vieljson (id INT PRIMARY KEY, custom_fields JSON)",
          f"INSERT INTO vieljson VALUES {rows}")
    started = time.monotonic()
    ok, nodes, message = asyncio.run(service.json_paths(dst, "vieljson", "custom_fields"))
    assert ok, message
    assert [n.path for n in nodes] == [("email",)]
    assert time.monotonic() - started < 5


def test_a_text_column_that_is_not_json_is_refused_cleanly(databases):
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS keinjson",
          "CREATE TABLE keinjson (id INT PRIMARY KEY, notiz LONGTEXT)",
          "INSERT INTO keinjson VALUES (1, 'das ist kein json')")
    ok, nodes, message = asyncio.run(service.json_paths(dst, "keinjson", "notiz"))
    assert not ok and not nodes
    assert "json" in message.lower()
```

Mirror the first and third test in `tests/test_postgres_integration.py` with `jsonb`.

- [ ] **Step 2: Run them to verify they fail**

Run: `SQLTRANSFER_TEST_MYSQL=127.0.0.1:3398:root:test .venv314/bin/python -m pytest -q tests/test_mysql_integration.py -k json`
Expected: FAIL with `AttributeError: 'TransferService' object has no attribute 'json_paths'`

- [ ] **Step 3: Write the implementation**

A sync helper that walks the levels, and a service method around it in the established shape
(resolve profile, tunnel host and port, password, `asyncio.to_thread`, close the tunnel):

```python
_MYSQL_JSON_LEVEL_SQL = """
SELECT DISTINCT jt.k, JSON_TYPE(JSON_EXTRACT(t.{column}, CONCAT({parent_sql}, '.\"', REPLACE(jt.k, '"', '""'), '\"')))
  FROM {table} t,
       JSON_TABLE(JSON_KEYS(t.{column}, {parent_sql}), '$[*]' COLUMNS (k VARCHAR(190) PATH '$')) jt
 WHERE t.{column} IS NOT NULL
"""

_PG_JSON_LEVEL_SQL = """
SELECT DISTINCT k, jsonb_typeof(t.{column}::jsonb #> ({parent}::text[] || ARRAY[k]))
  FROM {table} t, LATERAL jsonb_object_keys(t.{column}::jsonb #> {parent}::text[]) k
 WHERE t.{column} IS NOT NULL
"""
```

MySQL reports types as `STRING`, `OBJECT`, `ARRAY`, `INTEGER`, …; PostgreSQL as `string`, `object`,
`array`, `number`. Normalise to the MySQL spelling (upper case) so Task 1 sees one vocabulary. Before
the first level, check the column parses as JSON at all (`JSON_VALID` / a `::jsonb` cast in a
`SELECT … LIMIT 1`), and return `(False, [], "column … does not hold valid JSON")` when it does not.
Stop at `max_depth`; when a level still yields objects at that depth, add a node with types
`{"OBJECT"}` and let Task 4 report it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the MySQL command above, then the PostgreSQL file with its own variable.
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/transfer.py tests/test_mysql_integration.py tests/test_postgres_integration.py
git commit -m "feat: json-pfade in der datenbank ermitteln"
```

---

### Task 4: Wiring it into the run

**Files:**
- Modify: `src/sqltransfer_app/anonymize.py`, `src/sqltransfer_app/app.py`
- Test: `tests/driver_app.py`, `tests/test_anonymize_rules.py`

**Interfaces:**
- Consumes: `plan_json_column`, `json_paths`, `update_statement`.
- Produces: the rule kind `json`; `plan_table` returns the columns to look into as a third element.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_anonymize_rules.py  (append)
def test_a_json_rule_marks_a_column_for_looking_into_it():
    from sqltransfer_app.anonymize import Column, plan_table, Rule

    columns = [Column("custom_fields", "longtext"), Column("payload", "json"), Column("notiz", "text")]
    plan = plan_table("kunde", columns, [Rule(id=1, pattern="custom_fields", kind="json")])
    assert plan.json_columns == ("custom_fields", "payload")  # the rule, plus the native type
    assert plan.targets == ()                                  # a json column is never replaced whole
```

```python
# tests/driver_app.py  (new section)
print("\n9i. A JSON column is looked into, not overwritten")
app_module.TransferService.json_paths = _async((True, [JsonNode(("email",), frozenset({"STRING"}))], ""))
app_module.TransferService.table_columns = _async((True, [Column("custom_fields", "json")], ""))
captured: list = []

async def record_plan(_self, _profile, plan):
    captured.append(plan)
    return True, 1, ""

app_module.TransferService.anonymize_table = record_plan
click(button(root, "Run transfer"))
await asyncio.sleep(0.2)
check(captured and captured[-1].json_targets, f"the plan carries a JSON target: {captured[-1:]}")
check(captured[-1].targets == (), "and does not replace the column as a whole")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv314/bin/python -m pytest -q tests/test_anonymize_rules.py -k json_rule` and
`.venv314/bin/python tests/driver_app.py`
Expected: FAIL with `AttributeError: 'TablePlan' object has no attribute 'json_columns'`

- [ ] **Step 3: Write the implementation**

1. `KINDS` gains `("json", "Look inside the JSON")`; `TablePlan` gains
   `json_columns: tuple[str, ...] = ()`.
2. `plan_table`: a column whose rule kind is `json`, or whose `data_type` is `json`/`jsonb`, goes to
   `json_columns` and never to `targets`.
3. `anonymize_step` in `app.py`: after `plan_table`, for every column in `plan.json_columns` call
   `json_paths`, feed the nodes to `plan_json_column`, extend the plan's `json_targets` and `skipped`.
   A discovery failure is a WARN for that column, not a failed run: the column is left alone and said
   so, because a prose column marked as JSON must not stop a transfer.
4. The log line names the JSON paths alongside the columns, using `json_path_text`.
5. `preview_plan_task` does the same against the source and prints the paths it would rewrite.

- [ ] **Step 4: Run both to verify they pass**

Run: `.venv314/bin/python -m pytest -q && .venv314/bin/python tests/driver_app.py`
Expected: suite green, all driver checks pass.

- [ ] **Step 5: Commit**

```bash
git add src/sqltransfer_app/anonymize.py src/sqltransfer_app/app.py tests/test_anonymize_rules.py tests/driver_app.py
git commit -m "feat: json-spalten im lauf durchsuchen und ersetzen"
```

---

### Task 5: End to end against real servers

**Files:**
- Modify: `tests/test_mysql_integration.py`, `tests/test_postgres_integration.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_values_inside_json_are_replaced_and_the_structure_survives(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS jsonkunde",
          "CREATE TABLE jsonkunde (id INT PRIMARY KEY, email VARCHAR(190), custom_fields JSON)",
          """INSERT INTO jsonkunde VALUES
             (1,'max@axro.de','{"email":"max@axro.de","adresse":{"ort":"Hamburg"},"umsatz":42}'),
             (2,'erika@axro.de','{"notiz":"ohne mail"}'),
             (3,'leer@axro.de', NULL)""")

    ok, affected, message = _anonymize(dst, "jsonkunde", [Rule(id=1, pattern="*mail*", kind="email"),
                                                          Rule(id=2, pattern="ort", kind="city"),
                                                          Rule(id=3, pattern="custom_fields", kind="json")])
    assert ok, message
    rows = _rows(DST_DB, "SELECT id, email, custom_fields FROM jsonkunde ORDER BY id")
    first = json.loads(rows[0][2])
    assert first["email"].endswith("@example.invalid")
    assert first["email"] == rows[0][1]          # same value in column and JSON, same fake
    assert first["umsatz"] == 42                 # untouched sibling
    assert first["adresse"]["ort"] != "Hamburg"  # nested replaced
    assert json.loads(rows[1][2]) == {"notiz": "ohne mail"}
    assert rows[2][2] is None
    assert "axro.de" not in str(rows)
```

Mirror it in PostgreSQL with `jsonb`, and add the array case: a column holding
`{"positionen":[{"email":"x@y.de"}]}` keeps its array and the run reports it.

- [ ] **Step 2: Run them to verify they fail**, then implement whatever the earlier tasks missed.
- [ ] **Step 3: Run the whole suite with both servers**

Run: `SQLTRANSFER_TEST_MYSQL=… SQLTRANSFER_TEST_POSTGRES=… .venv314/bin/python -m pytest -q`
Expected: green except the five known PostgreSQL TLS tests, which need a server with a real
certificate.

- [ ] **Step 4: Commit**

```bash
git add tests/test_mysql_integration.py tests/test_postgres_integration.py
git commit -m "test: json-anonymisierung gegen echte datenbanken"
```

---

### Task 6: Documentation and version

- [ ] **Step 1:** Both user guides get a paragraph under the anonymization section: what the `json`
  rule kind does, that native JSON columns need no rule, and the three limits (arrays, depth 4,
  non-string values).
- [ ] **Step 2:** `README.md` feature bullet mentions JSON.
- [ ] **Step 3:** The design document's status becomes `implemented in 1.4.0`.
- [ ] **Step 4:** `python scripts/bump_version.py 1.4.0`, suite green.
- [ ] **Step 5:** Commit documentation and version separately.
