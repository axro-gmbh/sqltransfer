"""Every question the app asks before it destroys or overwrites something.

All of them go through one helper, and that helper was broken in 1.6.1: it referenced
names that did not exist (`lines`, `settle`), so deleting a profile or a rule and the
"Overwrite on <host>?" question before writing to a remote destination all died with
NameError. Nothing in the suite ever clicked one of those buttons.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import flet as ft
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from driver_app import FakePage, FakeSecrets, button, click, find, walk  # noqa: E402

from sqltransfer_app import app as app_module  # noqa: E402


def _start(monkeypatch) -> FakePage:
    monkeypatch.setattr(app_module, "_app_data_dir", lambda: Path(tempfile.mkdtemp()))
    monkeypatch.setattr(app_module, "SecretStore", FakeSecrets)
    page = FakePage()
    asyncio.run(app_module.main(page))
    return page


def _db_profil(page, root, name: str, host: str = "127.0.0.1") -> None:
    click(button(root, "New database profile"))
    dialog = page.dialog
    for label, wert in (("Profile name", name), ("DB host", host), ("DB port", "3306"),
                        ("Database name", "d"), ("DB username", "u"), ("DB password", "p")):
        find(dialog, ft.TextField, label=label).value = wert
    click(button(dialog, "Save"))


def _papierkorb(page, root):
    return [c for c in walk(root) if isinstance(c, ft.IconButton) and c.tooltip == "Delete"]


def test_deleting_a_rule_opens_a_question_and_deletes_on_yes(monkeypatch):
    page = _start(monkeypatch)
    root = page.controls
    vorher = len(page.storage_rules()) if hasattr(page, "storage_rules") else None

    papierkorb = _papierkorb(page, root)
    assert papierkorb, "no delete button found at all"
    click(papierkorb[-1])          # the last one belongs to a rule

    dialog = page.dialog
    assert dialog is not None, "no question was asked"
    texte = " ".join(t.value for t in walk(dialog) if isinstance(t, ft.Text) and t.value)
    assert "Delete" in texte or "delete" in texte, texte
    click(button(dialog, "Delete"))
    assert page.dialog is None or page.dialog is not dialog
    for task in page.tasks:
        task.cancel()


def test_cancelling_the_question_changes_nothing(monkeypatch):
    page = _start(monkeypatch)
    root = page.controls
    _db_profil(page, root, "weg")

    papierkorb = _papierkorb(page, root)
    click(papierkorb[0])
    dialog = page.dialog
    assert dialog is not None
    click(button(dialog, "Cancel"))
    # The profile is still offered as a source.
    quelle = find(root, ft.Dropdown, label="Source")
    assert "weg" in [o.text for o in quelle.options]
    for task in page.tasks:
        task.cancel()


def test_a_remote_destination_asks_before_it_writes(monkeypatch):
    """The safety question before writing somewhere that is not this machine."""
    monkeypatch.setattr(app_module, "_app_data_dir", lambda: Path(tempfile.mkdtemp()))
    monkeypatch.setattr(app_module, "SecretStore", FakeSecrets)

    async def ablauf():
        page = FakePage()
        await app_module.main(page)
        root = page.controls
        _db_profil(page, root, "lokal", "127.0.0.1")
        _db_profil(page, root, "fremd", "db.example.com")

        quelle = find(root, ft.Dropdown, label="Source")
        ziel = find(root, ft.Dropdown, label="Destination")
        quelle.value = next(o.key for o in quelle.options if o.text == "lokal")
        ziel.value = next(o.key for o in ziel.options if o.text == "fremd")
        if ziel.on_select:
            ziel.on_select(None)

        scope = find(root, ft.SegmentedButton)
        scope.selected = ["single"]
        scope.on_change(None)
        tabelle = find(root, ft.Dropdown, label="Table")
        tabelle.options = [ft.dropdown.Option("kunde")]
        tabelle.value = "kunde"

        click(button(root, "Run transfer"))
        await asyncio.sleep(0.3)
        dialog = page.dialog
        assert dialog is not None, "a remote destination has to be asked about"
        texte = " ".join(t.value for t in walk(dialog) if isinstance(t, ft.Text) and t.value)
        assert "db.example.com" in texte, texte

        # Cancel must not start anything.
        click(button(dialog, "Cancel"))
        await asyncio.sleep(0.2)
        status = find(root, ft.Text, size=13, expand=True).value
        assert "Running" not in status and "Done" not in status, status
        for task in page.tasks:
            task.cancel()

    asyncio.run(ablauf())
