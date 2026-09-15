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

## 4) App icon

`flet build` reads icons from `assets/`, not from the project root:

- `assets/icon.png` - default for every platform
- `assets/icon_macos.png` - overrides it for the macOS bundle

**The macOS source must be full-bleed.** `flutter_launcher_icons` draws the image onto a white
rounded background inside the standard content box. A source that already carries its own rounded
shape plus a transparent margin ends up framed twice: a white ring with the artwork shrunk to about
60% of the tile. `assets/icon_macos.png` is therefore a flattened, edge-to-edge 1024x1024 version of
`assets/icon.png` (transparent margin cropped on pixels with alpha > 200, corners filled with the
tile colour).

Verifying the result without installing anything:

```zsh
sips -s format png build/macos/sqltransfer.app/Contents/Resources/AppIcon.icns --out /tmp/icon.png
open /tmp/icon.png
```

## 5) Packaging

Prerequisites, all of them hit during the first build:

- **Full Xcode**, and the developer directory must point at it, not at the Command Line Tools:
  ```zsh
  sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
  sudo xcodebuild -runFirstLaunch
  xcode-select -p   # must print /Applications/Xcode.app/Contents/Developer
  ```
- **CocoaPods**: `brew install cocoapods`
- **Flutter** is downloaded automatically on first build (about 3.8 GB into `~/flutter`)
- **flet-cli must match flet**: `uv pip install "flet-cli==1.0.0"`

The build itself:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
.venv314/bin/flet build macos --arch arm64 --module-name run \
  --exclude .venv .venv314 .vendor build tests .git .pytest_cache .idea patches --yes
```

- `--module-name run` because the entry point is `run.py`, not `main.py`
- `--exclude` matters: without it the build copies `.vendor` (about 1 GB) into the bundle
- app metadata (product name, bundle id, org) comes from `[tool.flet]` in `pyproject.toml`
- `apitap` comes from the locally built wheel via `[tool.flet.dev_packages]`, since PyPI has no
  macOS wheel

Result: `build/macos/sqltransfer.app`, about 187 MB. The Flutter shell is universal, but every
Python extension inside (apitap, psycopg, cffi) is arm64, so the bundle is arm64 only.

## Signing and distribution

Out of the box the bundle is **ad-hoc signed** (`Signature=adhoc`, `TeamIdentifier=not set`). That
runs on the machine that built it. On any other Mac, Gatekeeper blocks it as soon as the download
carries the quarantine flag, and the recipient has to allow it by hand in System Settings.

For real distribution, Flet has the flags built in:

```zsh
.venv314/bin/flet build macos --arch arm64 --module-name run \
  --macos-distribution developer-id \
  --macos-signing-identity "Developer ID Application: <name> (<TEAMID>)" \
  --macos-notary-profile <notarytool-keychain-profile>
```

This needs an Apple Developer Program membership. As of the last check this machine had no signing
identity at all (`security find-identity -v -p codesigning` returned none).

Workaround for handing the app to a colleague without a certificate: use a transport that does not
set quarantine (internal share, `scp`, USB), or have them run once:

```zsh
xattr -dr com.apple.quarantine /Applications/sqltransfer.app
```

## 6) First run checks

After building:

1. Open the app and test `Test SSH connection` in Step 1.
2. Test `Test DB form` / `Test SSH tunnel target` in DB profile setup.
3. Run a small table transfer first.

## Notes

- Keep using `.venv314` for running, testing, and packaging to avoid dependency drift.
- If app startup fails after packaging, run from Terminal once to capture logs and missing module errors.
- If SSH tunneling errors return, confirm `paramiko` version is `<4` in the active build env.
