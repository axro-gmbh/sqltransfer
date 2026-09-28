"""Make the generated macOS project ours again, after `flet build` writes it.

Flet generates build/flutter from a template and rewrites it whenever its inputs
change (a new dependency, `flet clean`, a flet upgrade). Three things have to be
put back every time:

1. the deployment target, because Xcode 26 refuses anything below 12.0 while the
   template still says 11.0 and the pods 10.15;
2. the Sparkle pod for the Runner target;
3. the Sparkle updater in AppDelegate, which adds "Check for Updates…" to the app
   menu and keeps the scheduled checks running.

Run it before every build; `scripts/release.sh` does:

    python scripts/patch_macos_runner.py

Safe to run repeatedly. Exits 1 when the project is missing or an anchor it edits
has moved, so a build never silently produces an app without the update menu.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = "12.0"
SPARKLE_POD = "'~> 2.9'"
MARK = "patch_macos_runner.py"
MACOS = Path(__file__).resolve().parents[1] / "build" / "flutter" / "macos"

POD_DEPLOYMENT_FIX = (
    f"  # Xcode 26+ needs {TARGET}; added by {MARK}\n"
    "  installer.pods_project.targets.each do |target|\n"
    "    target.build_configurations.each do |config|\n"
    f"      config.build_settings['MACOSX_DEPLOYMENT_TARGET'] = '{TARGET}'\n"
    "    end\n"
    "  end\n"
)

POD_SPARKLE = (
    f"  # Sparkle for the Runner itself; added by {MARK}\n"
    f"  pod 'Sparkle', {SPARKLE_POD}\n"
)

SWIFT_IMPORT = "import Sparkle\n"

SWIFT_UPDATER = f'''
  // Sparkle lives here rather than in the auto_updater Flutter plugin, so the app
  // menu can carry "Check for Updates…" next to "About". The updater reads
  // SUFeedURL, SUPublicEDKey and SUScheduledCheckInterval from Info.plist and
  // keeps checking on its own schedule. Added by {MARK}.
  private let updaterController = SPUStandardUpdaterController(
    startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)

  override init() {{
    super.init()
    // The menu comes from MainMenu.xib and only exists once the app has launched.
    // Observing the notification avoids depending on which delegate methods
    // FlutterAppDelegate happens to declare.
    NotificationCenter.default.addObserver(
      forName: NSApplication.didFinishLaunchingNotification, object: nil, queue: .main
    ) {{ [weak self] _ in
      self?.addUpdateMenuItem()
    }}
  }}

  private func addUpdateMenuItem() {{
    guard let appMenu = NSApp.mainMenu?.item(at: 0)?.submenu else {{ return }}
    let item = NSMenuItem(
      title: "Check for Updates…",
      action: #selector(SPUStandardUpdaterController.checkForUpdates(_:)),
      keyEquivalent: "")
    item.target = updaterController
    // Right below "About <app>", where every Mac app puts it.
    appMenu.insertItem(item, at: 1)
    appMenu.insertItem(.separator(), at: 2)
  }}
'''


class PatchError(Exception):
    """An anchor this script edits is not where it used to be."""


def patch_podfile(path: Path) -> list[str]:
    text = original = path.read_text(encoding="utf-8")
    text = re.sub(r"platform :osx, '[\d.]+'", f"platform :osx, '{TARGET}'", text)

    if MARK not in text:
        # CocoaPods allows a single post_install hook, so extend the existing one,
        # at its end: Flutter's own settings in that hook must not override ours.
        hook = text.find("post_install do |installer|\n")
        tail = text[hook:].splitlines()[1:] if hook >= 0 else []
        if hook < 0 or text.count("post_install do") != 1 or tail[-1] != "end" \
                or any(line and not line.startswith(" ") for line in tail[:-1]):
            raise PatchError(f"{path}: expected one post_install hook as the last block")
        text = text.rstrip("\n")[: -len("end")] + POD_DEPLOYMENT_FIX + "end\n"

    if "pod 'Sparkle'" not in text:
        anchor = "  use_modular_headers!\n"
        if text.count(anchor) != 1:
            raise PatchError(f"{path}: expected one 'use_modular_headers!' in the Runner target")
        text = text.replace(anchor, anchor + "\n" + POD_SPARKLE, 1)

    if text == original:
        return []
    path.write_text(text, encoding="utf-8")
    return [str(path)]


def patch_pbxproj(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    patched, count = re.subn(
        r"MACOSX_DEPLOYMENT_TARGET = (?!" + re.escape(TARGET) + r";)[\d.]+;",
        f"MACOSX_DEPLOYMENT_TARGET = {TARGET};",
        text,
    )
    if not count:
        return []
    path.write_text(patched, encoding="utf-8")
    return [f"{path} ({count}x)"]


def patch_app_delegate(path: Path) -> list[str]:
    text = original = path.read_text(encoding="utf-8")
    if MARK in text:
        return []

    if SWIFT_IMPORT not in text:
        anchor = "import FlutterMacOS\n"
        if anchor not in text:
            raise PatchError(f"{path}: no 'import FlutterMacOS' to add the Sparkle import after")
        text = text.replace(anchor, anchor + SWIFT_IMPORT, 1)

    opening = "class AppDelegate: FlutterAppDelegate {\n"
    if text.count(opening) != 1:
        raise PatchError(f"{path}: expected one 'class AppDelegate: FlutterAppDelegate' declaration")
    text = text.replace(opening, opening + SWIFT_UPDATER, 1)

    if text == original:
        return []
    path.write_text(text, encoding="utf-8")
    return [str(path)]


def main() -> int:
    if not MACOS.is_dir():
        print(f"{MACOS} does not exist yet: run flet build once", file=sys.stderr)
        return 1
    try:
        changed = (
            patch_podfile(MACOS / "Podfile")
            + patch_pbxproj(MACOS / "Runner.xcodeproj" / "project.pbxproj")
            + patch_app_delegate(MACOS / "Runner" / "AppDelegate.swift")
        )
    except PatchError as exc:
        print(f"patch_macos_runner: {exc}", file=sys.stderr)
        return 1
    for entry in changed:
        print(f"patched {entry}")
    if not changed:
        print("macOS project already patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
