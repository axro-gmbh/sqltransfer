# Design: restoring the table definition

Status: implemented in 1.5.0. Written 02.10.2026.
Implementation plan: [2026-10-02-table-definition-plan.md](2026-10-02-table-definition-plan.md).

A destination table the app creates or swaps in keeps the data and the primary key,
and the app already restores the secondary indexes and the foreign keys. What it does
not restore is everything that governs **writing**: generated columns, defaults, check
constraints and `AUTO_INCREMENT`. Measured on a table carrying all of them:

| Property | Survives today |
|---|---|
| Column names, types, `NOT NULL`, per-column collation, primary key | yes, apitap carries them |
| Secondary indexes including UNIQUE, foreign keys with their rules | yes, the app restores them |
| Generated columns (`AS (…) STORED`) | **no** |
| `DEFAULT` values | **no** |
| `CHECK` constraints | **no** |
| `AUTO_INCREMENT` and its counter | **no** |

For a development copy that reads, this is invisible. It bites when the application
writes: `order_date` no longer follows `order_date_time`, an insert without a column
hits a missing default, and `auto_increment` is an ordinary number column.

## Goal and non-goals

**Goal.** After a transfer, the destination table carries the four properties above as
the source has them, so writing against the copy behaves like writing against the
original.

**Not a goal:**

- Triggers, views, partitioning, column comments, table comments.
- The exact column order, if apitap ever changes it.
- Making the copy byte-identical to `SHOW CREATE TABLE` of the source. The honest way
  to that would be creating the table from the source DDL, which is impossible while
  apitap drops and recreates its destination on every run (verified in
  `crates/apitap-core/src/sink/mysql.rs`).
- PostgreSQL generated columns, see [Limits](#limits).

## How it works

**The source of truth is `SHOW CREATE TABLE`, not information_schema.** The first attempt
built the clauses from information_schema and failed against a real server: expressions
come back with escaped quotes (`concat(_utf8mb4\\'x\\',\`coll\`)`), function defaults come
back without the brackets MySQL requires (`CURRENT_TIMESTAMP` instead of
`(CURRENT_TIMESTAMP)`), and neither is re-executable. `SHOW CREATE TABLE` is what the
server itself would run, which also brings `NOT NULL`, the column collation and the
`STORED`/`VIRTUAL` keyword along for free.

Read once per run, like the indexes: one cheap information_schema query per table
answers whether it carries anything at all, and only then does it cost a
`SHOW CREATE TABLE`.

**The DDL is read as bytes.** MySQL writes it in the table's own charset, not the
connection's, so a table whose DDL holds bytes that are not UTF-8 made the client's
own decoding fail. Those bytes are decoded here, UTF-8 first and latin-1 after, and
the codec travels with the text because it is what turns a binary default back into
the exact bytes it stood for.

**A binary default becomes a hex literal.** For a `binary`, `varbinary` or `blob`
column MySQL writes the default as a string of raw bytes. Sent back as text those are
more bytes than the column holds and the server refuses the column with error 1067
(seen on `category.cms_page_version_id` of a Shopware database). `DEFAULT 0x…` names
exactly the bytes that were there.

Applied to the **temp table, before the swap**, in this order, because the later steps
depend on the earlier ones:

1. the secondary indexes (already today), because `AUTO_INCREMENT` needs a key on its
   column
2. generated columns: `ALTER TABLE … MODIFY COLUMN x <type> GENERATED ALWAYS AS (…)
   STORED|VIRTUAL`, which recomputes every row
3. defaults: `ALTER TABLE … ALTER COLUMN x SET DEFAULT …`
4. `AUTO_INCREMENT`: `MODIFY COLUMN x <type> AUTO_INCREMENT`, then
   `ALTER TABLE … AUTO_INCREMENT = <counter>`
Check constraints are the exception: they run **after** the swap, with the foreign keys,
because their names belong to the database and the outgoing table still carries them. A
check that cannot be applied is reported and the rest still run.

A `VIRTUAL` generated column cannot be converted from a plain one (error 3106), so it is
dropped and re-added in place.

**A single detail that cannot be restored is named, not fatal.** The clause and the
server's error go into the log, the table's other details are still applied, and the
run finishes. The first version failed the whole run instead, and one column of one
Shopware table (a binary default MySQL would not take back) left the developer with no
copy at all after half an hour of transferring. The data is copied and correct either
way; what a named gap costs is one property of one column, and that is cheaper than no
copy. The same holds one level up: a table whose DDL cannot be read is named and
skipped instead of costing every other table its definition.

What still **fails the run** is the read itself not working at all (no connection, no
rights). Carrying on there would hand back a copy that looks finished and has lost
every generated column, default and check, which is exactly how this went unnoticed
before.

The in-place path (a table other tables reference) keeps its own definition, because the
table is not replaced, only its rows are. It is still compared against the source and
the differences are applied: a destination table created by an earlier version has no
generated columns, and nothing else would ever give them back.

**An aborted run takes its temp tables with it.** Whatever it created and did not swap
in is dropped when it ends, however it ends. Otherwise every failed run leaves another
`apx_…` table behind in the destination.

## Limits

- **MySQL to MySQL only.** The DDL of one dialect is not the DDL of the other, so a
  PostgreSQL source yields no clauses at all rather than clauses the destination cannot
  run, and a PostgreSQL destination is left as it was. Both are stated in the log.
- A check constraint referring to a column apitap did not carry is reported and skipped
  rather than failing the run.
- A DDL that is not valid UTF-8 is read as latin-1 as a whole, so a genuinely UTF-8
  string elsewhere in the same statement can come back mangled. Binary defaults, the
  case this was found on, are exact because they go back as hex.

## Testing

Unit, without a database: building the `ALTER` clauses from DDL, including the ordering,
and the decision which properties a table needs at all. The fixtures are verbatim
`SHOW CREATE TABLE` output, not written by hand: the hand-written ones agreed with the
first version's bug.

Integration against the disposable servers:

- a table with all four properties, transferred, then the destination's
  `SHOW CREATE TABLE` compared against the source's, normalised for the table name
- a generated column recomputes after the transfer (change the input, read the output)
- the `AUTO_INCREMENT` counter continues where the source left off
- an insert that relies on a default succeeds
- a row violating a check is refused by the copy
- a binary default: the clause is applied to a real table, then a row that leaves the
  column out is compared byte for byte against the original default
- one table whose DDL cannot be read, with a second one beside it that keeps its
  definition and a message that names the first

The real app, clicked through end to end against a real server (`tests/test_real_run_integration.py`):
whole-database scope restores the definitions on newly created tables and on the
in-place path, a failing read stops the run, one impossible clause does not, and an
aborted run leaves no temp table behind. Every other test calls the service methods in
the order the app calls them, which proves the pieces work but not that the app still
wires them that way.
