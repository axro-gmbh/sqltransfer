# Build macOS App (`sqltransfer`)

This guide packages the project as a macOS desktop app.

## 1) Prepare environment

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
uv python install 3.14
uv venv --python 3.14 .venv314
source .venv314/bin/activate
```

Install runtime dependencies used by the app:

```zsh
uv pip install flet sshtunnel "paramiko<4" keyring pymysql "psycopg[binary]" pytest
```

## 2) Install `apitap` from source (macOS workaround)

`apitap` wheels may not be available for macOS/arm64 in your environment, so install from source:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
mkdir -p .vendor
git clone --depth 1 https://github.com/apitap/apitap-lib.git .vendor/apitap-lib
source .venv314/bin/activate
uv pip install -e .vendor/apitap-lib/py-apitap
python -u -c "import apitap; print('IMPORT_OK')"
```

## 3) Validate before packaging

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
source .venv314/bin/activate
pytest -q
python -m compileall -q src run.py
```

## 4) Packaging options

## Option A - Flet CLI (recommended if available)

Install CLI tools first:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
source .venv314/bin/activate
uv pip install "flet[all]" flet-cli
flet --help
```

Depending on your installed CLI version, use one of these:

```zsh
# Newer CLI variants
flet build macos run.py

# Older CLI variants
flet pack run.py --name sqltransfer
```

If one command is not recognized, use the other and check `flet --help` output.

## Option B - PyInstaller fallback

Use this if Flet CLI packaging is unavailable in your environment:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
source .venv314/bin/activate
uv pip install pyinstaller
pyinstaller --name sqltransfer --windowed --onedir run.py
```

App output will be under:

- `dist/sqltransfer.app` (or a `dist/sqltransfer/` bundle depending options)

## 5) First run checks

After building:

1. Open the app and test `Test SSH connection` in Step 1.
2. Test `Test DB form` / `Test SSH tunnel target` in DB profile setup.
3. Run a small table transfer first.

## 6) Optional signing (distribution)

For distribution outside your machine, you will usually need code signing and (optionally) notarization:

```zsh
codesign --deep --force --verify --verbose --sign "Developer ID Application: YOUR NAME (TEAMID)" dist/sqltransfer.app
codesign --verify --deep --strict --verbose=2 dist/sqltransfer.app
```

Notarization commands depend on your Apple developer setup and are intentionally omitted here.

## Notes

- Keep using `.venv314` for running, testing, and packaging to avoid dependency drift.
- If app startup fails after packaging, run from Terminal once to capture logs and missing module errors.
- If SSH tunneling errors return, confirm `paramiko` version is `<4` in the active build env.
