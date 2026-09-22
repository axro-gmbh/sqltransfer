"""Raise the macOS deployment target of the generated Flutter project to 12.0.

Xcode 26 and newer refuse deployment targets below 12.0, while Flet's template
writes 11.0 and the pods 10.15. `flet build` regenerates build/flutter whenever
its inputs change (a new Flutter dependency, for example), which drops this fix.
Safe to run any number of times:

    python scripts/patch_macos_target.py

Prints what it changed and exits 0; exits 1 if build/flutter/macos is missing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = "12.0"
MACOS = Path(__file__).resolve().parents[1] / "build" / "flutter" / "macos"

POD_FIX = (
    "  # Xcode 26+ needs 12.0; added by scripts/patch_macos_target.py\n"
    "  installer.pods_project.targets.each do |target|\n"
    "    target.build_configurations.each do |config|\n"
    f"      config.build_settings['MACOSX_DEPLOYMENT_TARGET'] = '{TARGET}'\n"
    "    end\n"
    "  end\n"
)


def patch_podfile(path: Path) -> list[str]:
    text = original = path.read_text(encoding="utf-8")
    text = re.sub(r"platform :osx, '[\d.]+'", f"platform :osx, '{TARGET}'", text)
    if "patch_macos_target.py" not in text:
        # CocoaPods allows a single post_install hook, so extend the existing one,
        # at its end: Flutter's own settings in that hook must not override ours.
        hook = text.find("post_install do |installer|\n")
        tail = text[hook:].splitlines()[1:]
        if hook < 0 or text.count("post_install do") != 1 or tail[-1] != "end" \
                or any(line and not line.startswith(" ") for line in tail[:-1]):
            raise SystemExit(f"{path}: expected one post_install hook as the last block")
        text = text.rstrip("\n")[: -len("end")] + POD_FIX + "end\n"
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


def main() -> int:
    if not MACOS.is_dir():
        print(f"{MACOS} does not exist yet: run flet build once", file=sys.stderr)
        return 1
    changed = patch_podfile(MACOS / "Podfile") + patch_pbxproj(MACOS / "Runner.xcodeproj" / "project.pbxproj")
    for entry in changed:
        print(f"patched {entry}")
    if not changed:
        print(f"deployment target already {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
