from __future__ import annotations

import sqlite3
from pathlib import Path

from sqltransfer_app.models import DBProfile
from sqltransfer_app.storage import Storage


def _profile(**overrides) -> DBProfile:
    values = dict(
        id=None,
        name="pg-direct",
        db_type="postgres",
        host="db.example.com",
        port=5432,
        database="shop",
        username="reader",
    )
    values.update(overrides)
    return DBProfile(**values)


def test_encryption_settings_are_stored_and_read_back(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    pid = storage.save_db_profile(_profile(tls_mode="verified", tls_ca_path="/etc/ca.pem"))

    loaded = storage.get_db_profile(pid)
    assert (loaded.tls_mode, loaded.tls_ca_path) == ("verified", "/etc/ca.pem")
    listed = storage.list_db_profiles()[0]
    assert (listed.tls_mode, listed.tls_ca_path) == ("verified", "/etc/ca.pem")


def test_new_profiles_default_to_automatic(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    pid = storage.save_db_profile(_profile())
    loaded = storage.get_db_profile(pid)
    assert (loaded.tls_mode, loaded.tls_ca_path) == ("auto", None)


def test_updating_a_profile_changes_its_encryption(tmp_path: Path):
    storage = Storage(tmp_path / "profiles.db")
    pid = storage.save_db_profile(_profile(tls_mode="required"))
    storage.save_db_profile(_profile(id=pid, tls_mode="off"))
    assert storage.list_db_profiles()[0].tls_mode == "off"


def test_existing_database_without_the_columns_is_migrated(tmp_path: Path):
    # A profiles.db from before this change, like the one on every machine today.
    db = tmp_path / "profiles.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE db_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL, db_type TEXT NOT NULL, host TEXT NOT NULL,
            port INTEGER NOT NULL, database_name TEXT NOT NULL, username TEXT NOT NULL,
            password_secret_key TEXT, use_ssh INTEGER NOT NULL DEFAULT 0, ssh_profile_id INTEGER
        );
        INSERT INTO db_profiles (name, role, db_type, host, port, database_name, username, use_ssh)
        VALUES ('old', 'remote', 'mysql', '127.0.0.1', 3306, 'shop', 'reader', 1);
        """
    )
    conn.close()

    storage = Storage(db)
    old = storage.list_db_profiles()[0]
    assert (old.name, old.tls_mode, old.tls_ca_path) == ("old", "auto", None)
