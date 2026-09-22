from __future__ import annotations

import importlib.util
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
bump_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_version)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    # A copy of the real files, so the test also catches layout changes in them.
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "src" / "sqltransfer_app").mkdir(parents=True)
    shutil.copy(ROOT / "src" / "sqltransfer_app" / "__init__.py", tmp_path / "src" / "sqltransfer_app" / "__init__.py")
    return tmp_path


def _state(root: Path) -> tuple[str, int, str]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    init = (root / "src" / "sqltransfer_app" / "__init__.py").read_text(encoding="utf-8")
    return pyproject["project"]["version"], pyproject["tool"]["flet"]["build_number"], init


def test_bump_sets_all_three_and_raises_build(project):
    old_version, old_build, _ = _state(project)
    major, minor, patch = (int(p) for p in old_version.split("."))
    new = f"{major}.{minor}.{patch + 1}"

    assert bump_version.bump(project, new) == (old_version, old_build, old_build + 1)
    version, build, init = _state(project)
    assert (version, build) == (new, old_build + 1)
    assert f'__version__ = "{new}"' in init


def test_bump_keeps_everything_else(project):
    before = (project / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    old_version, _, _ = _state(project)
    major, minor, _patch = (int(p) for p in old_version.split("."))
    bump_version.bump(project, f"{major}.{minor + 1}.0")
    after = (project / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after) and len(changed) == 2


@pytest.mark.parametrize("bad", ["1.0", "v1.2.3", "1.0.0-beta", ""])
def test_rejects_malformed_versions(project, bad):
    with pytest.raises(bump_version.BumpError):
        bump_version.bump(project, bad)


def test_rejects_same_or_lower_version_and_writes_nothing(project):
    old_version, _, _ = _state(project)
    before = _state(project)
    for version in (old_version, "0.0.1"):
        with pytest.raises(bump_version.BumpError, match="not higher"):
            bump_version.bump(project, version)
    assert _state(project) == before
