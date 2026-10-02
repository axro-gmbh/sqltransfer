"""JSON keys that are hostile to string building, against both real servers.

Key names come out of customer data. Everything here used to be built into the SQL
by hand, which broke on quotes and braces and silently skipped others:

    SQLTRANSFER_TEST_MYSQL=127.0.0.1:3398:root:test \
    SQLTRANSFER_TEST_POSTGRES=127.0.0.1:5499:postgres:test:/dev/null pytest tests/test_json_keys_integration.py

Both variables are needed; each half skips without its own server.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace

import pytest

from sqltransfer_app.anonymize import Rule, plan_json_column, plan_table
from sqltransfer_app.models import DBProfile
from sqltransfer_app.transfer import TransferService
from sqltransfer_app.tunnel import TunnelManager

pymysql = pytest.importorskip("pymysql")
psycopg = pytest.importorskip("psycopg")

MYSQL = os.environ.get("SQLTRANSFER_TEST_MYSQL")
POSTGRES = os.environ.get("SQLTRANSFER_TEST_POSTGRES")

# One fixture, every shape that broke path building, plus two silent-miss cases.
HOSTILE_KEYS = {
    "a'b": "quote@axro.de",
    'a"b': "doublequote@axro.de",
    "a\\b": "backslash@axro.de",
    "a,b": "comma@axro.de",
    "a}b": "brace@axro.de",
    "a.b": "dot@axro.de",
    "a b": "space@axro.de",
    " mail ": "padded@axro.de",
    "EMail": "upper@axro.de",
    "email": "lower@axro.de",
    "k" * 250: "longkey@axro.de",
}

RULES = [
    Rule(id=1, pattern="*mail*", kind="email"),
    Rule(id=2, pattern="a*b", kind="email"),
    Rule(id=3, pattern="k*", kind="email"),
    Rule(id=4, pattern="daten", kind="json"),
]


class _Secrets:
    def __init__(self, password: str) -> None:
        self.password = password

    def get_secret(self, _key):
        return self.password

    def anonymization_salt(self):
        return "test-salt"


def _service(password: str) -> TransferService:
    return TransferService(storage=None, secret_store=_Secrets(password), tunnel_manager=TunnelManager())


def _anonymize(service: TransferService, profile: DBProfile, table: str) -> tuple[bool, int, str]:
    """The three steps the app takes."""
    ok, columns, message = asyncio.run(service.table_columns(profile, table))
    assert ok, message
    plan = plan_table(table, columns, RULES)
    targets = []
    for column, column_type in plan.json_columns:
        ok, nodes, message = asyncio.run(service.json_paths(profile, table, column))
        assert ok, f"discovery failed for {column}: {message}"
        found, _skipped, _descend = plan_json_column(column, nodes, RULES)
        targets += [replace(t, column_type=column_type) for t in found]
    return asyncio.run(service.anonymize_table(profile, replace(plan, json_targets=tuple(targets))))


def _payload() -> str:
    # Every hostile key at the top level, and one of them one level down.
    document = dict(HOSTILE_KEYS)
    document["nested"] = {"email": "nested@axro.de", "a'b": "nested-quote@axro.de"}
    return json.dumps(document)


@pytest.mark.skipif(not MYSQL, reason="SQLTRANSFER_TEST_MYSQL not set")
def test_mysql_rewrites_every_hostile_key():
    host, port, user, password = MYSQL.split(":", 3)
    profile = DBProfile(id=None, name="dst", db_type="mysql", host=host, port=int(port),
                        database="sqlt_dst_idx", username=user, password_secret_key="x")
    setup = pymysql.connect(host=host, port=int(port), user=user, password=password, autocommit=True)
    try:
        with setup.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS sqlt_dst_idx")
    finally:
        setup.close()
    conn = pymysql.connect(host=host, port=int(port), user=user, password=password,
                           database="sqlt_dst_idx", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS hostile")
            cur.execute("CREATE TABLE hostile (id INT PRIMARY KEY, daten JSON)")
            cur.execute("INSERT INTO hostile VALUES (1, %s)", (_payload(),))
    finally:
        conn.close()

    ok, _affected, message = _anonymize(_service(password), profile, "hostile")
    assert ok, message

    conn = pymysql.connect(host=host, port=int(port), user=user, password=password,
                           database="sqlt_dst_idx", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT daten FROM hostile WHERE id = 1")
            result = json.loads(cur.fetchone()[0])
    finally:
        conn.close()

    assert "axro.de" not in json.dumps(result), f"left behind: {result}"
    assert set(result) == set(HOSTILE_KEYS) | {"nested"}, "a key was lost or invented"
    assert result["EMail"] != result["email"], "keys differing only in case must stay distinct"


@pytest.mark.skipif(not POSTGRES, reason="SQLTRANSFER_TEST_POSTGRES not set")
def test_postgres_rewrites_every_hostile_key():
    host, port, user, password, _ca = POSTGRES.split(":", 4)
    profile = DBProfile(id=None, name="dst", db_type="postgres", host=host, port=int(port),
                        database="sqlt_pg_dst", username=user, password_secret_key="x", tls_mode="off")
    with psycopg.connect(host=host, port=int(port), user=user, password=password,
                         dbname="postgres", autocommit=True, sslmode="disable") as conn:
        conn.execute("DROP DATABASE IF EXISTS sqlt_pg_dst WITH (FORCE)")
        conn.execute("CREATE DATABASE sqlt_pg_dst")
    with psycopg.connect(host=host, port=int(port), user=user, password=password,
                         dbname="sqlt_pg_dst", autocommit=True, sslmode="disable") as conn:
        conn.execute("CREATE TABLE hostile (id INT PRIMARY KEY, daten JSONB)")
        conn.execute("INSERT INTO hostile VALUES (1, %s::jsonb)", (_payload(),))

    ok, _affected, message = _anonymize(_service(password), profile, "public.hostile")
    assert ok, message

    with psycopg.connect(host=host, port=int(port), user=user, password=password,
                         dbname="sqlt_pg_dst", autocommit=True, sslmode="disable") as conn:
        result = conn.execute("SELECT daten FROM hostile WHERE id = 1").fetchone()[0]

    assert "axro.de" not in json.dumps(result), f"left behind: {result}"
    assert set(result) == set(HOSTILE_KEYS) | {"nested"}, "a key was lost or invented"
    assert result[" mail "].endswith("@example.invalid"), "a padded key must not be trimmed away"
