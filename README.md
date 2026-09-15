# sqltransfer

Minimal macOS desktop app (Python + Flet) for moving data from remote databases to local databases using `apitap`, with optional SSH key tunneling.

## MVP Features

- Save SSH profiles (key path + optional passphrase)
- Load/edit/delete SSH profiles
- Test SSH connection directly from the SSH profile form
- Save database profiles for:
  - source/remote (`mysql` or `postgres`)
  - destination/local (`mysql` or `postgres`)
- Load/edit/delete DB profiles
- Test DB connectivity directly from the DB profile form (before saving)
- Test SSH tunnel target explicitly from DB form (DataGrip-like SSH flow)
- Test source and destination connectivity before transfer
- Preview transfer plan (dry-run validation for scope and profile routing)
- One scope question with three answers: single table, selected tables, whole database
- Browse source tables, filter them in the table picker, multi-select via checkboxes, select all or clear in one click
- Live progress while a transfer runs (table x of y) and a cancel button that stops after the current table
- Level-coloured, auto-scrolling transfer log (INFO/WARN/ERROR)
- Deleting a profile asks for confirmation before the keychain entry goes away
- Run transfers via `apitap.transfer()` in modes:
  - `table`
  - `tables` (comma-separated)
  - `schema`
- Optional parallel override in UI; defaults to `1` when SSH tunneling is used to avoid channel-limit failures
- Automatic MySQL staging-name pre-check blocks transfers that would exceed MySQL 64-char table-name limits (`__apitap_staging` suffix)
- For long MySQL table names, transfer now auto-falls back to short temporary `dest_table` names and atomic `RENAME TABLE` swap to keep final table names unchanged
- If empty MySQL fallback tables require FK references not yet present, app auto-creates table with deferred FKs and applies those constraints in a second pass
- Keep recent run history in local SQLite, summarised per run instead of one note per table
- Store DB passwords and SSH passphrases in macOS Keychain via `keyring`
- Remove related Keychain secrets when deleting profiles

## Notes

- SSH tunneling is handled by the app (`sshtunnel`), then `apitap` receives local forwarded DSNs.
- DataGrip-style semantics: DB host/port must be reachable from the SSH server (not necessarily from your Mac directly).
- SSH tunneling currently requires `paramiko<4` due `sshtunnel` compatibility (`DSSKey` removal in newer Paramiko).
- Connection test validates reachability at the TCP level (direct host:port or SSH-forwarded local port).
- Source table browser uses direct metadata queries (`pymysql` for MySQL, `psycopg` for Postgres).
- The UI targets Flet 0.86 (`page.show_dialog`, `page.window.*`); older `page.snack_bar`/`page.window_width` calls are silently ignored by that version.

## Quick Start

The working environment is `.venv314`:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
.venv314/bin/python run.py
```

The window takes about 15 to 20 seconds on first start.

Rebuilding the environment from scratch:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
python3 -m venv .venv314
source .venv314/bin/activate
pip install -r requirements.txt
python run.py
```

`flet` is pinned to `>=0.86.2,<0.90`. The UI calls `page.show_dialog`, `page.window.*` and
`ft.Clipboard()`; on 0.2x those calls are silently ignored (no error, no snack bar, no window
size), and 0.90 drops APIs this version still relies on.

## Run Tests

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
.venv314/bin/python -m pytest -q
```

## Package as macOS app

If your environment has Flet CLI commands available, you can package the app:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
source .venv314/bin/activate
flet pack run.py --name sqltransfer
```

If `flet pack` is unavailable in your installed version, check:

```zsh
flet --help
```

and use the macOS build command shown there.

## Project Layout

- `run.py` - app launcher
- `src/sqltransfer_app/app.py` - Flet UI wiring and transfer flow
- `src/sqltransfer_app/ui.py` - presentation helpers (cards, log panel, formatting)
- `src/sqltransfer_app/scope.py` - turns the scope choice into a transfer mode/value
- `src/sqltransfer_app/storage.py` - SQLite profile/run storage
- `src/sqltransfer_app/secrets.py` - Keychain access
- `src/sqltransfer_app/tunnel.py` - SSH tunnel manager
- `src/sqltransfer_app/transfer.py` - `apitap` transfer orchestration
- `tests/` - persistence, scope and formatting tests














