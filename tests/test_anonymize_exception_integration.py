"""The exception that keeps a value's shape, against both real servers.

Real case behind it: AxroCustomer decides whether a customer is a debtor by matching
its e-mail against ^[0-9]+@axro\\. (customer number at the company domain). The
anonymization replaced those addresses, so the admin lost the contacts tab, the debtor
header and "login as customer", silently and without an error anywhere.

    SQLTRANSFER_TEST_MYSQL=127.0.0.1:3398:root:test \
    SQLTRANSFER_TEST_POSTGRES=127.0.0.1:5499:postgres:test:<ca> pytest tests/test_anonymize_exception_integration.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from sqltransfer_app.anonymize import Rule, plan_table, Column
from sqltransfer_app.models import DBProfile
from sqltransfer_app.transfer import TransferService
from sqltransfer_app.tunnel import TunnelManager

pymysql = pytest.importorskip("pymysql")
psycopg = pytest.importorskip("psycopg")

MYSQL = os.environ.get("SQLTRANSFER_TEST_MYSQL")
POSTGRES = os.environ.get("SQLTRANSFER_TEST_POSTGRES")

# A debtor, an employee at the same domain, and an ordinary customer.
ZEILEN = [
    (1, "160547@axro.de"),
    (2, "eeromay@axro.de"),
    (3, "kunde@example.com"),
]

RULES = [Rule(id=1, pattern="*mail*", kind="email", exception=r"^[0-9]+@axro\.")]


class _Secrets:
    def __init__(self, password: str) -> None:
        self.password = password

    def get_secret(self, _key):
        return self.password

    def anonymization_salt(self):
        return "test-salt"


def _anonymize(password: str, profile: DBProfile, table: str) -> None:
    service = TransferService(storage=None, secret_store=_Secrets(password), tunnel_manager=TunnelManager())
    ok, columns, message = asyncio.run(service.table_columns(profile, table))
    assert ok, message
    plan = plan_table(table, columns, RULES)
    assert plan.targets, "the rule has to hit the column"
    ok, _affected, message = asyncio.run(service.anonymize_table(profile, plan))
    assert ok, message


@pytest.mark.skipif(not MYSQL, reason="SQLTRANSFER_TEST_MYSQL not set")
def test_mysql_keeps_the_debtor_address_and_replaces_the_others():
    host, port, user, password = MYSQL.split(":", 3)
    conn = pymysql.connect(host=host, port=int(port), user=user, password=password, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS sqlt_exc")
            cur.execute("USE sqlt_exc")
            cur.execute("DROP TABLE IF EXISTS kunde")
            cur.execute("CREATE TABLE kunde (id INT PRIMARY KEY, email VARCHAR(190) NOT NULL)")
            cur.executemany("INSERT INTO kunde VALUES (%s, %s)", ZEILEN)
    finally:
        conn.close()

    profile = DBProfile(id=None, name="dst", db_type="mysql", host=host, port=int(port),
                        database="sqlt_exc", username=user, password_secret_key="x")
    _anonymize(password, profile, "kunde")

    conn = pymysql.connect(host=host, port=int(port), user=user, password=password,
                           database="sqlt_exc", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email FROM kunde ORDER BY id")
            ergebnis = dict(cur.fetchall())
    finally:
        conn.close()

    assert ergebnis[1] == "160547@axro.de", "the debtor address carries meaning and stays"
    assert ergebnis[2] != "eeromay@axro.de", "an employee at the same domain is still a person"
    assert ergebnis[3] != "kunde@example.com"
    assert ergebnis[2].endswith("@example.invalid") and ergebnis[3].endswith("@example.invalid")


@pytest.mark.skipif(not POSTGRES, reason="SQLTRANSFER_TEST_POSTGRES not set")
def test_postgres_keeps_the_debtor_address_and_replaces_the_others():
    host, port, user, password, _ca = POSTGRES.split(":", 4)
    with psycopg.connect(host=host, port=int(port), user=user, password=password,
                         dbname="postgres", autocommit=True, sslmode="disable") as conn:
        conn.execute("DROP DATABASE IF EXISTS sqlt_exc WITH (FORCE)")
        conn.execute("CREATE DATABASE sqlt_exc")
    with psycopg.connect(host=host, port=int(port), user=user, password=password,
                         dbname="sqlt_exc", autocommit=True, sslmode="disable") as conn:
        conn.execute("CREATE TABLE kunde (id INT PRIMARY KEY, email VARCHAR(190) NOT NULL)")
        for zeile in ZEILEN:
            conn.execute("INSERT INTO kunde VALUES (%s, %s)", zeile)

    profile = DBProfile(id=None, name="dst", db_type="postgres", host=host, port=int(port),
                        database="sqlt_exc", username=user, password_secret_key="x", tls_mode="off")
    _anonymize(password, profile, "public.kunde")

    with psycopg.connect(host=host, port=int(port), user=user, password=password,
                         dbname="sqlt_exc", autocommit=True, sslmode="disable") as conn:
        ergebnis = dict(conn.execute("SELECT id, email FROM kunde ORDER BY id").fetchall())

    assert ergebnis[1] == "160547@axro.de"
    assert ergebnis[2] != "eeromay@axro.de"
    assert ergebnis[3] != "kunde@example.com"
