from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, List

from .models import DBProfile, SSHProfile


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ssh_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    private_key_path TEXT NOT NULL,
                    passphrase_secret_key TEXT
                );

                CREATE TABLE IF NOT EXISTS db_profiles (
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
                    tls_mode TEXT NOT NULL DEFAULT 'auto',
                    tls_ca_path TEXT,
                    FOREIGN KEY (ssh_profile_id) REFERENCES ssh_profiles(id)
                );

                CREATE TABLE IF NOT EXISTS transfer_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    source_profile_id INTEGER NOT NULL,
                    destination_profile_id INTEGER NOT NULL,
                    table_name TEXT NOT NULL,
                    scope_mode TEXT NOT NULL DEFAULT 'table',
                    scope_value TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    rows_copied INTEGER NOT NULL DEFAULT 0,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    message TEXT,
                    FOREIGN KEY (source_profile_id) REFERENCES db_profiles(id),
                    FOREIGN KEY (destination_profile_id) REFERENCES db_profiles(id)
                );
                """
            )
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        cols = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(transfer_runs)").fetchall()
        }
        if "scope_mode" not in cols:
            conn.execute("ALTER TABLE transfer_runs ADD COLUMN scope_mode TEXT NOT NULL DEFAULT 'table'")
        if "scope_value" not in cols:
            conn.execute("ALTER TABLE transfer_runs ADD COLUMN scope_value TEXT NOT NULL DEFAULT ''")

        profile_cols = {row["name"] for row in conn.execute("PRAGMA table_info(db_profiles)").fetchall()}
        if "tls_mode" not in profile_cols:
            # 'auto' keeps every existing profile working as before: tunnels and
            # localhost stay unencrypted, which is what they were.
            conn.execute("ALTER TABLE db_profiles ADD COLUMN tls_mode TEXT NOT NULL DEFAULT 'auto'")
        if "tls_ca_path" not in profile_cols:
            conn.execute("ALTER TABLE db_profiles ADD COLUMN tls_ca_path TEXT")

    def save_ssh_profile(self, profile: SSHProfile) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ssh_profiles(name, host, port, username, private_key_path, passphrase_secret_key)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  host=excluded.host,
                  port=excluded.port,
                  username=excluded.username,
                  private_key_path=excluded.private_key_path,
                  passphrase_secret_key=excluded.passphrase_secret_key
                """,
                (
                    profile.name,
                    profile.host,
                    profile.port,
                    profile.username,
                    profile.private_key_path,
                    profile.passphrase_secret_key,
                ),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute("SELECT id FROM ssh_profiles WHERE name = ?", (profile.name,)).fetchone()
            return int(row["id"])

    def list_ssh_profiles(self) -> List[SSHProfile]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, host, port, username, private_key_path, passphrase_secret_key FROM ssh_profiles ORDER BY name"
            ).fetchall()
        return [
            SSHProfile(
                id=int(r["id"]),
                name=str(r["name"]),
                host=str(r["host"]),
                port=int(r["port"]),
                username=str(r["username"]),
                private_key_path=str(r["private_key_path"]),
                passphrase_secret_key=r["passphrase_secret_key"],
            )
            for r in rows
        ]

    def get_ssh_profile(self, profile_id: int) -> SSHProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, host, port, username, private_key_path, passphrase_secret_key FROM ssh_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return SSHProfile(
            id=int(row["id"]),
            name=str(row["name"]),
            host=str(row["host"]),
            port=int(row["port"]),
            username=str(row["username"]),
            private_key_path=str(row["private_key_path"]),
            passphrase_secret_key=row["passphrase_secret_key"],
        )

    def delete_ssh_profile(self, profile_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE db_profiles SET use_ssh = 0, ssh_profile_id = NULL WHERE ssh_profile_id = ?", (profile_id,))
            conn.execute("DELETE FROM ssh_profiles WHERE id = ?", (profile_id,))

    def save_db_profile(self, profile: DBProfile) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO db_profiles(
                  name, role, db_type, host, port, database_name, username,
                  password_secret_key, use_ssh, ssh_profile_id, tls_mode, tls_ca_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  role=excluded.role,
                  db_type=excluded.db_type,
                  host=excluded.host,
                  port=excluded.port,
                  database_name=excluded.database_name,
                  username=excluded.username,
                  password_secret_key=excluded.password_secret_key,
                  use_ssh=excluded.use_ssh,
                  ssh_profile_id=excluded.ssh_profile_id,
                  tls_mode=excluded.tls_mode,
                  tls_ca_path=excluded.tls_ca_path
                """,
                (
                    profile.name,
                    profile.role,
                    profile.db_type,
                    profile.host,
                    profile.port,
                    profile.database,
                    profile.username,
                    profile.password_secret_key,
                    1 if profile.use_ssh else 0,
                    profile.ssh_profile_id,
                    profile.tls_mode or "auto",
                    profile.tls_ca_path or None,
                ),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute("SELECT id FROM db_profiles WHERE name = ?", (profile.name,)).fetchone()
            return int(row["id"])

    def list_db_profiles(self, role: str | None = None) -> List[DBProfile]:
        query = (
            "SELECT id, name, role, db_type, host, port, database_name, username, password_secret_key, use_ssh, "
            "ssh_profile_id, tls_mode, tls_ca_path FROM db_profiles"
        )
        params: Iterable[object] = ()
        if role:
            query += " WHERE role = ?"
            params = (role,)
        query += " ORDER BY name"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            DBProfile(
                id=int(r["id"]),
                name=str(r["name"]),
                role=str(r["role"]),
                db_type=str(r["db_type"]),
                host=str(r["host"]),
                port=int(r["port"]),
                database=str(r["database_name"]),
                username=str(r["username"]),
                password_secret_key=r["password_secret_key"],
                use_ssh=bool(r["use_ssh"]),
                ssh_profile_id=r["ssh_profile_id"],
                tls_mode=r["tls_mode"] or "auto",
                tls_ca_path=r["tls_ca_path"],
            )
            for r in rows
        ]

    def get_db_profile(self, profile_id: int) -> DBProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, role, db_type, host, port, database_name, username,
                       password_secret_key, use_ssh, ssh_profile_id, tls_mode, tls_ca_path
                FROM db_profiles
                WHERE id = ?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return DBProfile(
            id=int(row["id"]),
            name=str(row["name"]),
            role=str(row["role"]),
            db_type=str(row["db_type"]),
            host=str(row["host"]),
            port=int(row["port"]),
            database=str(row["database_name"]),
            username=str(row["username"]),
            password_secret_key=row["password_secret_key"],
            use_ssh=bool(row["use_ssh"]),
            ssh_profile_id=row["ssh_profile_id"],
            tls_mode=row["tls_mode"] or "auto",
            tls_ca_path=row["tls_ca_path"],
        )

    def delete_db_profile(self, profile_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM db_profiles WHERE id = ?", (profile_id,))

    def insert_transfer_run(
        self,
        *,
        started_at: str,
        source_profile_id: int,
        destination_profile_id: int,
        table_name: str,
        scope_mode: str,
        scope_value: str,
        status: str,
        rows_copied: int,
        elapsed_ms: int,
        message: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO transfer_runs(
                    started_at, finished_at, source_profile_id, destination_profile_id,
                    table_name, scope_mode, scope_value, status, rows_copied, elapsed_ms, message
                )
                VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    source_profile_id,
                    destination_profile_id,
                    table_name,
                    scope_mode,
                    scope_value,
                    status,
                    rows_copied,
                    elapsed_ms,
                    message,
                ),
            )

    def list_recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT r.id, r.started_at, r.finished_at, s.name AS source_name,
                       d.name AS destination_name, r.table_name, r.scope_mode,
                       r.scope_value, r.status,
                       r.rows_copied, r.elapsed_ms, r.message
                FROM transfer_runs r
                JOIN db_profiles s ON s.id = r.source_profile_id
                JOIN db_profiles d ON d.id = r.destination_profile_id
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

