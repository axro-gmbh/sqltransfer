from __future__ import annotations

import tomllib
from pathlib import Path

import sqltransfer_app


def test_app_version_matches_pyproject():
    # flet build stamps the bundle with project.version; the app shows __version__.
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert sqltransfer_app.__version__ == pyproject["project"]["version"]
