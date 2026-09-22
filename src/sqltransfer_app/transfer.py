from __future__ import annotations

import asyncio
import hashlib
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote_plus, urlencode

from .models import DBProfile, SSHProfile, TransferResult
from .secrets import SecretStore
from .storage import Storage
from .tls import TlsSettings, apitap_query_params, mysql_connect_kwargs, postgres_connect_kwargs, resolve_tls
from .tunnel import OpenTunnel, TunnelManager


@dataclass(slots=True)
class ResolvedEndpoint:
    dsn: str
    tunnel: OpenTunnel | None = None
    # Where the client really connects (the tunnel's local end when tunnelled)
    # and the encryption that applies there.
    host: str = ""
    port: int = 0
    tls: TlsSettings = TlsSettings("off")


def _default_port(db_type: str) -> int:
    return 5432 if db_type == "postgres" else 3306


def _mysql_connect(tls: TlsSettings, **kwargs: Any):
    """The only place that opens a pymysql connection, so none can skip the encryption setting."""
    import pymysql

    return pymysql.connect(**kwargs, **mysql_connect_kwargs(tls))


def _pg_connect(tls: TlsSettings, **kwargs: Any):
    """The only place that opens a psycopg connection, so none can skip the encryption setting."""
    import psycopg

    return psycopg.connect(**kwargs, **postgres_connect_kwargs(tls))


def _login_check_sync(
    db_type: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str | None,
    timeout_s: float,
    *,
    tls: TlsSettings,
) -> str:
    """Log in for real and report whether the session is encrypted.

    A bare port check used to pass for settings that fail at the first query,
    wrong password and wrong encryption setting alike.
    """
    if db_type == "postgres":
        conn = _pg_connect(
            tls, host=host, port=port, dbname=database, user=username,
            password=password or "", connect_timeout=int(timeout_s),
        )
        try:
            if conn.pgconn.ssl_in_use:
                cipher = conn.pgconn.ssl_attribute("cipher")
                return f"encrypted ({cipher.decode() if isinstance(cipher, bytes) else cipher})"
            return "not encrypted"
        finally:
            conn.close()

    conn = _mysql_connect(
        tls, host=host, port=port, user=username, password=password or "",
        database=database, connect_timeout=int(timeout_s), read_timeout=int(timeout_s),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            row = cur.fetchone()
        cipher = (row[1] if row else "") or ""
        if isinstance(cipher, bytes):
            cipher = cipher.decode()
        return f"encrypted ({cipher})" if cipher else "not encrypted"
    finally:
        conn.close()


def build_dsn(
    profile: DBProfile,
    password: str | None,
    host: str | None = None,
    port: int | None = None,
    tls: TlsSettings | None = None,
) -> str:
    scheme = "postgres" if profile.db_type == "postgres" else "mysql"
    user = quote_plus(profile.username)
    pwd = quote_plus(password) if password else ""
    auth = f"{user}:{pwd}" if pwd else user
    final_host = host or profile.host
    final_port = port or profile.port or _default_port(profile.db_type)
    db_name = quote_plus(profile.database)
    dsn = f"{scheme}://{auth}@{final_host}:{final_port}/{db_name}"
    if tls is not None:
        # Always explicit: apitap's own default depends on the host, and ours must match it.
        dsn += "?" + urlencode(apitap_query_params(profile.db_type, tls))
    return dsn


def _get_apitap_transfer_callable():
    try:
        import apitap
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Could not import apitap. Activate the correct venv and reinstall apitap.") from exc

    transfer_fn = getattr(apitap, "transfer", None)
    if callable(transfer_fn):
        return transfer_fn

    module_file = getattr(apitap, "__file__", None)
    module_paths = list(getattr(apitap, "__path__", [])) if hasattr(apitap, "__path__") else []
    raise RuntimeError(
        "apitap installation is incomplete: module has no 'transfer' attribute. "
        f"module_file={module_file}, module_paths={module_paths}. "
        "Reinstall apitap from source in this environment."
    )


def _report_failure_text(report: object) -> str | None:
    for key in ("failed", "failures", "errors", "error"):
        value = getattr(report, key, None)
        if value:
            return str(value)
    ok_value = getattr(report, "ok", None)
    if ok_value is False:
        return "apitap reported failure"
    return None


class TransferService:
    def __init__(self, storage: Storage, secret_store: SecretStore, tunnel_manager: TunnelManager) -> None:
        self.storage = storage
        self.secret_store = secret_store
        self.tunnel_manager = tunnel_manager

    def _resolve_profile(
        self,
        profile: DBProfile,
        *,
        password_override: str | None = None,
        ssh_profile_override: SSHProfile | None = None,
    ) -> ResolvedEndpoint:
        password = password_override if password_override is not None else self.secret_store.get_secret(profile.password_secret_key)
        if profile.use_ssh:
            if ssh_profile_override is not None:
                ssh = ssh_profile_override
            elif profile.ssh_profile_id:
                ssh = self.storage.get_ssh_profile(profile.ssh_profile_id)
            else:
                raise ValueError(f"Profile '{profile.name}' uses SSH but has no SSH profile selected")
            if not ssh:
                raise ValueError(f"SSH profile not found for '{profile.name}'")
            passphrase = self.secret_store.get_secret(ssh.passphrase_secret_key)
            tunnel = self.tunnel_manager.open_tunnel(
                ssh_profile=ssh,
                remote_host=profile.host,
                remote_port=profile.port,
                passphrase=passphrase,
            )
            tls = resolve_tls(profile.tls_mode, profile.tls_ca_path, tunnel.local_host)
            dsn = build_dsn(profile, password, host=tunnel.local_host, port=tunnel.local_port, tls=tls)
            return ResolvedEndpoint(
                dsn=dsn, tunnel=tunnel, host=tunnel.local_host, port=tunnel.local_port, tls=tls
            )

        tls = resolve_tls(profile.tls_mode, profile.tls_ca_path, profile.host)
        return ResolvedEndpoint(
            dsn=build_dsn(profile, password, tls=tls), host=profile.host, port=profile.port, tls=tls
        )

    def preview_scope(self, scope_mode: Literal["table", "tables", "schema"], scope_value: str) -> tuple[str, str, int]:
        cleaned = scope_value.strip()
        if scope_mode == "tables":
            tables = [part.strip() for part in cleaned.split(",") if part.strip()]
            if not tables:
                raise ValueError("'tables' mode requires a comma-separated table list")
            return "tables", ", ".join(tables), len(tables)
        if scope_mode == "schema":
            if not cleaned:
                raise ValueError("'schema' mode requires a schema/database value")
            return "schema", cleaned, 0
        if not cleaned:
            raise ValueError("'table' mode requires a table value")
        return "table", cleaned, 1

    async def _login_check(
        self, profile: DBProfile, endpoint: ResolvedEndpoint, password: str | None, timeout_s: float
    ) -> str:
        state = await asyncio.to_thread(
            _login_check_sync,
            profile.db_type,
            endpoint.host,
            endpoint.port,
            profile.database,
            profile.username,
            password,
            timeout_s,
            tls=endpoint.tls,
        )
        return f"Connected: {profile.name} ({endpoint.host}:{endpoint.port}), {state}"

    async def test_profile_connection(self, profile: DBProfile, timeout_s: float = 4.0) -> tuple[bool, str]:
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(profile)
            password = self.secret_store.get_secret(profile.password_secret_key)
            return True, await self._login_check(profile, endpoint, password, timeout_s)
        except Exception as exc:  # noqa: BLE001
            return False, f"Connection test failed: {exc}"
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def test_profile_connection_from_values(
        self,
        profile: DBProfile,
        *,
        password: str | None = None,
        ssh_profile: SSHProfile | None = None,
        timeout_s: float = 4.0,
    ) -> tuple[bool, str]:
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(profile, password_override=password, ssh_profile_override=ssh_profile)
            # An empty form field means "keep the stored password", so fall back to the keychain.
            effective_password = password if password else self.secret_store.get_secret(profile.password_secret_key)
            return True, await self._login_check(profile, endpoint, effective_password, timeout_s)
        except Exception as exc:  # noqa: BLE001
            return False, f"Connection test failed: {exc}"
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def transfer_scope(
        self,
        source: DBProfile,
        destination: DBProfile,
        scope_mode: Literal["table", "tables", "schema"],
        scope_value: str,
        parallel: int | None = None,
    ) -> TransferResult:
        src: ResolvedEndpoint | None = None
        dst: ResolvedEndpoint | None = None

        try:
            src = self._resolve_profile(source)
            dst = self._resolve_profile(destination)
            kwargs: dict[str, object]
            parsed_mode, parsed_value, _ = self.preview_scope(scope_mode, scope_value)
            if parsed_mode == "tables":
                tables = [part.strip() for part in parsed_value.split(",") if part.strip()]
                kwargs = {"tables": tables}
            elif parsed_mode == "schema":
                kwargs = {"schema": parsed_value}
            else:
                kwargs = {"table": parsed_value}
            if parallel is not None:
                kwargs["parallel"] = parallel

            transfer_fn = _get_apitap_transfer_callable()
            report = await asyncio.to_thread(
                transfer_fn,
                src.dsn,
                dst.dsn,
                **kwargs,
            )
            failure_text = _report_failure_text(report)
            if failure_text:
                return TransferResult(
                    status="failed",
                    rows=int(getattr(report, "rows", 0)),
                    elapsed_ms=int(getattr(report, "elapsed_ms", 0)),
                    parallel=int(getattr(report, "parallel", 0)),
                    message=failure_text,
                )
            return TransferResult(
                status="success",
                rows=int(getattr(report, "rows", 0)),
                elapsed_ms=int(getattr(report, "elapsed_ms", 0)),
                parallel=int(getattr(report, "parallel", 0)),
                message=f"Transfer completed ({scope_mode})",
            )
        except Exception as exc:  # noqa: BLE001
            return TransferResult(
                status="failed",
                rows=0,
                elapsed_ms=0,
                parallel=0,
                message=str(exc),
            )
        finally:
            if src and src.tunnel:
                self.tunnel_manager.close_tunnel(src.tunnel)
            if dst and dst.tunnel:
                self.tunnel_manager.close_tunnel(dst.tunnel)

    async def transfer_single_table(
        self,
        source: DBProfile,
        destination: DBProfile,
        table: str,
        *,
        dest_table: str | None = None,
        parallel: int | None = None,
    ) -> TransferResult:
        src: ResolvedEndpoint | None = None
        dst: ResolvedEndpoint | None = None
        try:
            src = self._resolve_profile(source)
            dst = self._resolve_profile(destination)
            kwargs: dict[str, object] = {"table": table}
            if dest_table:
                kwargs["dest_table"] = dest_table
            if parallel is not None:
                kwargs["parallel"] = parallel

            transfer_fn = _get_apitap_transfer_callable()
            report = await asyncio.to_thread(transfer_fn, src.dsn, dst.dsn, **kwargs)
            failure_text = _report_failure_text(report)
            if failure_text:
                return TransferResult(
                    status="failed",
                    rows=int(getattr(report, "rows", 0)),
                    elapsed_ms=int(getattr(report, "elapsed_ms", 0)),
                    parallel=int(getattr(report, "parallel", 0)),
                    message=failure_text,
                )
            label = f" ({dest_table})" if dest_table else ""
            return TransferResult(
                status="success",
                rows=int(getattr(report, "rows", 0)),
                elapsed_ms=int(getattr(report, "elapsed_ms", 0)),
                parallel=int(getattr(report, "parallel", 0)),
                message=f"Transfer completed{label}",
            )
        except Exception as exc:  # noqa: BLE001
            return TransferResult(
                status="failed",
                rows=0,
                elapsed_ms=0,
                parallel=0,
                message=str(exc),
            )
        finally:
            if src and src.tunnel:
                self.tunnel_manager.close_tunnel(src.tunnel)
            if dst and dst.tunnel:
                self.tunnel_manager.close_tunnel(dst.tunnel)

    def build_mysql_temp_name(self, final_table_name: str) -> str:
        digest = hashlib.sha1(final_table_name.encode("utf-8")).hexdigest()[:10]
        ts = int(time.time()) % 100000
        return f"apx_{digest}_{ts}"

    async def mysql_swap_temp_to_final(
        self,
        destination: DBProfile,
        *,
        temp_table: str,
        final_table: str,
    ) -> tuple[bool, str]:
        endpoint: ResolvedEndpoint | None = None
        try:
            if destination.db_type != "mysql":
                return False, "Destination is not MySQL"
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""

            result = await asyncio.to_thread(
                _mysql_swap_table_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                temp_table,
                final_table,
                tls=endpoint.tls,
            )
            return True, result
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_table_exists(self, destination: DBProfile, table_name: str) -> tuple[bool, str]:
        endpoint: ResolvedEndpoint | None = None
        try:
            if destination.db_type != "mysql":
                return False, "Destination is not MySQL"
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""
            exists = await asyncio.to_thread(
                _mysql_table_exists_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                table_name,
                tls=endpoint.tls,
            )
            return exists, ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_table_has_inbound_fk(self, destination: DBProfile, table_name: str) -> tuple[bool, bool, str]:
        endpoint: ResolvedEndpoint | None = None
        try:
            if destination.db_type != "mysql":
                return False, False, "Destination is not MySQL"
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""
            has_fk = await asyncio.to_thread(
                _mysql_table_has_inbound_fk_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                table_name,
                tls=endpoint.tls,
            )
            return True, has_fk, ""
        except Exception as exc:  # noqa: BLE001
            return False, False, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_replace_final_from_temp(
        self,
        destination: DBProfile,
        *,
        temp_table: str,
        final_table: str,
    ) -> tuple[bool, str]:
        endpoint: ResolvedEndpoint | None = None
        try:
            if destination.db_type != "mysql":
                return False, "Destination is not MySQL"
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""
            result = await asyncio.to_thread(
                _mysql_replace_final_from_temp_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                temp_table,
                final_table,
                tls=endpoint.tls,
            )
            return True, result
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def source_table_row_count(self, source: DBProfile, table_name: str) -> tuple[bool, int, str]:
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(source)
            host = endpoint.tunnel.local_host if endpoint.tunnel else source.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else source.port
            password = self.secret_store.get_secret(source.password_secret_key) or ""
            count = await asyncio.to_thread(
                _source_table_row_count_sync,
                source.db_type,
                host,
                port,
                source.database,
                source.username,
                password,
                table_name,
                tls=endpoint.tls,
            )
            return True, count, ""
        except Exception as exc:  # noqa: BLE001
            return False, 0, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_source_index_clauses(
        self, source: DBProfile, tables: list[str]
    ) -> tuple[bool, dict[str, dict[str, str]], str]:
        """Read the secondary indexes of all tables in scope with one source round trip.

        Returns clauses keyed by bare table name. Reading them per table would open
        an SSH tunnel per table, which roughly doubles a schema-wide run.
        """
        if source.db_type != "mysql":
            return True, {}, "Index copy is only supported from a MySQL source"
        schemas = sorted({table.rpartition(".")[0].strip("`\"") or source.database for table in tables})
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(source)
            host = endpoint.tunnel.local_host if endpoint.tunnel else source.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else source.port
            password = self.secret_store.get_secret(source.password_secret_key) or ""
            rows = await asyncio.to_thread(
                _mysql_source_index_rows_sync,
                host,
                port,
                source.database,
                source.username,
                password,
                schemas or [source.database],
                tls=endpoint.tls,
            )
            return True, group_mysql_index_rows_by_table(rows), ""
        except Exception as exc:  # noqa: BLE001
            return False, {}, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_apply_index_clauses(
        self,
        destination: DBProfile,
        *,
        target_table: str,
        clauses: dict[str, str],
    ) -> tuple[bool, list[str], list[str], str]:
        """Add the missing indexes to a destination table: (ok, applied, failed, error)."""
        if not clauses:
            return True, [], [], ""
        if destination.db_type != "mysql":
            return False, [], [], "Destination is not MySQL"
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""
            applied, failed = await asyncio.to_thread(
                _mysql_apply_index_clauses_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                target_table,
                clauses,
                tls=endpoint.tls,
            )
            return True, applied, failed, ""
        except Exception as exc:  # noqa: BLE001
            return False, [], [], str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_source_fk_clauses(
        self, source: DBProfile, tables: list[str]
    ) -> tuple[bool, dict[str, dict[str, str]], list[str], str]:
        """Read the foreign keys of all tables in scope with one source round trip.

        Returns (ok, clauses keyed by bare table name, skipped cross-schema keys, error).
        """
        if source.db_type != "mysql":
            return True, {}, [], "Foreign key copy is only supported from a MySQL source"
        schemas = sorted({table.rpartition(".")[0].strip("`\"") or source.database for table in tables})
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(source)
            host = endpoint.tunnel.local_host if endpoint.tunnel else source.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else source.port
            password = self.secret_store.get_secret(source.password_secret_key) or ""
            rows_by_schema = await asyncio.to_thread(
                _mysql_source_fk_rows_sync,
                host,
                port,
                source.database,
                source.username,
                password,
                schemas or [source.database],
                tls=endpoint.tls,
            )
            clauses_by_table: dict[str, dict[str, str]] = {}
            skipped: list[str] = []
            for schema, rows in rows_by_schema.items():
                per_table: dict[str, list] = {}
                for table, *fk_row in rows:
                    per_table.setdefault(table, []).append(tuple(fk_row))
                for table, fk_rows in per_table.items():
                    clauses, table_skipped = build_mysql_fk_clauses(fk_rows, source_schema=schema)
                    if clauses:
                        clauses_by_table[table] = clauses
                    skipped.extend(f"{table}.{entry}" for entry in table_skipped)
            return True, clauses_by_table, skipped, ""
        except Exception as exc:  # noqa: BLE001
            return False, {}, [], str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_apply_fk_clauses(
        self,
        destination: DBProfile,
        clauses_by_table: dict[str, dict[str, str]],
    ) -> tuple[bool, dict[str, list[str]], list[str], str]:
        """Add the missing foreign keys: (ok, applied per table, failures, error)."""
        if not clauses_by_table:
            return True, {}, [], ""
        if destination.db_type != "mysql":
            return False, {}, [], "Destination is not MySQL"
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""
            applied, failed = await asyncio.to_thread(
                _mysql_apply_fk_clauses_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                clauses_by_table,
                tls=endpoint.tls,
            )
            return True, applied, failed, ""
        except Exception as exc:  # noqa: BLE001
            return False, {}, [], str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def mysql_empty_table(self, destination: DBProfile, table: str) -> tuple[bool, int, str]:
        """Clear a destination table whose source is empty: (ok, rows deleted, error).

        apitap's 0-row guard leaves an existing destination untouched when the
        source has no rows, which would keep rows in the copy the source no longer has.
        """
        if destination.db_type != "mysql":
            return False, 0, "Destination is not MySQL"
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""
            deleted = await asyncio.to_thread(
                _mysql_empty_table_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                table,
                tls=endpoint.tls,
            )
            return True, deleted, ""
        except Exception as exc:  # noqa: BLE001
            return False, 0, str(exc)
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def ensure_empty_mysql_table_from_source(
        self,
        source: DBProfile,
        destination: DBProfile,
        *,
        source_table: str,
        final_table: str,
    ) -> tuple[bool, str, list[str]]:
        src_ep: ResolvedEndpoint | None = None
        dst_ep: ResolvedEndpoint | None = None
        try:
            if source.db_type != "mysql" or destination.db_type != "mysql":
                return False, "Automatic empty-table creation is only supported for MySQL -> MySQL", []

            src_ep = self._resolve_profile(source)
            dst_ep = self._resolve_profile(destination)

            src_host = src_ep.tunnel.local_host if src_ep.tunnel else source.host
            src_port = src_ep.tunnel.local_port if src_ep.tunnel else source.port
            dst_host = dst_ep.tunnel.local_host if dst_ep.tunnel else destination.host
            dst_port = dst_ep.tunnel.local_port if dst_ep.tunnel else destination.port

            src_password = self.secret_store.get_secret(source.password_secret_key) or ""
            dst_password = self.secret_store.get_secret(destination.password_secret_key) or ""

            msg, deferred_fk_sql = await asyncio.to_thread(
                _ensure_empty_mysql_table_from_source_sync,
                src_host,
                src_port,
                source.database,
                source.username,
                src_password,
                source_table,
                dst_host,
                dst_port,
                destination.database,
                destination.username,
                dst_password,
                final_table,
                src_tls=src_ep.tls,
                dst_tls=dst_ep.tls,
            )
            return True, msg, deferred_fk_sql
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), []
        finally:
            if src_ep and src_ep.tunnel:
                self.tunnel_manager.close_tunnel(src_ep.tunnel)
            if dst_ep and dst_ep.tunnel:
                self.tunnel_manager.close_tunnel(dst_ep.tunnel)

    async def mysql_run_second_pass_sql(
        self,
        destination: DBProfile,
        statements: list[str],
    ) -> tuple[bool, list[str]]:
        if not statements:
            return True, []
        endpoint: ResolvedEndpoint | None = None
        try:
            if destination.db_type != "mysql":
                return False, ["Destination is not MySQL"]
            endpoint = self._resolve_profile(destination)
            host = endpoint.tunnel.local_host if endpoint.tunnel else destination.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else destination.port
            password = self.secret_store.get_secret(destination.password_secret_key) or ""
            failures = await asyncio.to_thread(
                _mysql_execute_statements_sync,
                host,
                port,
                destination.database,
                destination.username,
                password,
                statements,
                tls=endpoint.tls,
            )
            return len(failures) == 0, failures
        except Exception as exc:  # noqa: BLE001
            return False, [str(exc)]
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)

    async def list_tables(
        self,
        source: DBProfile,
        schema_hint: str | None = None,
        limit: int = 500,
    ) -> tuple[bool, list[str], str]:
        password = self.secret_store.get_secret(source.password_secret_key)
        endpoint: ResolvedEndpoint | None = None
        try:
            endpoint = self._resolve_profile(source, password_override=password)
            host = endpoint.tunnel.local_host if endpoint.tunnel else source.host
            port = endpoint.tunnel.local_port if endpoint.tunnel else source.port
            rows = await asyncio.to_thread(
                _list_tables_sync,
                source.db_type,
                host,
                port,
                source.database,
                source.username,
                password,
                schema_hint,
                limit,
                tls=endpoint.tls,
            )
            if not rows:
                return True, [], "No tables found"
            return True, rows, f"Loaded {len(rows)} table(s)"
        except Exception as exc:  # noqa: BLE001
            return False, [], f"Load tables failed: {exc}"
        finally:
            if endpoint and endpoint.tunnel:
                self.tunnel_manager.close_tunnel(endpoint.tunnel)


def _list_tables_sync(
    db_type: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str | None,
    schema_hint: str | None,
    limit: int,
    *,
    tls: TlsSettings,
) -> list[str]:
    if db_type == "postgres":
        import psycopg

        schema = (schema_hint or "public").strip() or "public"
        conn = _pg_connect(tls,
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password or "",
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT schemaname, tablename
                    FROM pg_catalog.pg_tables
                    WHERE schemaname = %s
                    ORDER BY tablename
                    LIMIT %s
                    """,
                    (schema, limit),
                )
                return [f"{row[0]}.{row[1]}" for row in cur.fetchall()]
        finally:
            conn.close()

    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password or "",
        database=database,
        connect_timeout=5,
        read_timeout=8,
        write_timeout=8,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
                LIMIT %s
                """,
                (database, limit),
            )
            return [str(row[0]) for row in cur.fetchall()]
    finally:
        conn.close()


def _quote_mysql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _quote_pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# apitap creates destination tables with columns and the primary key only. Every
# other index has to be carried over from the source, or it is gone after the swap.

_MYSQL_INDEX_STATS_SQL = (
    "SELECT table_name, index_name, non_unique, seq_in_index, column_name, sub_part, "
    "collation, index_type, {expression} "
    "FROM information_schema.statistics WHERE table_schema IN ({schemas})"
)


def build_mysql_index_clauses(rows) -> dict[str, str]:
    """Turn information_schema.statistics rows of one table into ALTER TABLE clauses.

    Each row is (index_name, non_unique, seq_in_index, column_name, sub_part,
    collation, index_type, expression). PRIMARY is skipped, apitap creates it.
    """
    grouped: dict[str, dict] = {}
    for name, non_unique, _seq, column, sub_part, collation, index_type, expression in sorted(
        rows, key=lambda r: (r[0], int(r[2]))
    ):
        if name == "PRIMARY":
            continue
        entry = grouped.setdefault(
            name,
            {"unique": not int(non_unique), "type": (index_type or "BTREE").upper(), "parts": []},
        )
        if column is None and expression:
            # Functional key parts need their own parentheses.
            part = f"({expression})"
        else:
            part = _quote_mysql_ident(column)
            if sub_part:
                part += f"({int(sub_part)})"
        if (collation or "").upper() == "D":
            part += " DESC"
        entry["parts"].append(part)

    clauses: dict[str, str] = {}
    for name, entry in grouped.items():
        if entry["type"] == "FULLTEXT":
            kind = "FULLTEXT INDEX"
        elif entry["type"] == "SPATIAL":
            kind = "SPATIAL INDEX"
        elif entry["unique"]:
            kind = "UNIQUE INDEX"
        else:
            kind = "INDEX"
        clauses[name] = f"ADD {kind} {_quote_mysql_ident(name)} ({', '.join(entry['parts'])})"
    return clauses


def group_mysql_index_rows_by_table(rows) -> dict[str, dict[str, str]]:
    """Split schema-wide statistics rows per table and build the clauses for each."""
    per_table: dict[str, list] = {}
    for table, *index_row in rows:
        per_table.setdefault(table, []).append(tuple(index_row))
    return {table: build_mysql_index_clauses(index_rows) for table, index_rows in per_table.items()}


def _mysql_source_index_rows_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    schemas: list[str],
    *,
    tls: TlsSettings,
) -> list[tuple]:
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=60,
        write_timeout=60,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            placeholders = ", ".join(["%s"] * len(schemas))
            try:
                cur.execute(
                    _MYSQL_INDEX_STATS_SQL.format(expression="expression", schemas=placeholders),
                    tuple(schemas),
                )
            except pymysql.MySQLError:
                # MariaDB and MySQL < 8.0.13 have no EXPRESSION column, and no functional indexes.
                cur.execute(
                    _MYSQL_INDEX_STATS_SQL.format(expression="NULL", schemas=placeholders),
                    tuple(schemas),
                )
            return list(cur.fetchall())
    finally:
        conn.close()


def _mysql_apply_index_clauses_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    table: str,
    clauses: dict[str, str],
    *,
    tls: TlsSettings,
) -> tuple[list[str], list[str]]:
    """Add every index from `clauses` the table does not have yet.

    One ALTER TABLE first, so a big table is rebuilt once. If that fails (InnoDB
    for example builds only one FULLTEXT index per statement), fall back to one
    index at a time and report the ones that still fail instead of aborting.
    """
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        # Index builds on million-row tables take minutes.
        read_timeout=3600,
        write_timeout=3600,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT index_name FROM information_schema.statistics "
                "WHERE table_schema = %s AND table_name = %s",
                (database, table),
            )
            existing = {row[0] for row in cur.fetchall()}
            missing = {name: clause for name, clause in clauses.items() if name not in existing}
            if not missing:
                return [], []
            try:
                cur.execute(f"ALTER TABLE {_quote_mysql_ident(table)} " + ", ".join(missing.values()))
                return sorted(missing), []
            except pymysql.MySQLError:
                pass
            applied: list[str] = []
            failed: list[str] = []
            for name in sorted(missing):
                try:
                    cur.execute(f"ALTER TABLE {_quote_mysql_ident(table)} {missing[name]}")
                    applied.append(name)
                except pymysql.MySQLError as exc:
                    failed.append(f"{name}: {exc}")
            return applied, failed
    finally:
        conn.close()


# Foreign keys go the same way as indexes: apitap never creates them and the swap
# drops the table that had them. Unlike indexes they are restored after the whole
# run: key names are unique per database, so the outgoing table still holds the
# name until it is dropped, and the referenced table may be copied later in the run.

_MYSQL_FK_SQL = (
    "SELECT k.table_name, k.constraint_name, k.column_name, k.ordinal_position, "
    "k.referenced_table_schema, k.referenced_table_name, k.referenced_column_name, "
    "rc.update_rule, rc.delete_rule "
    "FROM information_schema.key_column_usage k "
    "JOIN information_schema.referential_constraints rc "
    "ON rc.constraint_schema = k.constraint_schema "
    "AND rc.constraint_name = k.constraint_name "
    "AND rc.table_name = k.table_name "
    "WHERE k.table_schema = %s AND k.referenced_table_name IS NOT NULL"
)


def build_mysql_fk_clauses(rows, source_schema: str) -> tuple[dict[str, str], list[str]]:
    """Turn the foreign key rows of one table into ALTER TABLE clauses.

    Each row is (constraint_name, column_name, ordinal_position, referenced_schema,
    referenced_table, referenced_column, update_rule, delete_rule). Keys into another
    schema are returned as skipped: the destination has no copy of that schema, and
    pointing them at a same-named local table would be a guess.
    """
    grouped: dict[str, dict] = {}
    for name, column, _pos, ref_schema, ref_table, ref_column, update_rule, delete_rule in sorted(
        rows, key=lambda r: (r[0], int(r[2]))
    ):
        entry = grouped.setdefault(
            name,
            {
                "ref_schema": ref_schema,
                "ref_table": ref_table,
                "cols": [],
                "ref_cols": [],
                "update": update_rule,
                "delete": delete_rule,
            },
        )
        entry["cols"].append(_quote_mysql_ident(column))
        entry["ref_cols"].append(_quote_mysql_ident(ref_column))

    clauses: dict[str, str] = {}
    skipped: list[str] = []
    for name, entry in grouped.items():
        if entry["ref_schema"] != source_schema:
            skipped.append(f"{name} -> {entry['ref_schema']}.{entry['ref_table']}")
            continue
        clauses[name] = (
            f"ADD CONSTRAINT {_quote_mysql_ident(name)} FOREIGN KEY ({', '.join(entry['cols'])}) "
            f"REFERENCES {_quote_mysql_ident(entry['ref_table'])} ({', '.join(entry['ref_cols'])}) "
            f"ON DELETE {entry['delete']} ON UPDATE {entry['update']}"
        )
    return clauses, skipped


def _mysql_source_fk_rows_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    schemas: list[str],
    *,
    tls: TlsSettings,
) -> dict[str, list[tuple]]:
    """Foreign key rows keyed by schema, each row starting with its table name."""
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=60,
        write_timeout=60,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            result: dict[str, list[tuple]] = {}
            for schema in schemas:
                cur.execute(_MYSQL_FK_SQL, (schema,))
                result[schema] = list(cur.fetchall())
            return result
    finally:
        conn.close()


def _mysql_apply_fk_clauses_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    clauses_by_table: dict[str, dict[str, str]],
    *,
    tls: TlsSettings,
) -> tuple[dict[str, list[str]], list[str]]:
    """Add the missing foreign keys of every table over one connection.

    Returns (applied names per table, failures as "table.key: error").
    """
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=600,
        write_timeout=600,
        autocommit=True,
    )
    applied: dict[str, list[str]] = {}
    failed: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, constraint_name FROM information_schema.referential_constraints "
                "WHERE constraint_schema = %s",
                (database,),
            )
            existing = {(table, name) for table, name in cur.fetchall()}
            # Without the check MySQL does not validate the existing rows, the same
            # way a dump restore works. Tables copied minutes apart from a live source
            # would otherwise refuse the key over rows that changed in between.
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                for table in sorted(clauses_by_table):
                    for name, clause in sorted(clauses_by_table[table].items()):
                        if (table, name) in existing:
                            continue
                        try:
                            cur.execute(f"ALTER TABLE {_quote_mysql_ident(table)} {clause}")
                            applied.setdefault(table, []).append(name)
                        except pymysql.MySQLError as exc:
                            failed.append(f"{table}.{name}: {exc}")
            finally:
                cur.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        conn.close()
    return applied, failed


def _short_backup_name(final_table: str) -> str:
    suffix = f"__old_{int(time.time()) % 100000}"
    allowed = 64 - len(suffix)
    trimmed = final_table[:allowed]
    return f"{trimmed}{suffix}"


def _mysql_swap_table_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    temp_table: str,
    final_table: str,
    *,
    tls: TlsSettings,
) -> str:
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
                """,
                (database, final_table),
            )
            exists = int(cur.fetchone()[0]) > 0

            if exists:
                backup = _short_backup_name(final_table)
                stmt = (
                    f"RENAME TABLE {_quote_mysql_ident(final_table)} TO {_quote_mysql_ident(backup)}, "
                    f"{_quote_mysql_ident(temp_table)} TO {_quote_mysql_ident(final_table)}"
                )
                cur.execute(stmt)
                try:
                    cur.execute(f"DROP TABLE {_quote_mysql_ident(backup)}")
                    return f"Swapped {temp_table} -> {final_table} (backup dropped: {backup})"
                except Exception as drop_exc:  # noqa: BLE001
                    return (
                        f"Swapped {temp_table} -> {final_table} (backup kept: {backup}; "
                        f"drop failed: {drop_exc})"
                    )

            cur.execute(
                f"RENAME TABLE {_quote_mysql_ident(temp_table)} TO {_quote_mysql_ident(final_table)}"
            )
            return f"Renamed {temp_table} -> {final_table}"
    finally:
        conn.close()


def _mysql_table_exists_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    table_name: str,
    *,
    tls: TlsSettings,
) -> bool:
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
                """,
                (database, table_name),
            )
            return int(cur.fetchone()[0]) > 0
    finally:
        conn.close()


def _mysql_table_has_inbound_fk_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    table_name: str,
    *,
    tls: TlsSettings,
) -> bool:
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.referential_constraints
                WHERE constraint_schema = %s
                  AND referenced_table_name = %s
                """,
                (database, table_name),
            )
            return int(cur.fetchone()[0]) > 0
    finally:
        conn.close()


def _mysql_replace_final_from_temp_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    temp_table: str,
    final_table: str,
    *,
    tls: TlsSettings,
) -> str:
    import pymysql

    last_exc: Exception | None = None
    for attempt in range(1, 3):
        conn = _mysql_connect(tls,
            host=host,
            port=port,
            user=username,
            password=password,
            database=database,
            connect_timeout=5,
            # Large INSERT..SELECT over SSH can exceed short client timeouts.
            read_timeout=600,
            write_timeout=600,
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION net_read_timeout=600")
                cur.execute("SET SESSION net_write_timeout=600")
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
                cur.execute(f"DELETE FROM {_quote_mysql_ident(final_table)}")
                cur.execute(
                    f"INSERT INTO {_quote_mysql_ident(final_table)} SELECT * FROM {_quote_mysql_ident(temp_table)}"
                )
                cur.execute(f"DROP TABLE {_quote_mysql_ident(temp_table)}")
                cur.execute("SET FOREIGN_KEY_CHECKS=1")
                return f"Replaced data in {final_table} from {temp_table} (in-place, FK-safe)"
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc).lower()
            transient = "lost connection" in msg or "timed out" in msg
            if not transient or attempt == 2:
                raise
        finally:
            conn.close()

    if last_exc:
        raise last_exc
    raise RuntimeError("In-place replace failed unexpectedly")


def _mysql_empty_table_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    table: str,
    *,
    tls: TlsSettings,
) -> int:
    """Delete every row, returning how many went.

    DELETE rather than TRUNCATE: MySQL refuses TRUNCATE on a table other tables
    reference, and the in-place path already handles those tables the same way.
    """
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=600,
        write_timeout=600,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                deleted = cur.execute(f"DELETE FROM {_quote_mysql_ident(table)}")
            finally:
                cur.execute("SET FOREIGN_KEY_CHECKS=1")
            return int(deleted)
    finally:
        conn.close()


def _source_table_row_count_sync(
    db_type: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    table_name: str,
    *,
    tls: TlsSettings,
) -> int:
    if db_type == "postgres":
        import psycopg

        if "." in table_name:
            schema, table = table_name.split(".", 1)
            schema = schema.strip('`"')
            table = table.strip('`"')
        else:
            schema, table = "public", table_name.strip('`"')
        conn = _pg_connect(tls,
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password,
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {_quote_pg_ident(schema)}.{_quote_pg_ident(table)}"
                )
                return int(cur.fetchone()[0])
        finally:
            conn.close()

    import pymysql

    tbl = table_name.split(".")[-1].strip('`"')
    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_quote_mysql_ident(tbl)}")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


# Error codes meaning "a foreign key stopped CREATE TABLE". Only 1005 used to be
# handled, which is what MySQL 5.x and MariaDB return; MySQL 8 reports a missing
# referenced table as 1824, so on a fresh MySQL 8 destination the run aborted.
_MYSQL_FK_BLOCKS_CREATE = {
    1005,  # ER_CANT_CREATE_TABLE (errno 150), MySQL 5.x / MariaDB
    1215,  # ER_CANNOT_ADD_FOREIGN, MySQL 5.6 / 5.7
    1822,  # ER_FK_NO_INDEX_PARENT
    1824,  # ER_FK_CANNOT_OPEN_PARENT, MySQL 8: referenced table missing
    3780,  # ER_FK_INCOMPATIBLE_COLUMNS
}


def _ensure_empty_mysql_table_from_source_sync(
    src_host: str,
    src_port: int,
    src_database: str,
    src_username: str,
    src_password: str,
    source_table: str,
    dst_host: str,
    dst_port: int,
    dst_database: str,
    dst_username: str,
    dst_password: str,
    final_table: str,
    *,
    src_tls: TlsSettings,
    dst_tls: TlsSettings,
) -> tuple[str, list[str]]:
    import pymysql

    src_tbl = source_table.split(".")[-1].strip('`"')
    src_conn = _mysql_connect(src_tls,
        host=src_host,
        port=src_port,
        user=src_username,
        password=src_password,
        database=src_database,
        charset="utf8mb4",
        use_unicode=False,
        connect_timeout=5,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )
    try:
        with src_conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE {_quote_mysql_ident(src_tbl)}")
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"SHOW CREATE TABLE returned no data for {src_tbl}")
            create_sql = _coerce_sql_text(row[1])
    finally:
        src_conn.close()

    dst_conn = _mysql_connect(dst_tls,
        host=dst_host,
        port=dst_port,
        user=dst_username,
        password=dst_password,
        database=dst_database,
        connect_timeout=5,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )
    try:
        with dst_conn.cursor() as cur:
            target = _quote_mysql_ident(final_table)
            source_quoted = _quote_mysql_ident(src_tbl)
            if source_quoted not in create_sql:
                raise RuntimeError(f"Could not rewrite CREATE TABLE statement for source table {src_tbl}")
            create_sql = create_sql.replace(source_quoted, target, 1)
            create_sql, removed_defaults = _strip_problematic_binary_defaults(create_sql)

            invalid_default_strips = 0
            while True:
                try:
                    cur.execute(create_sql)
                    note_parts: list[str] = []
                    if removed_defaults:
                        note_parts.append(f"removed {removed_defaults} binary DEFAULT literal(s)")
                    if invalid_default_strips:
                        note_parts.append(f"stripped {invalid_default_strips} invalid DEFAULT(s)")
                    note = f" ({'; '.join(note_parts)})" if note_parts else ""
                    return f"Created empty destination table {final_table} from source schema{note}", []
                except Exception as exc:  # noqa: BLE001
                    code = exc.args[0] if getattr(exc, "args", None) else None
                    if code == 1067:
                        bad_col = _extract_invalid_default_column(str(exc))
                        if bad_col:
                            new_sql, changed = _strip_default_for_column(create_sql, bad_col)
                            if changed:
                                create_sql = new_sql
                                invalid_default_strips += 1
                                continue
                        # Fallback: remove DEFAULT clauses from all column definitions.
                        new_sql, changed = _strip_all_column_defaults(create_sql)
                        if changed:
                            create_sql = new_sql
                            invalid_default_strips += 1
                            continue
                    if code == 1064:
                        meta_msg = _create_empty_table_from_source_columns_sync(
                            src_host,
                            src_port,
                            src_database,
                            src_username,
                            src_password,
                            source_table,
                            dst_host,
                            dst_port,
                            dst_database,
                            dst_username,
                            dst_password,
                            final_table,
                            src_tls=src_tls,
                            dst_tls=dst_tls,
                        )
                        return f"Created empty destination table {final_table} using metadata fallback ({meta_msg})", []
                    if code not in _MYSQL_FK_BLOCKS_CREATE:
                        raise
                    stripped_sql, deferred_fk_sql = _strip_mysql_fk_constraints(create_sql, final_table)
                    try:
                        cur.execute(stripped_sql)
                    except Exception as fk_exc:  # noqa: BLE001
                        code2 = fk_exc.args[0] if getattr(fk_exc, "args", None) else None
                        if code2 == 1064:
                            meta_msg = _create_empty_table_from_source_columns_sync(
                                src_host,
                                src_port,
                                src_database,
                                src_username,
                                src_password,
                                source_table,
                                dst_host,
                                dst_port,
                                dst_database,
                                dst_username,
                                dst_password,
                                final_table,
                                src_tls=src_tls,
                                dst_tls=dst_tls,
                            )
                            return (
                                f"Created empty destination table {final_table} using metadata fallback after FK strip "
                                f"({meta_msg})",
                                deferred_fk_sql,
                            )
                        raise
                    note_parts = []
                    if removed_defaults:
                        note_parts.append(f"removed {removed_defaults} binary DEFAULT literal(s)")
                    if invalid_default_strips:
                        note_parts.append(f"stripped {invalid_default_strips} invalid DEFAULT(s)")
                    note = ("; " + "; ".join(note_parts)) if note_parts else ""
                    return (
                        f"Created empty destination table {final_table} without FK constraints (deferred second pass{note})",
                        deferred_fk_sql,
                    )
    finally:
        dst_conn.close()


def _coerce_sql_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw.decode("latin-1")
            except Exception:  # noqa: BLE001
                return raw.decode("utf-8", errors="replace")
    return str(value)


def _strip_mysql_fk_constraints(create_sql: str, final_table: str) -> tuple[str, list[str]]:
    lines = create_sql.splitlines()
    if len(lines) < 3:
        return create_sql, []

    header = lines[0]
    footer = lines[-1]
    body = lines[1:-1]

    keep: list[str] = []
    deferred: list[str] = []
    for raw in body:
        line = raw.strip()
        upper = line.upper()
        if "FOREIGN KEY" in upper and "CONSTRAINT" in upper:
            clause = line.rstrip(",")
            deferred.append(f"ALTER TABLE {_quote_mysql_ident(final_table)} ADD {clause};")
            continue
        if upper.startswith("FOREIGN KEY"):
            clause = line.rstrip(",")
            deferred.append(f"ALTER TABLE {_quote_mysql_ident(final_table)} ADD {clause};")
            continue
        keep.append(raw)

    # Remove trailing comma before ENGINE line after FK lines are removed.
    while keep and keep[-1].rstrip().endswith(","):
        keep[-1] = keep[-1].rstrip().rstrip(",")

    rebuilt = "\n".join([header, *keep, footer])
    return rebuilt, deferred


def _strip_problematic_binary_defaults(create_sql: str) -> tuple[str, int]:
    # Some source schemas store binary default literals in a format MySQL rejects on create.
    # For fallback empty-table creation we can safely omit those defaults.
    type_pattern = re.compile(r"\b(?:binary|varbinary)\s*\(\s*\d+\s*\)", flags=re.IGNORECASE)

    count = 0
    out_lines: list[str] = []
    for line in create_sql.splitlines():
        if type_pattern.search(line):
            new_line, removed = _remove_default_clause_from_column_line(line)
            line = new_line
            count += removed
        out_lines.append(line)
    return "\n".join(out_lines), count


def _extract_invalid_default_column(error_text: str) -> str | None:
    match = re.search(r"Invalid default value for '([^']+)'", error_text)
    if match:
        return match.group(1)
    return None


def _strip_default_for_column(create_sql: str, column_name: str) -> tuple[str, bool]:
    col_regex = re.compile(rf"^\s*`?{re.escape(column_name)}`?\b", flags=re.IGNORECASE)
    changed = False
    out_lines: list[str] = []
    for raw in create_sql.splitlines():
        line = raw
        if col_regex.search(line):
            line2, removed = _remove_default_clause_from_column_line(line)
            if removed > 0:
                line = line2
                changed = True
        out_lines.append(line)
    return "\n".join(out_lines), changed


def _strip_all_column_defaults(create_sql: str) -> tuple[str, bool]:
    changed = False
    out_lines: list[str] = []
    for raw in create_sql.splitlines():
        line = raw
        # Apply only to likely column definition lines to avoid touching table options.
        if line.lstrip().startswith("`"):
            line2, removed = _remove_default_clause_from_column_line(line)
            if removed > 0:
                line = line2
                changed = True
        out_lines.append(line)
    return "\n".join(out_lines), changed


def _remove_default_clause_from_column_line(line: str) -> tuple[str, int]:
    lower = line.lower()
    match = re.search(r"\bdefault\b", lower)
    if not match:
        return line, 0

    # Remove from the DEFAULT token up to the next structural marker.
    start = match.start()
    if start > 0 and line[start - 1].isspace():
        start -= 1

    tail = lower[match.end():]
    markers = [
        " not null",
        " null",
        " comment ",
        " collate ",
        " primary key",
        " unique ",
        " key ",
        " references ",
        " check ",
        " on update",
    ]
    positions = [tail.find(marker) for marker in markers if tail.find(marker) >= 0]
    if positions:
        end = match.end() + min(positions)
    else:
        end = len(line)

    return line[:start] + line[end:], 1


def _mysql_execute_statements_sync(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    statements: list[str],
    *,
    tls: TlsSettings,
) -> list[str]:
    import pymysql

    conn = _mysql_connect(tls,
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )
    failures: list[str] = []
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                try:
                    cur.execute(stmt)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{stmt} :: {exc}")
    finally:
        conn.close()
    return failures


def _create_empty_table_from_source_columns_sync(
    src_host: str,
    src_port: int,
    src_database: str,
    src_username: str,
    src_password: str,
    source_table: str,
    dst_host: str,
    dst_port: int,
    dst_database: str,
    dst_username: str,
    dst_password: str,
    final_table: str,
    *,
    src_tls: TlsSettings,
    dst_tls: TlsSettings,
) -> str:
    import pymysql

    src_tbl = source_table.split(".")[-1].strip('`"')
    src_conn = _mysql_connect(src_tls,
        host=src_host,
        port=src_port,
        user=src_username,
        password=src_password,
        database=src_database,
        connect_timeout=5,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )
    try:
        with src_conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM {_quote_mysql_ident(src_tbl)}")
            rows = cur.fetchall()
            if not rows:
                raise RuntimeError(f"No columns returned for source table {src_tbl}")
    finally:
        src_conn.close()

    col_defs: list[str] = []
    pk_cols: list[str] = []
    for row in rows:
        field = str(row[0])
        col_type = str(row[1])
        nullable = str(row[2]).upper() == "YES"
        key = str(row[3] or "")
        extra = str(row[5] or "")

        definition = f"{_quote_mysql_ident(field)} {col_type}"
        definition += " NULL" if nullable else " NOT NULL"
        if extra:
            definition += f" {extra}"
        col_defs.append(definition)
        if key == "PRI":
            pk_cols.append(_quote_mysql_ident(field))

    if pk_cols:
        col_defs.append(f"PRIMARY KEY ({', '.join(pk_cols)})")

    create_sql = (
        f"CREATE TABLE {_quote_mysql_ident(final_table)} (\n  "
        + ",\n  ".join(col_defs)
        + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )

    dst_conn = _mysql_connect(dst_tls,
        host=dst_host,
        port=dst_port,
        user=dst_username,
        password=dst_password,
        database=dst_database,
        connect_timeout=5,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )
    try:
        with dst_conn.cursor() as cur:
            cur.execute(create_sql)
    finally:
        dst_conn.close()

    return "column-only schema copy"

















