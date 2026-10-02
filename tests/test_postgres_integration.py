"""Encryption against a real PostgreSQL server, including a private CA.

Needs a disposable PostgreSQL with TLS on and a certificate for 127.0.0.1 signed by
the CA passed in, and is skipped otherwise:

    SQLTRANSFER_TEST_POSTGRES=127.0.0.1:5499:postgres:test:/path/to/ca.pem pytest tests/test_postgres_integration.py

The test drops and recreates the databases `sqlt_pg_src` and `sqlt_pg_dst`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from sqltransfer_app.anonymize import Rule, plan_table
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
    def anonymization_salt(self):
        # Fixed, so the expected replacements stay the same between runs.
        return "test-salt"

    def get_secret(self, _key):
        return _spec()["password"]


def _service() -> TransferService:
    return TransferService(storage=None, secret_store=_Secrets(), tunnel_manager=TunnelManager())


def _profile(role: str, database: str, mode: str, ca: str | None = None) -> DBProfile:
    s = _spec()
    return DBProfile(
        id=None, name=f"pg-{role}", db_type="postgres", host=s["host"], port=s["port"],
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


def _rows(database: str, sql: str) -> list[tuple]:
    s = _spec()
    with psycopg.connect(host=s["host"], port=s["port"], user=s["user"], password=s["password"],
                         dbname=database, autocommit=True, sslmode="disable") as conn:
        return list(conn.execute(sql).fetchall())


def _anonymize(dst: DBProfile, table: str, rules: list[Rule]) -> tuple[bool, int, str]:
    """The three steps the app takes: read the columns, look into the JSON ones, rewrite."""
    from dataclasses import replace as _replace

    from sqltransfer_app.anonymize import plan_json_column

    service = _service()
    ok, columns, message = asyncio.run(service.table_columns(dst, table))
    assert ok, message
    plan = plan_table(table, columns, rules)
    json_targets = []
    for column, column_type in plan.json_columns:
        ok, nodes, message = asyncio.run(service.json_paths(dst, table, column))
        if not ok:
            continue
        found, _skipped, _descend = plan_json_column(column, nodes, rules)
        json_targets += [_replace(t, column_type=column_type) for t in found]
    return asyncio.run(service.anonymize_table(dst, _replace(plan, json_targets=tuple(json_targets))))


def test_anonymizing_replaces_only_the_matched_columns(databases):
    dst = _profile("local", DST_DB, "off")
    _admin(DST_DB, "DROP TABLE IF EXISTS kunde",
           "CREATE TABLE kunde (id INT PRIMARY KEY, email TEXT UNIQUE, ort TEXT, plz TEXT, telefon TEXT, umsatz INT)",
           "INSERT INTO kunde VALUES (1,'a@axro.de','Hamburg','20095','040 123',10),"
           " (2,'b@axro.de',NULL,'10115',NULL,20), (3,'','Köln','','',30)")

    ok, affected, message = _anonymize(dst, "public.kunde",
                                       [Rule(id=1, pattern="*mail*", kind="email"),
                                        Rule(id=2, pattern="ort", kind="city"),
                                        Rule(id=3, pattern="plz", kind="postcode"),
                                        Rule(id=4, pattern="telefon", kind="phone")])
    assert ok, message
    assert affected == 3

    rows = _rows(DST_DB, "SELECT id, email, ort, plz, telefon, umsatz FROM kunde ORDER BY id")
    assert [r[5] for r in rows] == [10, 20, 30]
    assert "axro.de" not in str(rows)
    assert rows[1][2] is None
    assert rows[2][1] == ""
    assert all(re.fullmatch(r"\d{5}", r[3]) for r in rows if r[3])
    assert all(re.fullmatch(r"\+49 30 \d{7}", r[4]) for r in rows if r[4])


def test_the_same_value_yields_the_same_replacement(databases):
    dst = _profile("local", DST_DB, "off")
    _admin(DST_DB, "DROP TABLE IF EXISTS a", "DROP TABLE IF EXISTS b",
           "CREATE TABLE a (id INT PRIMARY KEY, email TEXT)",
           "CREATE TABLE b (id INT PRIMARY KEY, email TEXT)",
           "INSERT INTO a VALUES (1,'same@axro.de')", "INSERT INTO b VALUES (1,'same@axro.de')")
    rules = [Rule(id=1, pattern="email", kind="email")]
    for table in ("public.a", "public.b"):
        assert _anonymize(dst, table, rules)[0]
    assert _rows(DST_DB, "SELECT email FROM a")[0][0] == _rows(DST_DB, "SELECT email FROM b")[0][0]


def test_a_unique_column_stays_unique(databases):
    dst = _profile("local", DST_DB, "off")
    values = ",".join(f"({i},'kunde{i}@axro.de')" for i in range(1, 201))
    _admin(DST_DB, "DROP TABLE IF EXISTS viele",
           "CREATE TABLE viele (id INT PRIMARY KEY, email TEXT UNIQUE)",
           f"INSERT INTO viele VALUES {values}")
    ok, _affected, message = _anonymize(dst, "public.viele", [Rule(id=1, pattern="email", kind="email")])
    assert ok, message
    assert _rows(DST_DB, "SELECT COUNT(DISTINCT email) FROM viele")[0][0] == 200


def test_json_paths_are_discovered_level_by_level(databases):
    service = _service()
    dst = _profile("local", DST_DB, "off")
    _admin(DST_DB, "DROP TABLE IF EXISTS mitjson",
           "CREATE TABLE mitjson (id INT PRIMARY KEY, custom_fields JSONB)",
           """INSERT INTO mitjson VALUES
              (1, '{"email":"a@axro.de","adresse":{"ort":"Hamburg","nummer":7},"positionen":[{"email":"x@y.de"}]}'),
              (2, '{"email":"b@axro.de","notiz":"ohne"}'),
              (3, NULL)""")

    ok, nodes, message = asyncio.run(service.json_paths(dst, "public.mitjson", "custom_fields"))
    assert ok, message
    found = {node.path: node.types for node in nodes}
    assert found[("email",)] == frozenset({"STRING"})
    assert found[("adresse",)] == frozenset({"OBJECT"})
    assert found[("adresse", "ort")] == frozenset({"STRING"})
    assert found[("adresse", "nummer")] == frozenset({"NUMBER"})
    assert found[("positionen",)] == frozenset({"ARRAY"})
    assert ("positionen", "email") not in found


def test_a_text_column_that_is_not_json_is_refused_cleanly(databases):
    service = _service()
    dst = _profile("local", DST_DB, "off")
    _admin(DST_DB, "DROP TABLE IF EXISTS keinjson",
           "CREATE TABLE keinjson (id INT PRIMARY KEY, notiz TEXT)",
           "INSERT INTO keinjson VALUES (1, 'das ist kein json')")
    ok, nodes, message = asyncio.run(service.json_paths(dst, "public.keinjson", "notiz"))
    assert not ok and not nodes
    assert "json" in message.lower()


def test_values_inside_json_are_replaced_and_the_structure_survives(databases):
    dst = _profile("local", DST_DB, "off")
    _admin(DST_DB, "DROP TABLE IF EXISTS jsonkunde",
           "CREATE TABLE jsonkunde (id INT PRIMARY KEY, email TEXT, custom_fields JSONB, daten TEXT)",
           """INSERT INTO jsonkunde VALUES
              (1,'max@axro.de','{"email":"max@axro.de","adresse":{"ort":"Hamburg"},"umsatz":42,
                                 "positionen":[{"email":"x@y.de"}]}','{"email":"max@axro.de"}'),
              (2,'erika@axro.de','{"notiz":"ohne mail"}','{}'),
              (3,'leer@axro.de', NULL, NULL)""")

    rules = [Rule(id=1, pattern="*mail*", kind="email"), Rule(id=2, pattern="ort", kind="city"),
             Rule(id=3, pattern="daten", kind="json")]
    ok, _affected, message = _anonymize(dst, "public.jsonkunde", rules)
    assert ok, message

    rows = _rows(DST_DB, "SELECT id, email, custom_fields, daten FROM jsonkunde ORDER BY id")
    first = rows[0][2] if isinstance(rows[0][2], dict) else json.loads(rows[0][2])
    assert first["email"].endswith("@example.invalid")
    assert first["email"] == rows[0][1]
    assert first["umsatz"] == 42
    assert first["adresse"]["ort"] != "Hamburg"
    assert first["positionen"] == [{"email": "x@y.de"}]
    assert json.loads(rows[0][3])["email"] == first["email"]   # the text column keeps being text
    assert rows[2][2] is None and rows[2][3] is None
    assert "max@axro.de" not in str(rows) and "Hamburg" not in str(rows)


def test_a_generated_column_is_recognised(databases):
    # The column query used to select MySQL's `extra`, which PostgreSQL does not
    # have: every destination of this type failed with 'column "extra" does not exist'.
    service = _service()
    dst = _profile("local", DST_DB, "off")
    _admin(DST_DB, "DROP TABLE IF EXISTS generiert",
           "CREATE TABLE generiert (id INT PRIMARY KEY, email TEXT,"
           " kontakt TEXT GENERATED ALWAYS AS (email || '.test') STORED)")
    ok, columns, message = asyncio.run(service.table_columns(dst, "public.generiert"))
    assert ok, message
    by_name = {c.name: c for c in columns}
    assert by_name["kontakt"].generated is True
    assert by_name["email"].generated is False


def test_table_definitions_cover_defaults_and_checks(databases):
    service = _service()
    src = _profile("remote", SRC_DB, "off")
    _admin(SRC_DB, "DROP TABLE IF EXISTS def_a",
           """CREATE TABLE def_a (
                id INT PRIMARY KEY,
                zeit TIMESTAMP NOT NULL,
                tag DATE GENERATED ALWAYS AS (zeit::date) STORED,
                betrag NUMERIC(10,2) DEFAULT 1.00,
                geprueft INT CHECK (geprueft > 0))""")

    ok, clauses, message = asyncio.run(service.source_table_definitions(src, [f"{SRC_DB}.def_a"]))
    assert ok, message
    joined = " | ".join(clauses["def_a"])
    assert "SET DEFAULT" in joined and "CHECK" in joined
    # a generated column cannot be restored here, so it must not be attempted either
    assert "GENERATED" not in joined
    assert "NOT NULL" not in joined   # PostgreSQL lists those as checks; they are the column's own
