"""Saving profiles: new ones, edits, renames, and the old schema with a role column."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqltransfer_app.models import DBProfile, SSHProfile
from sqltransfer_app.storage import DuplicateProfileName, ProfileGone, Storage


def _db(**overrides) -> DBProfile:
    values = dict(
        id=None,
        name="prod",
        db_type="mysql",
        host="db.example.com",
        port=3306,
        database="shop",
        username="reader",
    )
    values.update(overrides)
    return DBProfile(**values)


def _ssh(**overrides) -> SSHProfile:
    values = dict(id=None, name="jump", host="jump.example.com", port=22, username="deploy", private_key_path="~/.ssh/id")
    values.update(overrides)
    return SSHProfile(**values)


def test_a_second_profile_with_the_same_name_is_refused(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    storage.save_db_profile(_db(host="first.example.com"))

    with pytest.raises(DuplicateProfileName):
        storage.save_db_profile(_db(host="second.example.com"))

    # The first one is untouched: this is the accident the refusal exists for.
    assert [p.host for p in storage.list_db_profiles()] == ["first.example.com"]


def test_saving_with_an_id_edits_that_profile_and_can_rename_it(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    pid = storage.save_db_profile(_db())

    storage.save_db_profile(_db(id=pid, name="prod-shop", host="new.example.com", port=3307))

    profiles = storage.list_db_profiles()
    assert len(profiles) == 1
    assert (profiles[0].id, profiles[0].name, profiles[0].host, profiles[0].port) == (pid, "prod-shop", "new.example.com", 3307)


def test_renaming_onto_another_profile_is_refused(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    first = storage.save_db_profile(_db(name="one"))
    storage.save_db_profile(_db(name="two"))

    with pytest.raises(DuplicateProfileName):
        storage.save_db_profile(_db(id=first, name="two"))
    assert sorted(p.name for p in storage.list_db_profiles()) == ["one", "two"]


def test_editing_a_deleted_profile_says_so(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    pid = storage.save_db_profile(_db())
    storage.delete_db_profile(pid)

    with pytest.raises(ProfileGone):
        storage.save_db_profile(_db(id=pid, name="prod"))


def test_ssh_profiles_behave_the_same_way(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    sid = storage.save_ssh_profile(_ssh())

    with pytest.raises(DuplicateProfileName):
        storage.save_ssh_profile(_ssh(host="other.example.com"))

    storage.save_ssh_profile(_ssh(id=sid, name="jump-2", host="other.example.com"))
    profiles = storage.list_ssh_profiles()
    assert [(p.id, p.name, p.host) for p in profiles] == [(sid, "jump-2", "other.example.com")]

    with pytest.raises(ProfileGone):
        storage.save_ssh_profile(_ssh(id=sid + 99, name="ghost"))


OLD_SCHEMA = """
CREATE TABLE ssh_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT NOT NULL,
    private_key_path TEXT NOT NULL,
    passphrase_secret_key TEXT
);
CREATE TABLE db_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('remote', 'local')),
    db_type TEXT NOT NULL CHECK (db_type IN ('mysql', 'postgres')),
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    database_name TEXT NOT NULL,
    username TEXT NOT NULL,
    password_secret_key TEXT,
    use_ssh INTEGER NOT NULL DEFAULT 0,
    ssh_profile_id INTEGER,
    FOREIGN KEY (ssh_profile_id) REFERENCES ssh_profiles(id)
);
"""


def _old_database(path: Path) -> None:
    """A database as an installed 1.0.1 left it: role column, no TLS columns."""
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO ssh_profiles(id, name, host, port, username, private_key_path) VALUES (7,'jump','j.example.com',22,'deploy','~/.ssh/id')"
        )
        conn.executemany(
            "INSERT INTO db_profiles(id, name, role, db_type, host, port, database_name, username, password_secret_key, use_ssh, ssh_profile_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "prod", "remote", "mysql", "db.example.com", 3306, "shop", "reader", "db:prod:password", 1, 7),
                (2, "local", "local", "mysql", "127.0.0.1", 3306, "shop", "root", None, 0, None),
            ],
        )


def test_an_existing_database_keeps_its_profiles_when_role_goes_away(tmp_path: Path):
    path = tmp_path / "profiles.db"
    _old_database(path)

    storage = Storage(path)

    profiles = {p.name: p for p in storage.list_db_profiles()}
    assert set(profiles) == {"prod", "local"}
    prod = profiles["prod"]
    # Ids stay put: transfer_runs rows point at them.
    assert (prod.id, prod.host, prod.port, prod.database, prod.username) == (1, "db.example.com", 3306, "shop", "reader")
    assert (prod.password_secret_key, prod.use_ssh, prod.ssh_profile_id) == ("db:prod:password", True, 7)
    assert (prod.tls_mode, prod.tls_ca_path) == ("auto", None)
    assert profiles["local"].use_ssh is False

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(db_profiles)")}
    assert "role" not in columns
    assert {"tls_mode", "tls_ca_path"} <= columns

    # Still usable afterwards, including the unique name it inherited.
    storage.save_db_profile(_db(id=1, name="prod", host="moved.example.com"))
    assert storage.get_db_profile(1).host == "moved.example.com"
    with pytest.raises(DuplicateProfileName):
        storage.save_db_profile(_db(name="local"))


def test_opening_an_already_migrated_database_changes_nothing(tmp_path: Path):
    path = tmp_path / "profiles.db"
    _old_database(path)
    Storage(path)
    before = [(p.id, p.name, p.host) for p in Storage(path).list_db_profiles()]

    assert [(p.id, p.name, p.host) for p in Storage(path).list_db_profiles()] == before
