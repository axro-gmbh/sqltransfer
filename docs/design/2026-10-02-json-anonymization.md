# Design: anonymizing personal data inside JSON

Status: implemented in 1.4.0. Written 02.10.2026.
Implementation plan: [2026-10-02-json-anonymization-plan.md](2026-10-02-json-anonymization-plan.md).
Builds on [2026-10-01-anonymization.md](2026-10-01-anonymization.md); everything there about rules,
salted hashes, the point in the run and the guard rails still holds.

Personal data does not only sit in its own column. In Shopware it also sits inside JSON:
`customer.custom_fields`, `order_customer.custom_fields`, address payloads. Replacing the whole
column there is wrong: it would destroy the structure the application reads. This describes how the
values **inside** the JSON are replaced instead, while the structure stays as it is.

## Goal and non-goals

**Goal.** A key inside a JSON value that matches an anonymization rule gets the same treatment its
column would get: a fake value derived from the original, deterministic, salted. Every other key, the
structure, and the rows that do not carry the key stay untouched.

**Not a goal:**

- Arrays of objects (`{"positions": [{"email": "…"}]}`). `JSON_REPLACE` takes no wildcard path, so
  those are **reported, not rewritten** (see [Limits](#limits)).
- Free text inside JSON: an address in the value of `"comment"` is as unreachable as in a text column.
- Values that are not strings. A `"phone": 491234567` stays a number; replacing it with a string
  would change the type the application reads back.

## Which columns are looked into

Two ways in, because the two cases are genuinely different:

1. **Native JSON columns** (`json` in MySQL, `json`/`jsonb` in PostgreSQL) are looked into
   automatically. The type says unambiguously what the content is.
2. **Text columns holding JSON** need a rule with the new kind **`json`**. Shopware stores its JSON
   in `longtext` with a `json_valid` check, and the type alone cannot tell such a column from prose,
   so this stays a deliberate choice: a rule like `custom_fields` or `*_fields` with kind `json`.

A rule with kind `json` on a column that holds no valid JSON is reported as WARN and left alone;
the run continues.

## Which keys are replaced

The **existing rules**, matched against the key name instead of the column name. `*mail*` therefore
covers a column `email` and a JSON key `email` alike, and a rule the user adds works in both places
without being written twice. The kind of the matching rule decides the replacement, exactly as for a
column.

## Finding the keys

Keys are discovered from the data, one query per level, over **all** rows rather than a sample:

```sql
-- MySQL, level 1
SELECT DISTINCT jt.k
  FROM kunde,
       JSON_TABLE(JSON_KEYS(kunde.custom_fields), '$[*]' COLUMNS (k VARCHAR(190) PATH '$')) jt;
-- level 2, for every level-1 key whose value is an object
SELECT DISTINCT jt.k
  FROM kunde,
       JSON_TABLE(JSON_KEYS(kunde.custom_fields, '$.adresse'), '$[*]' COLUMNS (k VARCHAR(190) PATH '$')) jt;
```

PostgreSQL does the same with `jsonb_object_keys`. Descent stops at **depth 4**; anything deeper is
reported as WARN rather than silently skipped. Only keys whose value is an object are descended into,
and only keys whose value is a string are rewritten.

## The statement

One `UPDATE` per table as before, carrying the column assignments for plain columns and nested
`JSON_REPLACE` calls for the JSON ones. `JSON_REPLACE` touches only paths that exist, so a row
without the key is left alone, and `NULL` stays `NULL`:

```sql
UPDATE `kunde`
   SET `custom_fields` = JSON_REPLACE(
         JSON_REPLACE(`custom_fields`, '$.email',
           CONCAT('user-', SUBSTRING(SHA2(CONCAT(%s, JSON_UNQUOTE(JSON_EXTRACT(`custom_fields`, '$.email'))), 256), 1, 10), '@example.invalid')),
         '$.adresse.ort',
           ELT(1 + MOD(CONV(SUBSTRING(SHA2(CONCAT(%s, JSON_UNQUOTE(JSON_EXTRACT(`custom_fields`, '$.adresse.ort'))), 256), 1, 4), 16, 10), 6), 'Musterstadt', …))
 WHERE `custom_fields` IS NOT NULL;
```

PostgreSQL uses `jsonb_set` guarded by the key's existence. A text column keeps its type; MySQL
returns the JSON serialised in its own normal form, so formatting (spaces after colons, key order)
can change. The content does not, and `json_valid` still holds.

## Reporting

The log says per table and column which paths were rewritten, the same way it already names columns:

```
anonymized kunde: email, custom_fields$.email, custom_fields$.adresse.ort (3 values)
kunde.custom_fields: positions[*].email is inside an array and was not rewritten  [WARN]
kunde.daten: not valid JSON, left alone  [WARN]
```

`Preview plan` lists the same paths before anything is copied, read from the source.

## Limits

- **Arrays of objects are not rewritten.** Detected during discovery, reported as WARN with the path.
- **Depth 4.** Deeper nesting is reported, not followed.
- **Non-string values** under a matching key are left as they are, and reported once per path.
- **Cost.** Discovery is one query per JSON column per level, run once per table in scope, plus the
  `UPDATE` itself. On a wide `custom_fields` column of a large table this is the expensive part of
  the run, and the log reports the duration.
- **Uniqueness** carries over from the column case: only `email` and `text` keep it.

## Testing

Unit, without a database: path building per dialect and kind, the `NULL` and missing-key branches,
the decision table for which keys are descended into, rewritten, or reported.

Integration against the disposable MySQL and PostgreSQL:

- a value inside JSON is replaced, its siblings and the structure survive
- a row without the key, a `NULL` column and an empty object stay untouched
- nested keys two and three levels down are replaced
- an array of objects is reported and left alone
- a non-string value under a matching key is left alone
- the same original value in a column and inside JSON yields the same fake value
- a text column that is not valid JSON is reported, and the run continues
