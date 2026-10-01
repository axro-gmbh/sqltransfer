# Design: anonymizing personal data on the way in

Status: draft, not implemented. Written 01.10.2026.

Copying production-like data pulls real names, e-mail addresses and phone numbers onto a developer
machine. This describes how SQL Transfer replaces those values with fake ones that keep the data
usable, and what the approach does **not** protect against.

## Goal and non-goals

**Goal.** After a transfer, columns that hold personal data contain invented values, while the data
stays usable: joins still match, `UNIQUE` columns stay unique, row counts and types are unchanged.

**Not a goal, and deliberately so:**

- Keeping real data off the machine entirely. The rows travel from the source into the destination
  through apitap, and the app never sees them in flight. Anonymizing happens **after** they have
  landed, so real values exist on disk for seconds. Only a view or an export that is already
  anonymized on the source side would avoid that, which is a different project.
- Reversibility. There is no mapping table from fake back to real.
- Anything but text columns. A phone number stored as `BIGINT` is reported, not rewritten (see
  [Guard rails](#guard-rails)).
- Per-table exceptions. Rules match column names across all tables; see
  [Open questions](#open-questions).

## How a value is replaced

Every replacement derives from the original value through a hash, so the **same input always yields
the same output**: a person's address in `kunde` and in `bestellung` becomes the same fake address,
joins over those columns keep working, `UNIQUE` columns stay unique, and a second run produces the
same database.

The hash is salted with a random value created once per installation and kept in the macOS Keychain
(`anonymization:salt`). Without a salt, anyone holding a list of real addresses could hash them and
check which ones occur in the data, which would defeat the whole exercise. A consequence worth
knowing: two machines produce different fake values for the same input.

`NULL` stays `NULL` and an empty string stays empty, so "has no e-mail" keeps meaning that.

### Expressions

MySQL, for a column `email`:

```sql
CASE
  WHEN `email` IS NULL THEN NULL
  WHEN `email` = ''    THEN ''
  ELSE CONCAT('user-', SUBSTRING(SHA2(CONCAT(:salt, `email`), 256), 1, 10), '@example.invalid')
END
```

PostgreSQL, same column:

```sql
CASE
  WHEN email IS NULL THEN NULL
  WHEN email = ''    THEN ''
  ELSE 'user-' || substr(encode(sha256(convert_to(:salt || email, 'UTF8')), 'hex'), 1, 10)
       || '@example.invalid'
END
```

`SHA2` is built into MySQL, `sha256()` into PostgreSQL 11 and newer; neither needs an extension.
`example.invalid` is reserved by RFC 2606, so a fake address can never reach a real mailbox.

The other kinds follow the same shape, picking from a fixed list of invented values by hash
(`ELT(…)` in MySQL, an array index in PostgreSQL) or formatting digits from the hash:

| Kind | Result, example |
|---|---|
| `email` | `user-7f3a9c21d4@example.invalid` |
| `firstname`, `lastname`, `fullname` | `Alina`, `Brandt`, `Alina Brandt` |
| `phone` | `+49 30 4718326` |
| `street` | `Lindenweg 42` |
| `city` | `Musterstadt` |
| `postcode` | `48291` |
| `text` | `text-7f3a9c21` (generic fallback) |
| `empty` | `''` |

## Which columns are hit

A rule is a **pattern on the column name** plus a kind. Matching ignores case and uses shell-style
wildcards, so `*mail*` covers `email`, `Email`, `kunde_email` and `billing_mail`.

Rules live in `profiles.db`:

```sql
CREATE TABLE anonymization_rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT    NOT NULL UNIQUE,
    kind    TEXT    NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
```

The first enabled rule that matches wins, in the order the list shows them (the order they were added). On first start the table is seeded with
German and English defaults (`*mail*`, `vorname`, `first_name`, `nachname`, `last_name`, `*telefon*`,
`phone`, `mobil*`, `strasse`, `street`, `adresse`, `address*`, `plz`, `zip*`, `ort`, `city`). They can
be edited, disabled, deleted and extended in the UI, in the same dialog style as the profiles.

Only text columns are rewritten (`char`, `varchar`, `text` and their variants). The column list comes
from `information_schema.columns` of the destination, so a column that the source has but the
destination does not is simply absent.

## Where it runs

**MySQL destinations:** right after the rows have landed in the per-table temp table, and **before**
the indexes are built and before the swap. The finished table therefore never holds real data, and a
`UNIQUE` index is built over the fake values, where it belongs.

**PostgreSQL destinations:** directly after the transfer, as an `UPDATE` on the destination table;
there is no temp table in that path.

One `UPDATE` per table, carrying every matched column at once:

```sql
UPDATE `shop`.`kunde__sqlt_tmp`
   SET `email` = <expression>, `vorname` = <expression>, `telefon` = <expression>;
```

**If that statement fails, the run fails.** For MySQL the temp table is *not* swapped, so the real
data never reaches the destination table; the temp table is left in place for inspection. For
PostgreSQL the table at that point still holds real data, which the log says in plain words, with
the advice to drop it or repeat the run.

## Guard rails

A forgotten column is the dangerous case: the transfer looks fine and real phone numbers sit in the
destination. Three things work against that.

1. **The preview names the columns.** `Preview plan` reads the columns of the tables in scope from
   the source and lists what would be replaced, before a single row moves. It asks the source
   because the destination table may not exist yet; the run itself asks the destination, which is
   what actually gets rewritten.
2. **The log names what happened**, per table: `anonymized kunde: email, vorname, telefon (3 columns)`.
3. **Suspicious columns without a rule are reported as WARN.** A built-in list of fragments (mail,
   name, phone, tel, strasse, street, address, adresse, plz, zip, city, ort, iban, geburt, birth)
   flags any column that looks personal but was not rewritten, including the reason: no rule, rule
   disabled, or not a text column.

## Using it

The **Transfer** section gets a switch, "Anonymize personal data". It is **on** whenever enabled
rules exist, because forgetting to anonymize is the expensive mistake and anonymizing by accident is
not. The switch applies to the run, not to a profile.

The **Profiles** section gets a third tile, "Anonymization rules", with the same search field,
"New rule" button and edit dialog as the other lists. A rule row shows pattern, kind and whether it
is enabled.

## Testing

Unit tests, no database needed:

- pattern matching: case, wildcards, order, disabled rules
- the generated SQL per dialect and kind, including the `NULL` and empty-string branches
- the suspicion check: which columns are reported and why

Integration tests against the disposable MySQL and PostgreSQL containers the repo already uses:

- values really changed, and no column outside the rules touched
- the same input in two tables yields the same output
- `NULL` and `''` survive
- a `UNIQUE` column stays unique after the rewrite
- a failing `UPDATE` leaves the MySQL destination table untouched (no swap)

## Risks and limits

- **Cost on large tables.** One `UPDATE` across every row, per table. It runs before the indexes
  exist in the MySQL path, which is the cheap moment, but it is still a full pass. The log reports
  the duration.
- **Real data exists briefly**, see the non-goals. For MySQL it is confined to the temp table.
- **A hash is not forgetting.** Values are pseudonymous: whoever knows a real value, has the salt
  from the Keychain and can run a query could confirm a guess. The salt makes that impossible from
  the outside, not from this machine.
- **The rules only know column names.** A free-text `kommentar` column containing an address stays
  as it is. That is not solvable by name matching, and the WARN list will not catch it either.

## Open questions

- Per-table exceptions ("this column despite the rule, that one additionally"). Left out for now,
  the rule list is global. Worth adding only when a concrete table makes it necessary.
- Whether the switch should also be available per destination profile, so a given database is always
  anonymized. The current answer: no, one switch per run is easier to understand.
