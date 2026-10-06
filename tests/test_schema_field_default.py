"""The schema field must not carry a PostgreSQL default into a MySQL run.

It was pre-filled with "public". MySQL ignored the field, so that was harmless, until
the field was given precedence over the profile's database: from then on a MySQL source
was searched for a schema called "public" and the listing came back empty.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

import flet as ft

sys.path.insert(0, str(Path(__file__).parent))
from driver_app import FakePage, FakeSecrets, find  # noqa: E402

from sqltransfer_app import app as app_module  # noqa: E402


def test_the_schema_field_starts_empty(monkeypatch):
    monkeypatch.setattr(app_module, "_app_data_dir", lambda: Path(tempfile.mkdtemp()))
    monkeypatch.setattr(app_module, "SecretStore", FakeSecrets)
    page = FakePage()
    asyncio.run(app_module.main(page))

    feld = find(page.controls, ft.TextField, label="Source schema hint")
    assert (feld.value or "") == "", (
        "empty means: the profile's database for MySQL, public for PostgreSQL"
    )
    for task in page.tasks:
        task.cancel()
