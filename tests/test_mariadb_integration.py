"""The JSON discovery against MariaDB, which answers differently than MySQL.

MariaDB 10.6.11 cuts JSON_TYPE to four characters as soon as a query uses DISTINCT:
'OBJE' instead of 'OBJECT'. A column holding 25088 objects was therefore reported as
"the whole value is an value", and nested objects were invisible too, so nothing in it
was ever anonymized. Found on a developer's local MariaDB.

    docker run -d --name sqlt-maria611 -e MARIADB_ROOT_PASSWORD=test -p 3396:3306 mariadb:10.6.11
    SQLTRANSFER_TEST_MARIADB=127.0.0.1:3396:root:test pytest tests/test_mariadb_integration.py
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

SPEC = os.environ.get("SQLTRANSFER_TEST_MARIADB")
pytestmark = pytest.mark.skipif(not SPEC, reason="SQLTRANSFER_TEST_MARIADB not set")

DOKUMENT = {
    "Rma": {"id": 205054, "company": "ComStern.de GmbH", "contact": "Muhammed Mayar / DL4",
            "email": "mmayar@axro.de", "phone": "undefined"},
    "RmaInvoices": {"RmaInvoice": [{"invoice_number": 698221,
                                    "RmaItems": {"RmaItem": [{"id": 2008641, "quantity": 6}]}}]},
}


def _args() -> dict:
    host, port, user, password = SPEC.split(":", 3)
    return {"host": host, "port": int(port), "user": user, "password": password}


class _Secrets:
    def get_secret(self, _key):
        return _args()["password"]

    def anonymization_salt(self):
        return "test-salt"


def _service() -> TransferService:
    return TransferService(storage=None, secret_store=_Secrets(), tunnel_manager=TunnelManager())


@pytest.fixture()
def tabelle():
    conn = pymysql.connect(**_args(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS sqlt_maria_rma")
            cur.execute("CREATE DATABASE sqlt_maria_rma")
            cur.execute("USE sqlt_maria_rma")
            # The developer's own DDL, binary collation and check constraint included.
            cur.execute("CREATE TABLE axro_rma (id INT PRIMARY KEY, rma_data LONGTEXT "
                        "COLLATE utf8mb4_bin NOT NULL CHECK (json_valid(`rma_data`)))")
            for nummer in (1, 2, 3):
                cur.execute("INSERT INTO axro_rma VALUES (%s, %s)", (nummer, json.dumps(DOKUMENT)))
    finally:
        conn.close()
    yield
    conn = pymysql.connect(**_args(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS sqlt_maria_rma")
    finally:
        conn.close()


def _profile() -> DBProfile:
    args = _args()
    return DBProfile(id=None, name="maria", db_type="mysql", host=args["host"], port=args["port"],
                     database="sqlt_maria_rma", username=args["user"], password_secret_key="x")


def test_the_root_is_recognised_as_an_object(tabelle):
    ok, nodes, message = asyncio.run(_service().json_paths(_profile(), "axro_rma", "rma_data"))
    assert ok, message
    pfade = [n.path for n in nodes]
    assert () not in pfade, "the root was reported as a non-object; JSON_TYPE came back cut"
    assert ("Rma",) in pfade and ("Rma", "email") in pfade, pfade


def test_the_values_are_really_rewritten(tabelle):
    service = _service()
    profile = _profile()
    regeln = [Rule(id=1, pattern="*mail*", kind="email"),
              Rule(id=2, pattern="rma_data", kind="json"),
              Rule(id=3, pattern="contact", kind="fullname")]

    ok, columns, message = asyncio.run(service.table_columns(profile, "axro_rma"))
    assert ok, message
    plan = plan_table("axro_rma", columns, regeln)
    assert plan.json_columns == (("rma_data", "longtext"),), plan.json_columns

    ok, nodes, message = asyncio.run(service.json_paths(profile, "axro_rma", "rma_data"))
    assert ok, message
    found, skipped, _descend = plan_json_column("rma_data", nodes, regeln)
    ziele = tuple(replace(t, column_type="longtext") for t in found)
    assert {t.path for t in ziele} == {("Rma", "email"), ("Rma", "contact")}, ziele

    ok, _rows, message = asyncio.run(service.anonymize_table(profile, replace(plan, json_targets=ziele)))
    assert ok, message

    conn = pymysql.connect(**_args(), database="sqlt_maria_rma", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rma_data FROM axro_rma WHERE id = 1")
            dokument = json.loads(cur.fetchone()[0])
    finally:
        conn.close()

    assert dokument["Rma"]["email"].endswith("@example.invalid"), dokument["Rma"]["email"]
    assert dokument["Rma"]["contact"] != "Muhammed Mayar / DL4"
    # The structure survives, including the lists nobody touches.
    assert dokument["RmaInvoices"]["RmaInvoice"][0]["invoice_number"] == 698221
    assert dokument["Rma"]["company"] == "ComStern.de GmbH", "no rule for it, so it stays"
