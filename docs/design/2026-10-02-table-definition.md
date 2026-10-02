# Design: restoring the table definition

Status: draft, not implemented. Written 02.10.2026.

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

Read once per run, like the indexes: `information_schema.columns` gives the default,
`extra` and the generation expression per column, `information_schema.check_constraints`
joined with `table_constraints` gives the checks, and `information_schema.tables` gives
the `AUTO_INCREMENT` counter.

Applied to the **temp table, before the swap**, in this order, because the later steps
depend on the earlier ones:

1. the secondary indexes (already today), because `AUTO_INCREMENT` needs a key on its
   column
2. generated columns: `ALTER TABLE … MODIFY COLUMN x <type> GENERATED ALWAYS AS (…)
   STORED|VIRTUAL`, which recomputes every row
3. defaults: `ALTER TABLE … ALTER COLUMN x SET DEFAULT …`
4. `AUTO_INCREMENT`: `MODIFY COLUMN x <type> AUTO_INCREMENT`, then
   `ALTER TABLE … AUTO_INCREMENT = <counter>`
5. checks: `ALTER TABLE … ADD CONSTRAINT <name> CHECK (…)`

A failure in any of these **fails the run**; the temp table is not swapped in, so the
destination keeps its previous content rather than a half-restored definition. That is
the same rule the anonymization follows.

The in-place path (a table other tables reference) needs none of this: the table is not
replaced, only its rows are, so its definition was never lost.

## Limits

- **PostgreSQL generated columns** cannot be added to an existing column; the column
  would have to be dropped and re-added, which moves it to the end of the table. Out of
  scope for now: the app reports them instead, as it does today.
- **PostgreSQL identity columns** are left alone for the same reason.
- Defaults and checks are restored on both dialects.
- A check constraint referring to a column apitap did not carry is reported and skipped
  rather than failing the run.

## Testing

Unit, without a database: building the `ALTER` clauses from `information_schema` rows,
including the ordering, and the decision which properties a table needs at all.

Integration against the disposable servers:

- a table with all four properties, transferred, then the destination's
  `SHOW CREATE TABLE` compared against the source's, normalised for the table name
- a generated column recomputes after the transfer (change the input, read the output)
- the `AUTO_INCREMENT` counter continues where the source left off
- an insert that relies on a default succeeds
- a row violating a check is refused by the copy
- a failing restore leaves the destination table untouched (no swap)
