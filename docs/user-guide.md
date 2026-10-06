# SQL Transfer: User Guide

*Auch auf Deutsch: [benutzerhandbuch.md](benutzerhandbuch.md)*

SQL Transfer copies tables from a remote database into a local one, through an SSH tunnel when needed. It is built for the everyday case "I need production-like data on my machine", without dumps and without files in between.

## Requirements

| | |
|---|---|
| Operating system | macOS 12 or newer |
| Processor | Apple Silicon (M1 and newer). The app does not run on Intel Macs. |
| Access | An SSH key for the jump host, credentials for the source and destination databases |
| Databases | MySQL and PostgreSQL, each as source and as destination |

## Installation

1. Unpack the ZIP file (a double click is enough).
2. Drag `sqltransfer.app` into `/Applications`.
3. Double click it.

The app is signed and notarized by Apple, so **no** security warning appears and there is no need to right click and choose "Open". If a warning does show up, something is wrong with the downloaded file: get it again rather than clicking the warning away.

The first start takes about 15 to 20 seconds, after that it is quick.

### Updates

The app checks for a new version every four hours. If there is one, a window offers "Install Update", "Remind Me Later" and "Skip This Version". After "Install Update" the app downloads the new version, restarts and is up to date. Tick "Automatically download and install updates in the future" and later updates arrive without asking.

You can look right away at any time: the **SQL Transfer** menu has **Check for Updates…** directly below "About SQL Transfer". If there is nothing new, the app says so too.

Every update is signed: the app only installs versions that provably come from us. Without an internet connection nothing happens at all, and the check is repeated later.

## One-time setup

Everything below lives in the **Profiles** section. You need it when setting things up or changing them; day to day it stays collapsed.

### Creating an SSH profile

Needed when the database cannot be reached directly from your machine, which is the normal case here.

1. Open **Profiles**, then **SSH profiles**.
2. Press **New SSH profile**. A window with empty fields opens.
3. Fill in: profile name, host, port (22 by default), username, path to the private key.
4. Only enter a passphrase if your key has one. When you edit a profile the field stays empty on purpose: the secret lives in the Keychain and is never displayed. Leaving it empty means "keep the stored one".
5. Press **Test SSH connection**. Only continue once it reports success.
6. **Save**.

### Creating database profiles

You need at least two: a source and a destination. **Every profile can be both**, there is no role to pick when creating one.

1. Open **Profiles**, then **Database profiles**.
2. Press **New database profile**.
3. Give it a name and choose **Database**: MySQL or PostgreSQL.
4. Enter host, port, database name, username and password.
5. If a tunnel is needed: tick **Use SSH tunnel** and pick the SSH profile.
6. Set **Encryption**, see below. For almost every profile **Automatic** is right.
7. **Test connection** really logs in and tells you whether the session is encrypted. **Test through tunnel** additionally checks the path across the jump host. When the password field is empty, the test uses the stored password from the Keychain.
8. **Save**.

### Finding, changing and deleting profiles

Each list has a search field. It filters by name, host, database and user, and several words narrow it further ("prod shop" only shows what contains both). From about five profiles on, the list scrolls instead of stretching the page.

Each row carries markers: **local** in green for databases on your machine, **remote** in red for everything else, plus **SSH** and the encryption setting when it differs from **Automatic**. The pencil opens the profile for editing, the bin deletes it after a confirmation.

Creating and editing are deliberately separate: the window header reads either "New database profile" or "Edit database profile '<name>'". A new profile with a name that is already taken is refused instead of silently overwriting the existing one. Renaming while editing works at any time, and the password in the Keychain is kept.

> **Important when tunneling:** host and port are the addresses under which the database is reachable **from the SSH server**, not from your machine. This is the same logic as in DataGrip. Often that is `127.0.0.1` or an internal hostname that would not resolve from outside at all.

### Encrypting the database connection

| Setting | Meaning |
|---|---|
| **Automatic** | Off for `localhost` and for connections through an SSH tunnel (which already encrypts), encrypted and verified for every other host |
| **Off** | Never encrypted, only for databases on this machine or in a trusted network |
| **Encrypted, certificate not checked** | Encrypted, but without checking who is on the other end. Protects against eavesdropping, not against a forged server. For servers with a self-signed certificate, which is the normal case with MySQL |
| **Encrypted and verified** | Encrypted, with the certificate and the hostname checked |

A private certificate authority (field **CA certificate**) works for PostgreSQL only. With MySQL the transfer trusts publicly recognised authorities only, so pick **Encrypted, certificate not checked** for internal servers there.

Passwords and passphrases go into the **macOS Keychain**, not into a file of the app. Deleting a profile deletes its Keychain entry as well, which is why the app asks first.

## Running a transfer

### 1. Pick source and destination

Fill the two dropdowns at the top of the **Transfer** section. The source is on the left, the destination on the right. Both offer every profile, and the app refuses the same profile on both ends.

The entries are grouped: first **On this machine** with a laptop icon, below it **Elsewhere** with a cloud icon. The headings cannot be selected. You can type into both fields and the list filters along, which beats scrolling once you have many profiles. If you type something that matches no profile and select nothing, the previous selection stays active.

**Destinations outside your machine are marked in red.** As soon as you pick a destination that is not directly on `127.0.0.1` or `localhost`, a red note appears below the dropdown naming host and database. A destination behind an SSH tunnel always counts as elsewhere, even when it says `127.0.0.1`: that address is then the near end of a tunnel to another machine.

For such destinations a question appears when you start, naming host, database and scope. Only the red **Overwrite on <host>** button starts the transfer; **Cancel** aborts without writing anything. For destinations on your own machine the app does not ask.

**Test source** and **Test destination** check both connections before you start. That costs a few seconds and saves aborted transfers.

### 2. Decide what is transferred

The question "what should be copied" has exactly three answers:

| Choice | Meaning |
|---|---|
| **One table** | A single table. Type the name or pick it from the list. |
| **Selected tables** | Several tables, ticked off a list. |
| **Whole database** | The whole schema or database. |

For the first two, press **Load tables** first so the app reads the table list from the source. The field under **One table** is searchable: just type and the list filters along. Under **Selected tables**, **Select all** and **Clear** help, and the line above always says how many of how many are ticked.

**Advanced** holds two settings that are rarely needed:

- **Parallel pipes**: how many transfers run at the same time. When the connection goes through an SSH tunnel the app sets this to 1 automatically, because a tunnel otherwise hits its channel limit. Raise it only if you know why.
- **Source schema hint**: the schema of the source, usually `public` with PostgreSQL. It is used when loading the table list and for "Whole database".

### 3. Look at the preview

**After the import: your admin account.** Copying the `user` table brings the source's accounts into the destination and your local one is gone. Create it again, or you cannot get into the administration:

```
bin/console user:create --admin your-name
```

**Preview plan** transfers nothing yet. It shows in the log what would happen: source, destination, scope, parallelism, and whether any special handling applies. For larger transfers it is always worth a look.

### 4. Start and follow along

**Run transfer** starts it. While it runs:

- The progress bar shows which table of how many the app is on.
- The log fills up continuously and scrolls along.
- Every other button is locked so nothing interferes.
- **Cancel** aborts. The abort takes effect after the **table currently being copied**, not in the middle of it. With one very large table that can take a while.

At the end the status line says how many rows were transferred in what time, and the run appears in the history on the right.

## Anonymizing personal data

The **Transfer** section has a switch, **Anonymize personal data**. It is on as long as enabled rules exist. During a transfer the app then replaces the real values in columns holding personal data with invented ones.

**Rules on column names decide what is replaced.** They live under **Profiles → Anonymization rules**, seeded with German and English defaults (e-mail, first and last name, phone, street, city, postcode). A pattern like `*mail*` covers `email`, `Email` and `kunde_email`. **New rule** adds one, the pencil edits, the bin deletes, and a rule can be disabled without losing it.

**The same value always yields the same replacement.** An address appearing in two tables is replaced identically in both, so joins keep working. Uniqueness, however, only holds for the kinds whose replacement carries the hash: **e-mail** and **generic text**. Names, cities and streets come from a list and repeat, so on a `UNIQUE` column pick e-mail or generic text. A random value in the Keychain, which never leaves your machine, makes those replacements unguessable. `NULL` stays `NULL`, empty stays empty.

**Values that have to keep their shape: the "Keep values matching" field.** Some applications read meaning from the shape of a value. At Axro, the AxroCustomer plugin recognises a debtor by its e-mail having the form `160547@axro.de`, the customer number at the company domain. Replace that address and the administration loses the contacts tab, the debtor header and "login as customer", with no error anywhere. So put a regular expression over the **value** into the rule, here `^[0-9]+@axro\.`: whatever matches is left as it is, everything else is replaced. An employee address like `first.last@axro.de` does not match and is still anonymized, because there is a person in it.

**JSON is rewritten too.** Columns of type `json` are searched automatically. When the JSON sits in a text column (the normal case in Shopware, `custom_fields` for example), add a rule with the kind **Look inside the JSON**. Your other rules then apply to the **key names**: `*mail*` covers the key `email` just as it covers a column `email`. The structure stays, only values change, and keys without a rule are left alone.

Three limits: values inside **arrays** (`{"positions":[{"email":...}]}`) are reported but not replaced. Nesting is followed to the **fourth level**. And values that are **not strings** (a phone number stored as a number) stay as they are, so the application does not read back a different type. All three appear as WARN in the log.

**See it before it happens:** **Preview plan** lists the affected columns before a single row is copied. After the run the log says what was replaced. Columns that look personal but have no rule appear as WARN, with the reason.

**Limits worth knowing:**

- Text columns only. A `telefon` column of type `BIGINT` is reported, not changed.
- When a column is too short for the replacement, the value is cut rather than aborting the transfer.
- Free text stays as it is. An address inside a `kommentar` column is not something name matching can find.
- Real data exists briefly on your disk: with MySQL only in the temp table (the finished table never sees it), with PostgreSQL in the destination table itself until the step has run.
- If the replacement fails, the run stops. With MySQL nothing is swapped, so the destination table keeps its previous content.

## Reading the log

Every line carries a time and a level, told apart by colour:

| Level | Meaning |
|---|---|
| **INFO** | normal progress |
| **WARN** | special handling kicked in, the run continues |
| **ERROR** | the run failed |

**Copy** puts the whole log on the clipboard, handy when asking a colleague. **Clear** empties the display; the transfer itself is not affected.

## History

On the right are the last 20 runs with status, source, destination, scope, row count, duration and local time. The arrow icon on an entry loads its scope back into the form, so you can repeat the same run without clicking everything together again.

## Worth knowing

**The destination table is replaced, not added to.** A transfer overwrites the table at the destination. It does not append rows and does not merge anything. So the destination is your local development database, never something whose content anyone depends on.

**The table definition is restored.** When the app creates a table or swaps one in, apitap brings columns, types and the primary key only. The app then puts back what governs writing: **generated columns** with their expression, **defaults**, **CHECK constraints** and **AUTO_INCREMENT** with its counter, on top of the indexes and foreign keys it already restored. Writing against the copy therefore behaves like writing against the original: `order_date` follows along again, an insert without the column takes the default, and a violated constraint is refused.

If one of those steps fails, the run stops and nothing is swapped. The old table is better than a half-restored one.

The source of truth is the source's own `SHOW CREATE TABLE`, the definition as the server states it. Check constraints are applied after the swap, because in MySQL their names belong to the database and the outgoing table holds them until then. One that cannot be applied is reported and does not cost the table. That holds for every detail: whatever the server will not take back (a binary default, for instance, which it writes differently from how it accepts it) goes into the log with its clause and the reason, and the table's other details are still applied.

Not carried over: triggers, views, partitioning and column comments. **All of this is MySQL to MySQL only.** With a PostgreSQL source or destination the previous behaviour stands: the copy loses these properties and the log says so.

**Special handling with MySQL as destination.** The app copies every table into a temporary table first and swaps it in one step at the end, so nobody sees a half filled table. When other tables reference the destination table by a foreign key, the app does not swap it but replaces the rows in place instead, because a swap would break those references. That shows up as WARN in the log. It is not an error, it says a detour was taken.

**Indexes are copied from the source.** For MySQL to MySQL the app creates the same indexes at the destination as in the source, including UNIQUE, FULLTEXT and prefix indexes. This happens before the swap, so the table is fully indexed from the first moment. On large tables it costs noticeable time, and the log then says "Building … index(es)". When the source is PostgreSQL, the destination table only gets its primary key.

**Foreign keys are copied too**, including rules such as `ON DELETE CASCADE`. They are created at the end of the run, once every table is there, shown as "Restoring foreign keys" in the log. The existing rows are not checked while doing so, exactly like a database import: tables from a live source are copied minutes apart and therefore do not always match row for row. Foreign keys pointing into another schema are not copied, and the log names them as WARN.

**Empty source tables** lead to an empty destination table: if it is missing it gets created, and if it still holds old rows those are removed. Both appear as WARN in the log. If the app creates an empty table whose foreign key targets are still missing, it sets those foreign keys in a second pass at the end.

**Cancelling with destinations other than MySQL.** There the transfer runs as one single operation. **Cancel** is noted and confirmed in the log, but only takes effect once the running operation ends by itself.

## Troubleshooting

| Message or symptom | What to do |
|---|---|
| **Missing: …** under a field | A required field is empty. The field is marked in red. |
| The SSH test fails | Check the key path, check the passphrase, check that the jump host is reachable. |
| The database test fails although SSH works | Host and port have to be entered as seen from the SSH server, not from your machine. |
| `certificate verify failed` or `UnknownIssuer` | The server has a self-signed or internal certificate. With PostgreSQL enter the CA file, with MySQL choose **Encrypted, certificate not checked**. |
| `SSH host key … does not match` | The jump host presents a different key than on first contact. Do not click it away, clarify with whoever runs it first. The command to remove the old entry is in the message. |
| The transfer aborts with a channel error | Set **Parallel pipes** to 1. |
| The table list stays empty | Wrong schema under **Source schema hint**, usually `public` with PostgreSQL. |
| The app seems unresponsive | During a run the buttons are locked on purpose. The progress bar and the log show that it is still going. |

For anything not listed here: copy the log with **Copy** and send it along. The actual cause is almost always in there.

## Where your data lives

| What | Where |
|---|---|
| Profiles and history | `~/Library/Application Support/sqltransfer/profiles.db` |
| Passwords and passphrases | macOS Keychain |

The app sends nothing anywhere. It talks only to the databases and the SSH server you entered yourself.
