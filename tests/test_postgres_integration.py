"""Encryption against a real PostgreSQL server, including a private CA.

Needs a disposable PostgreSQL with TLS on and a certificate for 127.0.0.1 signed by
the CA passed in, and is skipped otherwise:

    SQLTRANSFER_TEST_POSTGRES=127.0.0.1:5499:postgres:test:/path/to/ca.pem pytest tests/test_postgres_integration.py

The test drops and recreates the databases `sqlt_pg_src` and `sqlt_pg_dst`.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from sqltransfer_app.models import DBProfile
from sqltransfer_app.transfer import TransferService
from sqltransfer_app.tunnel import TunnelManager

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("apitap")

_SPEC = os.environ.get("SQLTRANSFER_TEST_POSTGRES")
pytestmark = pytest.mark.skipif(not _SPEC, reason="SQLTRANSFER_TEST_POSTGRES not set")

SRC_DB = "sqlt_pg_src"
DST_DB = "sqlt_pg_dst"


def _spec() -> dict:
    host, port, user, password, ca = _SPEC.split(":", 4)
    return {"host": host, "port": int(port), "user": user, "password": password, "ca": ca}


class _Secrets:
    def get_secret(self, _key):
        return _spec()["password"]


def _service() -> TransferService:
    return TransferService(storage=None, secret_store=_Secrets(), tunnel_manager=TunnelManager())


def _profile(role: str, database: str, mode: str, ca: str | None = None) -> DBProfile:
    s = _spec()
    return DBProfile(
        id=None, name=f"pg-{role}", role=role, db_type="postgres", host=s["host"], port=s["port"],
        database=database, username=s["user"], password_secret_key="unused", tls_mode=mode, tls_ca_path=ca,
    )


def _admin(database: str, *statements: str) -> None:
    s = _spec()
    with psycopg.connect(host=s["host"], port=s["port"], user=s["user"], password=s["password"],
                         dbname=database, autocommit=True, sslmode="disable") as conn:
        for stmt in statements:
            conn.execute(stmt)


@pytest.fixture()
def databases():
    _admin("postgres", f"DROP DATABASE IF EXISTS {SRC_DB}", f"DROP DATABASE IF EXISTS {DST_DB}",
           f"CREATE DATABASE {SRC_DB}", f"CREATE DATABASE {DST_DB}")
    _admin(SRC_DB, "CREATE TABLE items (id INT PRIMARY KEY, name TEXT NOT NULL)",
           "INSERT INTO items VALUES (1,'a'),(2,'b'),(3,'c')")
    yield
    _admin("postgres", f"DROP DATABASE IF EXISTS {SRC_DB} WITH (FORCE)", f"DROP DATABASE IF EXISTS {DST_DB} WITH (FORCE)")


def _login(profile: DBProfile) -> tuple[bool, str]:
    return asyncio.run(_service().test_profile_connection(profile))


def test_off_is_really_unencrypted(databases):
    # The server offers TLS and libpq's own default ("prefer") would take it.
    ok, message = _login(_profile("local", DST_DB, "off"))
    assert ok, message
    assert message.endswith("not encrypted")


def test_required_encrypts(databases):
    ok, message = _login(_profile("local", DST_DB, "required"))
    assert ok, message
    assert "encrypted (" in message and "not encrypted" not in message


def test_verified_without_the_private_ca_is_refused(databases):
    # The system trust store does not know the test CA.
    ok, message = _login(_profile("local", DST_DB, "verified"))
    assert not ok
    assert "certificate" in message.lower()


def test_verified_with_the_private_ca_connects(databases):
    ok, message = _login(_profile("local", DST_DB, "verified", _spec()["ca"]))
    assert ok, message
    assert "encrypted (" in message


def test_metadata_queries_use_the_setting(databases):
    service = _service()
    src = _profile("remote", SRC_DB, "verified", _spec()["ca"])
    ok, tables, message = asyncio.run(service.list_tables(src, schema_hint="public"))
    assert ok, message
    assert tables == ["public.items"]
    ok, count, message = asyncio.run(service.source_table_row_count(src, "public.items"))
    assert ok and count == 3, message

    ok, _tables, message = asyncio.run(service.list_tables(_profile("remote", SRC_DB, "verified"), schema_hint="public"))
    assert not ok and "certificate" in message.lower()


def test_apitap_transfer_follows_the_setting(databases):
    service = _service()
    ca = _spec()["ca"]

    src = _profile("remote", SRC_DB, "verified", ca)
    dst = _profile("local", DST_DB, "verified", ca)
    result = asyncio.run(service.transfer_single_table(src, dst, "public.items", dest_table="items_verified"))
    assert result.status == "success", result.message

    src = _profile("remote", SRC_DB, "required")
    dst = _profile("local", DST_DB, "required")
    result = asyncio.run(service.transfer_single_table(src, dst, "public.items", dest_table="items_required"))
    assert result.status == "success", result.message

    src = _profile("remote", SRC_DB, "verified")  # no CA: apitap must refuse, not fall back
    result = asyncio.run(service.transfer_single_table(src, dst, "public.items", dest_table="items_refused"))
    assert result.status == "failed"
    assert "certificate" in result.message.lower(), result.message  # refused for the right reason
