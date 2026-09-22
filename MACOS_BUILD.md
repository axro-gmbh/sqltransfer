# Build macOS App (`sqltransfer`)

This guide packages the project as a macOS desktop app.

## 1) Environment and apitap

Set up the environment and build apitap from source exactly as in the README section
[Development](README.md#development). `flet build` takes apitap from the wheel that step leaves in
`.vendor/`.

## 2) Validate before packaging

```zsh
cd sqltransfer
source .venv314/bin/activate
python -m pytest -q
```

## 3) Third-party notices

Regenerate the notices from the environment you build with, so they list exactly what ships:

```zsh
python scripts/third_party_notices.py
```

Commit `THIRD_PARTY_NOTICES.txt` if it changed. It lands in the bundle next to the app code.

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
cd sqltransfer
.venv314/bin/flet build macos --arch arm64 --module-name run \
  --exclude .venv .venv314 .vendor build tests .git .pytest_cache .idea patches dist --yes
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
cd sqltransfer
.venv314/bin/flet build macos --arch arm64 --module-name run \
  --exclude .venv .venv314 .vendor build tests .git .pytest_cache .idea patches dist --yes \
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
10.15. The fix goes into the generated project under `build/flutter/macos` (`Podfile` and
`Runner.xcodeproj/project.pbxproj`):

```zsh
python scripts/patch_macos_target.py
```

Flet regenerates that project whenever its inputs change (`flet clean`, a new Flutter dependency),
which drops the fix: the build then fails with `The macOS deployment target
'MACOSX_DEPLOYMENT_TARGET' is set to 11.0`. Run the script and build again; `scripts/release.sh`
does both on its own. Consequence: **the app requires macOS 12 or newer.**

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
ditto -x -k build/sqltransfer-<version>-arm64.zip /tmp/recv
xattr -w com.apple.quarantine "0081;00000000;Safari;" /tmp/recv/sqltransfer.app
spctl -a -vvv -t exec /tmp/recv/sqltransfer.app        # must still say accepted
```

### Handing it out

Pack with `ditto`, not with the Finder, which breaks the signature:

```zsh
ditto -c -k --keepParent build/macos/sqltransfer.app build/sqltransfer-<version>-arm64.zip
```

Recipients open it normally. No right-click, no trip through System Settings, no
`xattr -dr com.apple.quarantine`.

## Releases and updates

Installed apps update themselves through [Sparkle](https://sparkle-project.org). The Flutter
plugin `auto_updater` (see `[tool.flet.flutter.pubspec.dependencies]`) starts Sparkle's standard
updater on launch, which reads two keys from `[tool.flet.macos.info]`:

- `SUFeedURL`: `https://github.com/axro-gmbh/sqltransfer/releases/latest/download/appcast.xml`,
  the appcast attached to the newest GitHub release
- `SUPublicEDKey`: the public half of the EdDSA key every release is signed with

Sparkle checks once a day, shows its update dialog and swaps the app in place. It only installs
archives signed with the matching private key, and compares **build numbers**
(`[tool.flet].build_number`, `CFBundleVersion`), not version strings. There is no "Check for
updates" menu item: Flet has no hook for it.

### One-time setup

1. **Sparkle tools**, into `.vendor/sparkle` (only the command line tools are used; the framework in
   the app comes from CocoaPods):
   ```zsh
   mkdir -p .vendor/sparkle && cd .vendor/sparkle
   curl -LO https://github.com/sparkle-project/Sparkle/releases/download/2.10.0/Sparkle-2.10.0.tar.xz
   shasum -a 256 Sparkle-2.10.0.tar.xz   # c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c
   tar -xf Sparkle-2.10.0.tar.xz
   ```
2. **Signing key.** Exists once, in the login keychain of the machine that releases, under the
   account `sqltransfer`. Its public key is `SUPublicEDKey`. Print it with
   `.vendor/sparkle/bin/generate_keys --account sqltransfer -p`.

   **Back it up.** Without the private key no installed app accepts another update; a new key
   means everybody downloads the next version by hand once. Export it into a password manager or
   another safe place, never into the repository:
   ```zsh
   .vendor/sparkle/bin/generate_keys --account sqltransfer -x sparkle-private-key.txt
   ```
   and import it on another machine with `-f sparkle-private-key.txt`.
3. The notary profile and signing identity from [Signing and distribution](#signing-and-distribution),
   and `gh auth login`.

### Every release

1. Raise `version` in `[project]` **and** `build_number` in `[tool.flet]` (by one), plus
   `__version__` in `src/sqltransfer_app/__init__.py` (a test checks they agree).
2. Merge to `main` as usual.
3. On `main`:
   ```zsh
   scripts/release.sh            # tests, notices, signed build, zip and appcast in dist/<version>/
   scripts/release.sh --publish  # the same, then the GitHub release with both files
   ```
   The script refuses a build number that is not above the published one, a dirty or outdated
   `main`, an existing tag, and changed third-party notices.

Only the newest release carries an appcast, and it lists only itself: that is all Sparkle needs.
Deleting the newest release makes the feed point at the one before, so installed apps simply see no
update.

**Testing an update without GitHub.** Build two versions (`flet build ... --build-version 1.0.1
--build-number 2` leaves `pyproject.toml` alone), zip the newer one into a folder, run
`generate_appcast --account sqltransfer --download-url-prefix http://127.0.0.1:8765/ <folder>` and
serve the folder with `python3 -m http.server 8765 --bind 127.0.0.1`. In a copy of the older app,
point `SUFeedURL` in `Contents/Info.plist` at `http://127.0.0.1:8765/appcast.xml` and re-sign it
ad hoc (`codesign --force --deep -s -`); Sparkle ignores a feed URL in the user defaults. Then:
```zsh
defaults write de.axro.sqltransfer SUAutomaticallyUpdate -bool true
defaults write de.axro.sqltransfer SULastCheckTime -date "2020-01-01 00:00:00 +0000"
```
start the copy, wait for the download (`log show --last 2m --predicate 'subsystem BEGINSWITH
"org.sparkle-project"'`), quit it, and read its `CFBundleShortVersionString`. Remove both
defaults afterwards, they apply to the real app as well. Ad hoc signed builds log a code signature
mismatch; Sparkle installs anyway because the EdDSA signature is valid.

## 6) First run checks

After building:

1. Open the app and test `Test SSH connection` in Step 1.
2. Test `Test DB form` / `Test SSH tunnel target` in DB profile setup.
3. Run a small table transfer first.

## Notes

- Keep using `.venv314` for running, testing, and packaging to avoid dependency drift.
- If app startup fails after packaging, run from Terminal once to capture logs and missing module errors.
- If SSH tunneling errors return, confirm `paramiko` version is `<4` in the active build env.
