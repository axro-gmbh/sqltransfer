"""Integration tests for the MySQL -> MySQL finalize steps.

Needs a disposable MySQL server and is skipped otherwise, e.g.:

    docker run -d --name sqltransfer-indextest -e MYSQL_ROOT_PASSWORD=test \
        -p 127.0.0.1:3398:3306 mysql:8.4
    docker exec sqltransfer-indextest mysql -uroot -ptest -e "SET PERSIST local_infile=1"
    SQLTRANSFER_TEST_MYSQL=127.0.0.1:3398:root:test pytest tests/test_mysql_integration.py

apitap loads with LOAD DATA LOCAL, which MySQL 8.4 disables server-side by default.

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
        ok, source_fks, _skipped, message = await service.mysql_source_fk_clauses(src, [table])
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
        # Foreign keys come last, once the outgoing table and its key names are gone.
        if table in source_fks:
            ok, _applied, failed, message = await service.mysql_apply_fk_clauses(dst, {table: source_fks[table]})
            assert ok and not failed, message or failed

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


CATEGORIES_DDL = "CREATE TABLE categories (id INT PRIMARY KEY, name VARCHAR(20)) ENGINE=InnoDB"
PRODUCTS_DDL = (
    "CREATE TABLE products (id INT PRIMARY KEY, category_id INT, parent_cat INT, "
    "KEY idx_cat (category_id), "
    "CONSTRAINT fk_prod_cat FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE, "
    "CONSTRAINT fk_prod_parent FOREIGN KEY (parent_cat) REFERENCES categories(id) ON DELETE SET NULL ON UPDATE CASCADE"
    ") ENGINE=InnoDB"
)


def _foreign_keys(database: str, table: str) -> set[tuple[str, str, str, str, str]]:
    conn = pymysql.connect(**_conn_args(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rc.constraint_name, rc.referenced_table_name,
                       GROUP_CONCAT(k.column_name ORDER BY k.ordinal_position),
                       rc.delete_rule, rc.update_rule
                FROM information_schema.referential_constraints rc
                JOIN information_schema.key_column_usage k
                  ON k.constraint_schema = rc.constraint_schema
                 AND k.constraint_name = rc.constraint_name
                 AND k.table_name = rc.table_name
                WHERE rc.constraint_schema = %s AND rc.table_name = %s
                GROUP BY rc.constraint_name, rc.referenced_table_name, rc.delete_rule, rc.update_rule
                """,
                (database, table),
            )
            return {tuple(row) for row in cur.fetchall()}
    finally:
        conn.close()


def test_swapped_table_keeps_its_foreign_keys(databases):
    for db in (SRC_DB, DST_DB):
        _exec(db, CATEGORIES_DDL, PRODUCTS_DDL, "INSERT INTO categories VALUES (1,'a'),(2,'b')")
    _exec(SRC_DB, "INSERT INTO products VALUES (10,1,NULL),(11,2,1)")
    _exec(DST_DB, "INSERT INTO products VALUES (99,1,NULL)")
    expected = _foreign_keys(SRC_DB, "products")
    assert _foreign_keys(DST_DB, "products") == expected  # precondition: destination starts with both FKs

    _run_table_like_the_app("products")

    assert _row_count(DST_DB, "products") == 2
    assert _foreign_keys(DST_DB, "products") == expected


def _service() -> TransferService:
    return TransferService(storage=None, secret_store=_Secrets(), tunnel_manager=TunnelManager())


def test_empty_table_referencing_a_missing_table_is_still_created(databases):
    # Fresh destination: the referenced table does not exist yet. MySQL 8 reports that
    # with 1824, MySQL 5.x and MariaDB with 1005; both must lead to a deferred key.
    _exec(
        SRC_DB,
        CATEGORIES_DDL,
        "CREATE TABLE archive (id INT PRIMARY KEY, category_id INT, "
        "CONSTRAINT fk_arch_cat FOREIGN KEY (category_id) REFERENCES categories(id)) ENGINE=InnoDB",
    )

    created, message, deferred = asyncio.run(
        _service().ensure_empty_mysql_table_from_source(
            _profile("remote", SRC_DB), _profile("local", DST_DB), source_table="archive", final_table="archive"
        )
    )

    assert created, message
    assert _row_count(DST_DB, "archive") == 0
    assert any("fk_arch_cat" in stmt for stmt in deferred)


def test_emptying_a_destination_removes_stale_rows(databases):
    # apitap's 0-row guard leaves an existing destination untouched when the source
    # table is empty, so the app has to clear it or the copy keeps rows the source lost.
    _exec(DST_DB, "CREATE TABLE audit (id INT PRIMARY KEY, note VARCHAR(20))",
          "INSERT INTO audit VALUES (1,'stale'),(2,'stale')")

    ok, deleted, error = asyncio.run(_service().mysql_empty_table(_profile("local", DST_DB), "audit"))

    assert ok, error
    assert deleted == 2
    assert _row_count(DST_DB, "audit") == 0


def test_emptying_works_on_a_table_other_tables_reference(databases):
    # TRUNCATE is refused for a parent table; the app must still be able to clear it.
    _exec(
        DST_DB,
        "CREATE TABLE categories (id INT PRIMARY KEY)",
        "INSERT INTO categories VALUES (1),(2)",
        "CREATE TABLE products (id INT PRIMARY KEY, category_id INT, "
        "CONSTRAINT fk_prod_cat FOREIGN KEY (category_id) REFERENCES categories(id))",
        "INSERT INTO products VALUES (10,1)",
    )

    ok, deleted, error = asyncio.run(_service().mysql_empty_table(_profile("local", DST_DB), "categories"))

    assert ok, error
    assert deleted == 2
    assert _row_count(DST_DB, "categories") == 0


# --- encryption -------------------------------------------------------------
# The disposable server generates its own self-signed certificate on first start,
# which is exactly the case "encrypted, certificate not checked" exists for.


def _with_tls(profile: DBProfile, mode: str) -> DBProfile:
    profile.tls_mode = mode
    return profile


def test_login_check_reports_encryption_for_each_setting(databases):
    service = _service()

    ok, message = asyncio.run(service.test_profile_connection(_with_tls(_profile("local", DST_DB), "off")))
    assert ok, message
    assert message.endswith("not encrypted")

    ok, message = asyncio.run(service.test_profile_connection(_with_tls(_profile("local", DST_DB), "required")))
    assert ok, message
    assert "encrypted (TLS" in message

    # A self-signed certificate must not pass a verified connection.
    ok, message = asyncio.run(service.test_profile_connection(_with_tls(_profile("local", DST_DB), "verified")))
    assert not ok
    assert "certificate" in message.lower()


def test_off_really_disables_tls(databases):
    # pymysql's own default tries TLS and would pass here silently; "off" must not.
    _exec(None, "SET GLOBAL require_secure_transport = ON")
    try:
        service = _service()
        ok, message = asyncio.run(service.test_profile_connection(_with_tls(_profile("local", DST_DB), "off")))
        assert not ok
        assert "insecure transport" in message.lower()

        ok, message = asyncio.run(service.test_profile_connection(_with_tls(_profile("local", DST_DB), "required")))
        assert ok, message
    finally:
        _exec(None, "SET GLOBAL require_secure_transport = OFF")


def test_apitap_transfer_follows_the_setting(databases):
    service = _service()

    src = _with_tls(_profile("remote", SRC_DB), "required")
    dst = _with_tls(_profile("local", DST_DB), "required")
    result = asyncio.run(service.transfer_single_table(src, dst, "items", dest_table="items_tls"))
    assert result.status == "success", result.message
    assert _row_count(DST_DB, "items_tls") == 5

    src = _with_tls(_profile("remote", SRC_DB), "verified")
    result = asyncio.run(service.transfer_single_table(src, dst, "items", dest_table="items_verified"))
    assert result.status == "failed"
    assert "certificate" in result.message.lower()  # refused for the right reason
