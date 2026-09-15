from __future__ import annotations

from pathlib import Path

from sqltransfer_app.models import DBProfile, SSHProfile
from sqltransfer_app.storage import Storage


def test_storage_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "profiles.db")

    ssh_id = storage.save_ssh_profile(
        SSHProfile(
            id=None,
            name="ssh-prod",
            host="ssh.example.com",
            port=22,
            username="deployer",
            private_key_path="/tmp/id_ed25519",
        )
    )
    assert ssh_id > 0

    db_id = storage.save_db_profile(
        DBProfile(
            id=None,
            name="remote-mysql",
            role="remote",
            db_type="mysql",
            host="db.example.com",
            port=3306,
            database="shop",
            username="root",
            use_ssh=True,
            ssh_profile_id=ssh_id,
        )
    )
    assert db_id > 0

    remotes = storage.list_db_profiles(role="remote")
    assert len(remotes) == 1
    assert remotes[0].ssh_profile_id == ssh_id


def test_transfer_history_and_deletes(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "profiles.db")

    src_id = storage.save_db_profile(
        DBProfile(
            id=None,
            name="src-postgres",
            role="remote",
            db_type="postgres",
            host="source",
            port=5432,
            database="app",
            username="app",
        )
    )
    dst_id = storage.save_db_profile(
        DBProfile(
            id=None,
            name="dst-postgres",
            role="local",
            db_type="postgres",
            host="local",
            port=5432,
            database="app",
            username="app",
        )
    )

    storage.insert_transfer_run(
        started_at="2026-07-23T10:00:00",
        source_profile_id=src_id,
        destination_profile_id=dst_id,
        table_name="public.events",
        scope_mode="schema",
        scope_value="public",
        status="success",
        rows_copied=10,
        elapsed_ms=12,
        message="ok",
    )
    runs = storage.list_recent_runs()
    assert runs[0]["scope_mode"] == "schema"
    assert runs[0]["scope_value"] == "public"

    storage.delete_db_profile(src_id)
    assert storage.get_db_profile(src_id) is None


