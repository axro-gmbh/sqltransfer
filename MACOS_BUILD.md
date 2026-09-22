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

This works and has been done once end to end. Prerequisites: an Apple Developer Program
membership, a **Developer ID Application** certificate in the keychain, and a stored notarytool
profile.

The notary profile is created once, in a normal terminal (the app-specific password comes from
appleid.apple.com, it is not the Apple ID password):

```zsh
xcrun notarytool store-credentials "axro-notary" \
  --apple-id <apple-id> --team-id <TEAMID> --password <app-specific-password>
```

Then build:

```zsh
cd /Volumes/T7/Projects/playground/sqltransfer
.venv314/bin/flet build macos --arch arm64 --module-name run \
  --exclude .venv .venv314 .vendor build tests .git .pytest_cache .idea patches --yes \
  --macos-distribution developer-id \
  --macos-signing-identity "Developer ID Application: <name> (<TEAMID>)" \
  --macos-notary-profile axro-notary
```

`developer-id` is the path for distribution **outside** the App Store. The upload to Apple is an
automated malware scan, nothing is published or reviewed. Apple returns a ticket, which Flet
staples to the bundle. Flet signs every nested binary (86 of them in this app, including the
Python extensions) and refuses to start without notary credentials.

**Deployment target (this bites every time Xcode is updated).** The distribution build goes
through `xcodebuild archive`, which is stricter than a plain build: Xcode 26 only accepts
deployment targets from 12.0 upwards, while the Flutter template still writes 11.0 and the pods
10.15. The unsigned build passes, the signed one fails. Fix in the generated project under
`build/flutter/macos`:

- `Podfile`: `platform :osx, '12.0'`, and inside the **existing** `post_install` hook (CocoaPods
  allows only one) add:
  ```ruby
  target.build_configurations.each do |config|
    config.build_settings['MACOSX_DEPLOYMENT_TARGET'] = '12.0'
  end
  ```
- `Runner.xcodeproj/project.pbxproj`: replace `MACOSX_DEPLOYMENT_TARGET = 11.0` with `12.0`
  (3 occurrences)

These files are generated but survive between builds. They are recreated by `flet clean`, so the
patch has to be reapplied after one. Consequence: **the app requires macOS 12 or newer.**

**Close Finder windows on `build/macos` while building.** Flet deletes that folder before copying the
new bundle; a Finder window showing it writes a fresh `.DS_Store` in the middle of that, the delete
fails with `Errno 66 Directory not empty`, and Flet's error handler (written for read-only files on
Windows) then sets the folder to write-only (`d-w-------`). Recovery:

```zsh
chmod 755 build/macos && rm -f build/macos/.DS_Store && rmdir build/macos
```

then build again. Nothing reaches Apple before the copy step, so a failed run costs no notarization.

Notarization of the first submission from a new account took about 43 minutes. The second one took two minutes. Later ones are
usually a few minutes.

### Verifying the result

```zsh
codesign -dv --verbose=4 build/macos/sqltransfer.app   # expect flags=0x10000(runtime), TeamIdentifier
spctl -a -vvv -t exec build/macos/sqltransfer.app      # expect: accepted, source=Notarized Developer ID
xcrun stapler validate build/macos/sqltransfer.app
```

The honest test is a copy that carries the quarantine flag, the state a download arrives in:

```zsh
ditto -x -k build/sqltransfer-0.1.0-arm64.zip /tmp/recv
xattr -w com.apple.quarantine "0081;00000000;Safari;" /tmp/recv/sqltransfer.app
spctl -a -vvv -t exec /tmp/recv/sqltransfer.app        # must still say accepted
```

### Handing it out

Pack with `ditto`, not with the Finder, which breaks the signature:

```zsh
ditto -c -k --keepParent build/macos/sqltransfer.app build/sqltransfer-0.1.0-arm64.zip
```

Recipients open it normally. No right-click, no trip through System Settings, no
`xattr -dr com.apple.quarantine`.

## 6) First run checks

After building:

1. Open the app and test `Test SSH connection` in Step 1.
2. Test `Test DB form` / `Test SSH tunnel target` in DB profile setup.
3. Run a small table transfer first.

## Notes

- Keep using `.venv314` for running, testing, and packaging to avoid dependency drift.
- If app startup fails after packaging, run from Terminal once to capture logs and missing module errors.
- If SSH tunneling errors return, confirm `paramiko` version is `<4` in the active build env.
