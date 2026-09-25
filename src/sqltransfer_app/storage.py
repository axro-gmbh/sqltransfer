from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

from .models import DBProfile, SSHProfile

DB_PROFILE_COLUMNS = (
    "name, db_type, host, port, database_name, username, "
    "password_secret_key, use_ssh, ssh_profile_id, tls_mode, tls_ca_path"
)


class DuplicateProfileName(ValueError):
    """Another profile already carries this name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"A profile named '{name}' already exists")
        self.name = name


class ProfileGone(LookupError):
    """The profile being edited is no longer in the database."""


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
        if "role" in profile_cols:
            self._drop_role_column(conn)

    def _drop_role_column(self, conn: sqlite3.Connection) -> None:
        """Every profile can be source and destination now, so the role is gone.

        Rebuilt rather than dropped in place, because the old column carries a
        CHECK constraint and the ids have to survive: transfer_runs points at them.
        """
        conn.executescript(
            f"""
            CREATE TABLE db_profiles_rebuilt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
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
            INSERT INTO db_profiles_rebuilt (id, {DB_PROFILE_COLUMNS})
                SELECT id, {DB_PROFILE_COLUMNS} FROM db_profiles;
            DROP TABLE db_profiles;
            ALTER TABLE db_profiles_rebuilt RENAME TO db_profiles;
            """
        )

    def save_ssh_profile(self, profile: SSHProfile) -> int:
        """Insert when the profile has no id, otherwise edit exactly that row.

        Saving used to key on the name, which silently overwrote a profile when a
        form still held an old name. An id now means "edit this one" and no id
        means "add one", and a name collision is refused either way.
        """
        values = (
            profile.name,
            profile.host,
            profile.port,
            profile.username,
            profile.private_key_path,
            profile.passphrase_secret_key,
        )
        with self._connect() as conn:
            if profile.id is None:
                try:
                    cur = conn.execute(
                        "INSERT INTO ssh_profiles(name, host, port, username, private_key_path, passphrase_secret_key)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        values,
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateProfileName(profile.name) from exc
                return int(cur.lastrowid)
            try:
                cur = conn.execute(
                    "UPDATE ssh_profiles SET name=?, host=?, port=?, username=?, private_key_path=?,"
                    " passphrase_secret_key=? WHERE id=?",
                    (*values, profile.id),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateProfileName(profile.name) from exc
            if cur.rowcount == 0:
                raise ProfileGone(f"SSH profile {profile.id} no longer exists")
            return int(profile.id)

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
        """Insert when the profile has no id, otherwise edit exactly that row.

        See save_ssh_profile: an id means "edit this one", no id means "add one".
        """
        values = (
            profile.name,
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
        )
        with self._connect() as conn:
            if profile.id is None:
                try:
                    cur = conn.execute(
                        f"INSERT INTO db_profiles({DB_PROFILE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateProfileName(profile.name) from exc
                return int(cur.lastrowid)
            assignments = ", ".join(f"{column.strip()}=?" for column in DB_PROFILE_COLUMNS.split(","))
            try:
                cur = conn.execute(f"UPDATE db_profiles SET {assignments} WHERE id=?", (*values, profile.id))
            except sqlite3.IntegrityError as exc:
                raise DuplicateProfileName(profile.name) from exc
            if cur.rowcount == 0:
                raise ProfileGone(f"Database profile {profile.id} no longer exists")
            return int(profile.id)

    def list_db_profiles(self) -> List[DBProfile]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT id, {DB_PROFILE_COLUMNS} FROM db_profiles ORDER BY name").fetchall()

        return [
            DBProfile(
                id=int(r["id"]),
                name=str(r["name"]),
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
                SELECT id, name, db_type, host, port, database_name, username,
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

