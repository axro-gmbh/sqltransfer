#!/bin/bash
# Build, sign, notarize and package a release, and write its Sparkle appcast.
#
#   scripts/release.sh             build into dist/<version>/, publish nothing
#   scripts/release.sh --publish   the same, then create the GitHub release
#
# Version and build number come from pyproject.toml ([project].version and
# [tool.flet].build_number). See MACOS_BUILD.md, "Releases", for the one-time setup.
#
# Environment (optional):
#   SQLTRANSFER_SIGN_IDENTITY   default: the only "Developer ID Application" identity
#   SQLTRANSFER_NOTARY_PROFILE  default: axro-notary
#   SPARKLE_ACCOUNT             keychain account of the EdDSA key, default: sqltransfer
set -euo pipefail

REPO="axro-gmbh/sqltransfer"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=".venv314/bin/python"
FLET=".venv314/bin/flet"
SPARKLE_BIN=".vendor/sparkle/bin"
NOTARY_PROFILE="${SQLTRANSFER_NOTARY_PROFILE:-axro-notary}"
SPARKLE_ACCOUNT="${SPARKLE_ACCOUNT:-sqltransfer}"

PUBLISH=0
[ "${1:-}" = "--publish" ] && PUBLISH=1

die() { echo "release: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[ -x "$PY" ] || die "missing $PY, set up the environment as in README.md"
[ -x "$SPARKLE_BIN/generate_appcast" ] || die "missing $SPARKLE_BIN, see MACOS_BUILD.md, Releases"

VERSION="$("$PY" -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
BUILD="$("$PY" -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["tool"]["flet"]["build_number"])')"
TAG="v$VERSION"
OUT="dist/$VERSION"
ZIP="sqltransfer-$VERSION-arm64.zip"
APP="build/macos/sqltransfer.app"
echo "SQL Transfer $VERSION, build $BUILD"

step "Checks"
if [ "$PUBLISH" = 1 ]; then
  command -v gh >/dev/null || die "gh CLI missing"
  [ "$(git branch --show-current)" = "main" ] || die "--publish only from main"
  git fetch --quiet origin main
  [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "main is not equal to origin/main"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "uncommitted changes"
  ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1 || die "release $TAG exists already"
fi
# Sparkle compares build numbers: a release must raise it, or nobody gets it.
PUBLISHED="$(curl -fsL "https://github.com/$REPO/releases/latest/download/appcast.xml" 2>/dev/null \
  | sed -n 's:.*<sparkle\:version>\([0-9]*\)</sparkle\:version>.*:\1:p' | sort -n | tail -1 || true)"
if [ -n "$PUBLISHED" ]; then
  [ "$BUILD" -gt "$PUBLISHED" ] || die "build_number $BUILD must be greater than the published $PUBLISHED"
  echo "published build: $PUBLISHED"
else
  echo "no published appcast yet"
fi

step "Tests"
"$PY" -m pytest -q

step "Third-party notices"
"$PY" scripts/third_party_notices.py
[ -z "$(git status --porcelain THIRD_PARTY_NOTICES.txt)" ] || die "THIRD_PARTY_NOTICES.txt changed: review and commit it first"

IDENTITY="${SQLTRANSFER_SIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
  IDENTITY="$(security find-identity -v -p codesigning | sed -n 's/.*"\(Developer ID Application: .*\)"/\1/p')"
  [ "$(printf '%s\n' "$IDENTITY" | grep -c .)" = 1 ] || die "set SQLTRANSFER_SIGN_IDENTITY (none or several Developer ID identities)"
fi
echo "signing as: $IDENTITY"

build() {
  # Flet deletes build/macos before copying; a Finder .DS_Store in it breaks that (MACOS_BUILD.md).
  if [ -e build/macos ]; then chmod -R u+rwx build/macos; rm -rf build/macos || rm -rf build/macos; fi
  "$FLET" build macos --arch arm64 --module-name run \
    --exclude .venv .venv314 .vendor build tests .git .pytest_cache .idea patches dist --yes \
    --macos-distribution developer-id \
    --macos-signing-identity "$IDENTITY" \
    --macos-notary-profile "$NOTARY_PROFILE"
}

step "Build, sign, notarize"
if ! build; then
  # flet regenerates build/flutter when its inputs change, which drops the deployment target fix.
  PATCHED="$("$PY" scripts/patch_macos_target.py)" || die "build failed, see above"
  echo "$PATCHED"
  case "$PATCHED" in *patched*) ;; *) die "build failed, see above" ;; esac
  echo "deployment target patched, building again"
  build
fi

step "Verify"
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")" = "$VERSION" ] || die "bundle version mismatch"
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist")" = "$BUILD" ] || die "bundle build number mismatch"
codesign --verify --deep --strict "$APP"
spctl -a -t exec "$APP"
xcrun stapler validate "$APP"

step "Package and appcast"
rm -rf "$OUT"
mkdir -p "$OUT"
ditto -c -k --keepParent "$APP" "$OUT/$ZIP"
# The feed lists only this release: Sparkle needs nothing but the newest one.
"$SPARKLE_BIN/generate_appcast" --account "$SPARKLE_ACCOUNT" \
  --download-url-prefix "https://github.com/$REPO/releases/download/$TAG/" \
  --link "https://github.com/$REPO" "$OUT"
grep -q 'sparkle:edSignature=' "$OUT/appcast.xml" || die "appcast is not signed"
ls -l "$OUT"

if [ "$PUBLISH" = 0 ]; then
  echo
  echo "Built $OUT. To publish: scripts/release.sh --publish (from main), or by hand:"
  echo "  gh release create $TAG $OUT/$ZIP $OUT/appcast.xml --repo $REPO --target main --title \"SQL Transfer $VERSION\" --generate-notes"
  exit 0
fi

step "Publish $TAG"
gh release create "$TAG" "$OUT/$ZIP" "$OUT/appcast.xml" --repo "$REPO" --target main \
  --title "SQL Transfer $VERSION" --generate-notes
echo "Published. Installed apps pick it up at their next daily check."
