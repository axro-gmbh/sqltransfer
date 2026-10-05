"""The real app, driven end to end against a real MySQL.

Every other integration test calls the service methods in the order app.py calls them,
which proves the pieces work but not that the app still wires them that way. This one
clicks the app itself: profiles through the dialog, scope "Whole database", Run, and
then looks at what landed in the destination.

    SQLTRANSFER_TEST_MYSQL=127.0.0.1:3398:root:test pytest tests/test_real_run_integration.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

pymysql = pytest.importorskip("pymysql")
pytest.importorskip("apitap")
import flet as ft

from driver_app import FakePage, FakeSecrets, button, click, find, fire, log_text
from sqltransfer_app import app as app_module

_SPEC = os.environ.get("SQLTRANSFER_TEST_MYSQL")
pytestmark = pytest.mark.skipif(not _SPEC, reason="SQLTRANSFER_TEST_MYSQL not set")

SRC_DB = "sqlt_real_src"
DST_DB = "sqlt_real_dst"


def _args() -> dict:
    host, port, user, password = _SPEC.split(":", 3)
    return {"host": host, "port": int(port), "user": user, "password": password}


def _sql(database: str | None, *statements: str):
    conn = pymysql.connect(**_args(), database=database, autocommit=True)
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture()
def databases():
    _sql(None, f"DROP DATABASE IF EXISTS {SRC_DB}", f"DROP DATABASE IF EXISTS {DST_DB}",
         f"CREATE DATABASE {SRC_DB}", f"CREATE DATABASE {DST_DB}")
    yield
    _sql(None, f"DROP DATABASE IF EXISTS {SRC_DB}", f"DROP DATABASE IF EXISTS {DST_DB}")


class SharedSecrets(FakeSecrets):
    """One store for the whole test: a second app start must still find the password."""

    store: dict[str, str] = {}

    def __init__(self) -> None:
        super().__init__()
        self.store = SharedSecrets.store


async def _run_whole_database(page: FakePage, *, create_profiles: bool = True) -> str:
    """Click through the app the way a user does; returns the final status line."""
    await app_module.main(page)
    root = page.controls

    def profile(name: str, database: str) -> None:
        click(button(root, "New database profile"))
        dialog = page.dialog
        for label, value in (("Profile name", name), ("DB host", _args()["host"]),
                             ("DB port", str(_args()["port"])), ("Database name", database),
                             ("DB username", _args()["user"]), ("DB password", _args()["password"])):
            find(dialog, ft.TextField, label=label).value = value
        click(button(dialog, "Save"))

    if create_profiles:
        profile("quelle", SRC_DB)
        profile("ziel", DST_DB)
    source = find(root, ft.Dropdown, label="Source")
    destination = find(root, ft.Dropdown, label="Destination")
    source.value = next(o.key for o in source.options if o.text == "quelle")
    destination.value = next(o.key for o in destination.options if o.text == "ziel")
    fire(destination, "on_select")
    find(root, ft.Switch, label="Anonymize personal data").value = False

    scope = find(root, ft.SegmentedButton)
    scope.selected = ["all"]
    scope.on_change(None)
    find(root, ft.TextField, label="Schema or database").value = SRC_DB

    click(button(root, "Run transfer"))
    status = ""
    for _ in range(120):
        await asyncio.sleep(0.5)
        status = find(root, ft.Text, size=13, expand=True).value
        if "Done" in status or "failed" in status.lower() or "cancelled" in status.lower():
            break
    for task in page.tasks:
        task.cancel()
    return status


def test_a_whole_database_run_restores_the_definitions(databases, monkeypatch):
    _sql(SRC_DB,
         """CREATE TABLE neu (
              id INT PRIMARY KEY,
              zeit DATETIME NOT NULL,
              tag DATE AS (CAST(zeit AS DATE)) STORED,
              angelegt DATETIME DEFAULT CURRENT_TIMESTAMP,
              betrag DECIMAL(10,2) DEFAULT 1.00)""",
         "INSERT INTO neu (id, zeit) VALUES (1,'2026-01-01 10:00:00')",
         # a table pointing at it, so the second run takes the in-place path
         "CREATE TABLE zeiger (id INT PRIMARY KEY, neu_id INT, CONSTRAINT fk_z FOREIGN KEY (neu_id) REFERENCES neu(id))")

    tmp = Path(tempfile.mkdtemp(prefix="sqlt-real-"))
    monkeypatch.setattr(app_module, "_app_data_dir", lambda: tmp)
    SharedSecrets.store = {}
    monkeypatch.setattr(app_module, "SecretStore", SharedSecrets)

    page = FakePage()
    status = asyncio.run(_run_whole_database(page))
    assert "Done" in status, f"{status} | {log_text(page.controls)[-400:]}"

    ddl = _sql(DST_DB, "SHOW CREATE TABLE neu")[0][1]
    assert "GENERATED ALWAYS AS (cast(`zeit` as date)) STORED" in ddl, ddl
    assert "DEFAULT CURRENT_TIMESTAMP" in ddl, ddl
    assert "DEFAULT '1.00'" in ddl, ddl

    # the destination now has a table referencing `neu`, so a second run goes in place
    _sql(DST_DB, "INSERT INTO zeiger VALUES (1, 1)")
    _sql(SRC_DB, "INSERT INTO neu (id, zeit) VALUES (2,'2026-02-02 11:00:00')")

    page = FakePage()
    status = asyncio.run(_run_whole_database(page, create_profiles=False))
    assert "Done" in status, f"{status} | {log_text(page.controls)[-400:]}"
    assert "In-place replace done for neu" in log_text(page.controls)

    ddl = _sql(DST_DB, "SHOW CREATE TABLE neu")[0][1]
    assert "GENERATED ALWAYS AS (cast(`zeit` as date)) STORED" in ddl, ddl
    rows = _sql(DST_DB, "SELECT id, tag FROM neu ORDER BY id")
    assert [(r[0], str(r[1])) for r in rows] == [(1, "2026-01-01"), (2, "2026-02-02")]


def test_a_failed_definition_read_stops_the_run(databases, monkeypatch):
    """Silently copying on is worse than stopping.

    The copy would look finished while every generated column, default and check is
    missing, and the run would still say "Done".
    """
    _sql(SRC_DB, "CREATE TABLE neu (id INT PRIMARY KEY, zeit DATETIME NOT NULL, "
                 "tag DATE AS (CAST(zeit AS DATE)) STORED)",
         "INSERT INTO neu (id, zeit) VALUES (1,'2026-01-01 10:00:00')")

    tmp = Path(tempfile.mkdtemp(prefix="sqlt-real-"))
    monkeypatch.setattr(app_module, "_app_data_dir", lambda: tmp)
    SharedSecrets.store = {}
    monkeypatch.setattr(app_module, "SecretStore", SharedSecrets)

    async def boom(self, source, tables):
        return False, {}, "information_schema timed out"

    monkeypatch.setattr(app_module.TransferService, "table_definitions", boom)

    page = FakePage()
    status = asyncio.run(_run_whole_database(page))
    assert "failed" in status.lower(), status
    assert "information_schema timed out" in log_text(page.controls)
