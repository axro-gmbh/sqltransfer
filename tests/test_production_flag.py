"""The production marker, clicked through the real app.

A marked profile must not be offered as a destination, must say why it is missing, and
must not slip through when it was selected before the marker was set.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import flet as ft

sys.path.insert(0, str(Path(__file__).parent))
from driver_app import FakePage, FakeSecrets, button, click, find, rows_of, walk  # noqa: E402

from sqltransfer_app import app as app_module  # noqa: E402


def _start(monkeypatch):
    monkeypatch.setattr(app_module, "_app_data_dir", lambda: Path(tempfile.mkdtemp()))
    monkeypatch.setattr(app_module, "SecretStore", FakeSecrets)
    page = FakePage()
    asyncio.run(app_module.main(page))
    return page


def _neues_profil(page, root, name: str, host: str, produktiv: bool) -> None:
    click(button(root, "New database profile"))
    dialog = page.dialog
    for label, wert in (("Profile name", name), ("DB host", host), ("DB port", "3306"),
                        ("Database name", "sw6"), ("DB username", "sw6"), ("DB password", "x")):
        find(dialog, ft.TextField, label=label).value = wert
    find(dialog, ft.Checkbox, label="Production database").value = produktiv
    click(button(dialog, "Save"))


def test_a_marked_profile_is_missing_in_the_destination_list(monkeypatch):
    page = _start(monkeypatch)
    root = page.controls
    _neues_profil(page, root, "prod", "db.intern", True)
    _neues_profil(page, root, "lokal", "127.0.0.1", False)

    quelle = find(root, ft.Dropdown, label="Source")
    ziel = find(root, ft.Dropdown, label="Destination")
    assert "prod" in [o.text for o in quelle.options], "reading from production stays possible"
    assert "prod" not in [o.text for o in ziel.options]
    assert "lokal" in [o.text for o in ziel.options]

    for task in page.tasks:
        task.cancel()


def test_marking_a_chosen_destination_clears_the_choice(monkeypatch):
    page = _start(monkeypatch)
    root = page.controls
    _neues_profil(page, root, "stage", "db.stage", False)
    _neues_profil(page, root, "lokal", "127.0.0.1", False)

    ziel = find(root, ft.Dropdown, label="Destination")
    ziel.value = next(o.key for o in ziel.options if o.text == "stage")

    # Mark exactly "stage" as production, found by its name instead of by row order.
    for stift in [c for c in walk(root) if isinstance(c, ft.IconButton) and c.tooltip == "Edit"]:
        click(stift)
        dialog = page.dialog
        if find(dialog, ft.TextField, label="Profile name").value == "stage":
            find(dialog, ft.Checkbox, label="Production database").value = True
            click(button(dialog, "Save"))
            break
        click(button(dialog, "Cancel"))

    assert ziel.value is None, "a destination that is no longer offered must not stay selected"
    assert "stage" not in [o.text for o in ziel.options]
    for task in page.tasks:
        task.cancel()
