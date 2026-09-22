"""Integration test: secondary indexes must survive a MySQL -> MySQL transfer.

Needs a disposable MySQL server and is skipped otherwise, e.g.:

    docker run -d --name sqltransfer-indextest -e MYSQL_ROOT_PASSWORD=test \
        -p 127.0.0.1:3398:3306 mysql:8.4
    SQLTRANSFER_TEST_MYSQL=127.0.0.1:3398:root:test pytest tests/test_mysql_indexes.py

The test drops and recreates the databases `sqlt_src_idx` and `sqlt_dst_idx`,
so never point it at a server whose data matters.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from sqltransfer_app.models import DBProfile
from sqltransfer_app.transfer import TransferService
from sqltransfer_app.tunnel import TunnelManager

pymysql = pytest.importorskip("pymysql")
pytest.importorskip("apitap")

_SPEC = os.environ.get("SQLTRANSFER_TEST_MYSQL")
pytestmark = pytest.mark.skipif(not _SPEC, reason="SQLTRANSFER_TEST_MYSQL not set")

SRC_DB = "sqlt_src_idx"
DST_DB = "sqlt_dst_idx"

ITEMS_DDL = """
CREATE TABLE items (
  id INT NOT NULL,
  sku VARCHAR(32) NOT NULL,
  name VARCHAR(100) NOT NULL,
  category_id INT NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_sku (sku),
  KEY idx_category (category_id),
  KEY idx_cat_name (category_id, name),
  KEY idx_created (created_at)
) ENGINE=InnoDB
"""


def _conn_args() -> dict:
    host, port, user, password = _SPEC.split(":", 3)
    return {"host": host, "port": int(port), "user": user, "password": password}


class _Secrets:
    """Stands in for the keychain so the test never touches it."""

    def get_secret(self, _key):
        return _conn_args()["password"]


def _profile(role: str, database: str) -> DBProfile:
    args = _conn_args()
    return DBProfile(
        id=None,
        name=f"test-{role}",
        role=role,
        db_type="mysql",
        host=args["host"],
        port=args["port"],
        database=database,
        username=args["user"],
        password_secret_key="unused",
    )


def _exec(database: str | None, *statements: str) -> None:
    conn = pymysql.connect(**_conn_args(), database=database, autocommit=True)
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


def _indexes(database: str, table: str) -> set[tuple[str, bool, str]]:
    conn = pymysql.connect(**_conn_args(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT index_name, non_unique = 0, GROUP_CONCAT(column_name ORDER BY seq_in_index)
                FROM information_schema.statistics
                WHERE table_schema = %s AND table_name = %s
                GROUP BY index_name, non_unique
                """,
                (database, table),
            )
            return {(name, bool(unique), cols) for name, unique, cols in cur.fetchall()}
    finally:
        conn.close()


def _row_count(database: str, table: str) -> int:
    conn = pymysql.connect(**_conn_args(), database=database)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


@pytest.fixture()
def databases():
    _exec(
        None,
        f"DROP DATABASE IF EXISTS {SRC_DB}",
        f"DROP DATABASE IF EXISTS {DST_DB}",
        f"CREATE DATABASE {SRC_DB}",
        f"CREATE DATABASE {DST_DB}",
    )
    _exec(
        SRC_DB,
        ITEMS_DDL,
        "INSERT INTO items VALUES "
        "(1,'A-1','Alpha',10,'2026-01-01'),(2,'B-2','Beta',10,'2026-01-02'),"
        "(3,'C-3','Gamma',20,'2026-01-03'),(4,'D-4','Delta',20,'2026-01-04'),"
        "(5,'E-5','Epsilon',30,'2026-01-05')",
    )
    yield
    _exec(None, f"DROP DATABASE IF EXISTS {SRC_DB}", f"DROP DATABASE IF EXISTS {DST_DB}")


def _run_table_like_the_app(table: str) -> None:
    """Mirror the per-table MySQL flow in app.run_transfer_task (swap branch)."""
    service = TransferService(storage=None, secret_store=_Secrets(), tunnel_manager=TunnelManager())
    src = _profile("remote", SRC_DB)
    dst = _profile("local", DST_DB)

    async def flow() -> None:
        ok, source_indexes, message = await service.mysql_source_index_clauses(src, [table])
        assert ok, message
        temp = service.build_mysql_temp_name(table)
        result = await service.transfer_single_table(src, dst, table, dest_table=temp)
        assert result.status == "success", result.message
        ok, _applied, failed, message = await service.mysql_apply_index_clauses(
            dst, target_table=temp, clauses=source_indexes.get(table, {})
        )
        assert ok and not failed, message or failed
        swapped, message = await service.mysql_swap_temp_to_final(dst, temp_table=temp, final_table=table)
        assert swapped, message

    asyncio.run(flow())


def test_existing_destination_keeps_its_indexes(databases):
    _exec(DST_DB, ITEMS_DDL, "INSERT INTO items VALUES (99,'OLD','old row',1,'2020-01-01')")
    expected = _indexes(SRC_DB, "items")
    assert _indexes(DST_DB, "items") == expected  # precondition: destination starts fully indexed

    _run_table_like_the_app("items")

    assert _row_count(DST_DB, "items") == 5
    assert _indexes(DST_DB, "items") == expected


def test_new_destination_gets_the_source_indexes(databases):
    expected = _indexes(SRC_DB, "items")

    _run_table_like_the_app("items")

    assert _row_count(DST_DB, "items") == 5
    assert _indexes(DST_DB, "items") == expected
