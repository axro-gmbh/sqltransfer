"""Set a new release version everywhere it lives and raise the build number by one.

    python scripts/bump_version.py 1.0.2

Writes [project].version and [tool.flet].build_number in pyproject.toml and
__version__ in src/sqltransfer_app/__init__.py. Refuses a version that is not
higher than the current one. Commits nothing.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"\d+\.\d+\.\d+")


class BumpError(Exception):
    pass


def _parse(version: str) -> tuple[int, ...]:
    if not SEMVER.fullmatch(version):
        raise BumpError(f"{version!r} is not a version like 1.2.3")
    return tuple(int(part) for part in version.split("."))


def _replace_once(text: str, pattern: str, replacement: str, where: str) -> str:
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise BumpError(f"{where}: pattern {pattern!r} not found")
    return new


def bump(root: Path, version: str) -> tuple[str, int, int]:
    """Write the new version; returns (old version, old build, new build)."""
    pyproject_path = root / "pyproject.toml"
    init_path = root / "src" / "sqltransfer_app" / "__init__.py"
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)
    old_version = pyproject["project"]["version"]
    old_build = int(pyproject["tool"]["flet"]["build_number"])
    if _parse(version) <= _parse(old_version):
        raise BumpError(f"{version} is not higher than the current {old_version}")
    new_build = old_build + 1

    # Line-based edits keep the file's comments and layout; tomllib has no writer.
    pyproject_text = _replace_once(pyproject_text, r'^version = "[^"]*"$', f'version = "{version}"', "pyproject.toml")
    pyproject_text = _replace_once(pyproject_text, r"^build_number = \d+$", f"build_number = {new_build}", "pyproject.toml")
    init_text = _replace_once(init_path.read_text(encoding="utf-8"), r'^__version__ = "[^"]*"$',
                              f'__version__ = "{version}"', "__init__.py")

    check = tomllib.loads(pyproject_text)
    if check["project"]["version"] != version or check["tool"]["flet"]["build_number"] != new_build:
        raise BumpError("pyproject.toml edit hit the wrong line, nothing written")
    pyproject_path.write_text(pyproject_text, encoding="utf-8")
    init_path.write_text(init_text, encoding="utf-8")
    return old_version, old_build, new_build


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        old_version, old_build, new_build = bump(ROOT, argv[0])
    except BumpError as exc:
        print(f"bump_version: {exc}", file=sys.stderr)
        return 1
    print(f"{old_version} (build {old_build}) -> {argv[0]} (build {new_build})")
    print("changed: pyproject.toml, src/sqltransfer_app/__init__.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
