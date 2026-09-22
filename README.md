# SQL Transfer

macOS app that copies MySQL and PostgreSQL tables from remote servers into a local database,
through SSH tunnels, with indexes, foreign keys and TLS handled. Built with Python and
[Flet](https://flet.dev); the rows themselves are moved by [apitap](https://github.com/apitap/apitap-lib).

## Download

Signed and notarized builds for Apple Silicon (macOS 12 or newer) are on the
[Releases](https://github.com/axro-gmbh/sqltransfer/releases) page. Unzip, move the app to
Applications, open it.

A user guide (German) is in [docs/benutzerhandbuch.md](docs/benutzerhandbuch.md).

## Features

- SSH and database profiles; passwords and key passphrases live in the macOS Keychain and are
  removed with the profile
- Transfer one table, a selection of tables or a whole database; preview the plan first
- Live progress (table x of y), a cancel button that stops after the current table, and a
  colour-coded, auto-scrolling log
- MySQL and PostgreSQL as source and destination
- MySQL to MySQL keeps the source's secondary indexes (unique, fulltext, prefix, functional) and
  foreign keys, including their `ON DELETE` / `ON UPDATE` rules
- Per-profile encryption: Automatic, Off, Encrypted (certificate not checked), Encrypted and
  verified; a custom CA for PostgreSQL
- SSH host keys are checked against `~/.ssh/known_hosts`; a changed key stops the connection
- The connection test logs in for real and reports whether the session is encrypted
- Run history with the last 20 transfers, reusable with one click
- Automatic updates through [Sparkle](https://sparkle-project.org), signed and served from GitHub
  Releases

## How it works

- **SSH:** the app opens the tunnel itself (OpenSSH, or paramiko for keys with a passphrase) and
  hands apitap a local forwarded address. Host and port in a profile are as seen from the SSH
  server, like in DataGrip. Both backends share `~/.ssh/known_hosts`: the first key seen is
  recorded (like OpenSSH's `accept-new`), any later change is refused and never retried over the
  other backend.
- **MySQL destinations** go through a per-table temp table and the app's own atomic
  `RENAME TABLE` swap. apitap creates tables with columns and primary key only, so the source's
  indexes are read once per run and added to the temp table before the swap; foreign keys follow
  after the whole run (key names are unique per database, and the referenced table may be copied
  later), with `FOREIGN_KEY_CHECKS=0` like a dump restore. Tables that other tables reference are
  not swapped (InnoDB would move those keys onto the outgoing table) but have their rows replaced
  in place.
- **Empty source tables** empty the destination table, or create it when missing. apitap's 0-row
  guard alone would leave old rows in place.
- **Encryption** is one setting applied the same way to apitap (`ssl-mode` / `sslmode` in the
  URL), pymysql and psycopg. Automatic mirrors apitap's own default since 0.55.1: off for
  loopback addresses and SSH tunnels, verified TLS for any other host. A custom CA works for
  PostgreSQL only, because apitap trusts its bundled public roots for MySQL.

## Development

Requirements: macOS on Apple Silicon, Python 3.12 or newer (developed on 3.14) and a Rust
toolchain ([rustup](https://rustup.rs)) to build apitap.

```zsh
git clone https://github.com/axro-gmbh/sqltransfer.git
cd sqltransfer
python3 -m venv .venv314
source .venv314/bin/activate
pip install -r requirements.txt
```

### apitap

apitap is not installed from PyPI, which only has Linux x86_64 wheels. It is built from source,
and upstream v0.56.0 needs one patch to compile on macOS: `crates/apitap-core/src/wire/mywire.rs`
uses the Linux-only `libc::TCP_KEEPIDLE`, which Apple platforms call `TCP_KEEPALIVE`
([patch](patches/apitap-0.56.0-macos-tcp-keepalive.patch)).

```zsh
git clone --branch v0.56.0 --depth 1 https://github.com/apitap/apitap-lib.git .vendor/apitap-lib
git -C .vendor/apitap-lib apply ../../patches/apitap-0.56.0-macos-tcp-keepalive.patch
DEVELOPER_DIR=/Library/Developer/CommandLineTools pip wheel --no-deps -w .vendor .vendor/apitap-lib/py-apitap
pip install .vendor/apitap-0.56.0-*.whl
python -c "import apitap"
```

`DEVELOPER_DIR` builds apitap with the Command Line Tools (macOS 26 SDK) instead of Xcode. Built
with the Xcode 27 toolchain, the module compiles but fails to load (`mis-aligned LINKEDIT string
pool`); the last line catches that. Install the Command Line Tools with `xcode-select --install` if
that directory does not exist. Only this step needs them; `flet build` itself uses Xcode.

The wheel stays in `.vendor`, because `flet build` picks it up from there (see
`[tool.flet.dev_packages]` in `pyproject.toml`).

### Run and test

```zsh
python run.py
python -m pytest -q
```

The window takes 15 to 20 seconds on first start. Integration tests against real MySQL,
PostgreSQL and SSH servers are skipped unless their environment variable is set; each test file
explains how to start a disposable server for it.

### Build and release

Signing, notarization, packaging and releases (`scripts/release.sh`, Sparkle keys and appcast) are
described in [MACOS_BUILD.md](MACOS_BUILD.md).

## Project layout

- `run.py` - app launcher
- `assets/` - app icon (`icon_macos.png` is the full-bleed variant for the macOS bundle)
- `src/sqltransfer_app/app.py` - Flet UI and transfer flow
- `src/sqltransfer_app/ui.py` - presentation helpers
- `src/sqltransfer_app/transfer.py` - apitap orchestration and MySQL/PostgreSQL helpers
- `src/sqltransfer_app/tunnel.py` - SSH tunnels and host key checks
- `src/sqltransfer_app/tls.py` - encryption settings for all three database clients
- `src/sqltransfer_app/scope.py` - turns the scope choice into a transfer mode
- `src/sqltransfer_app/storage.py` - SQLite storage for profiles and run history
- `src/sqltransfer_app/secrets.py` - Keychain access
- `patches/` - local patches to dependencies
- `scripts/` - release script, third-party notices, deployment target fix for the Xcode project
- `docs/` - user guide
- `tests/` - unit tests, plus integration tests that need a disposable server

## License

[Apache License 2.0](LICENSE). Components shipped inside the app bundle are listed with their
licenses in [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt). The AXRO name and logo are not
covered by this license.
