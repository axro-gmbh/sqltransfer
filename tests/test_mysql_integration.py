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
import json
import os
import re
import time

import pymysql
import pytest

from sqltransfer_app.anonymize import Column, JsonNode, Rule, plan_table
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

    def anonymization_salt(self):
        # Fixed, so the expected replacements stay the same between runs.
        return "test-salt"

    def get_secret(self, _key):
        return _conn_args()["password"]


def _profile(role: str, database: str) -> DBProfile:
    args = _conn_args()
    return DBProfile(
        id=None,
        name=f"test-{role}",
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
        ok, has_inbound, message = await service.mysql_table_has_inbound_fk(dst, table)
        assert ok, message
        if has_inbound:
            # in-place: the rows are replaced and the table keeps standing, so its
            # definition has to be compared against the source's and fixed.
            replaced, message = await service.mysql_replace_final_from_temp(dst, temp_table=temp, final_table=table)
            assert replaced, message
            ok, definitions, message = await service.table_definitions(src, [table])
            assert ok, message
            before_swap, after_swap = definitions.get(table, ([], []))
            if before_swap:
                ok, _failed, message = await service.apply_definition_clauses(dst, table, before_swap)
                assert ok, message
            if after_swap:
                await service.apply_definition_clauses(dst, table, after_swap, True)
            return
        ok, _applied, failed, message = await service.mysql_apply_index_clauses(
            dst, target_table=temp, clauses=source_indexes.get(table, {})
        )
        assert ok and not failed, message or failed
        # The definition comes after the indexes and before the swap, as in the app.
        ok, definitions, message = await service.table_definitions(src, [table])
        assert ok, message
        before_swap, after_swap = definitions.get(table, ([], []))
        if before_swap:
            ok, _failed, message = await service.apply_definition_clauses(dst, temp, before_swap)
            assert ok, message
        swapped, message = await service.mysql_swap_temp_to_final(dst, temp_table=temp, final_table=table)
        assert swapped, message
        if after_swap:
            # after the swap, like the foreign keys: the names belong to the database
            ok, failed, message = await service.apply_definition_clauses(dst, table, after_swap, True)
            assert ok and not failed, message or failed
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


def _rows(database: str, sql: str) -> list[tuple]:
    import pymysql

    args = _conn_args()
    conn = pymysql.connect(host=args["host"], port=args["port"], user=args["user"],
                           password=args["password"], database=database, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())
    finally:
        conn.close()


def _anonymize(dst, table: str, rules: list[Rule]) -> tuple[bool, int, str]:
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
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS kunde",
          "CREATE TABLE kunde (id INT PRIMARY KEY, email VARCHAR(190) UNIQUE, ort VARCHAR(80), plz VARCHAR(5), umsatz INT)",
          "INSERT INTO kunde VALUES (1,'a@axro.de','Hamburg','20095',10),"
          " (2,'b@axro.de',NULL,'10115',20), (3,'','Köln','',30)")

    ok, affected, message = _anonymize(dst, "kunde", [Rule(id=1, pattern="*mail*", kind="email"),
                                                      Rule(id=2, pattern="ort", kind="city"),
                                                      Rule(id=3, pattern="plz", kind="postcode")])
    assert ok, message
    assert affected == 3

    rows = _rows(DST_DB, "SELECT id, email, ort, plz, umsatz FROM kunde ORDER BY id")
    assert [r[4] for r in rows] == [10, 20, 30]                      # untouched column
    assert all(r[1].endswith("@example.invalid") or r[1] == "" for r in rows)
    assert "axro.de" not in str(rows)                                # nothing real left
    assert rows[1][2] is None                                        # NULL stays NULL
    assert rows[2][1] == ""                                          # empty stays empty
    assert all(re.fullmatch(r"\d{5}", r[3]) for r in rows if r[3])   # Review Focus 6: no minus


def test_the_same_value_yields_the_same_replacement(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS a", "DROP TABLE IF EXISTS b",
          "CREATE TABLE a (id INT PRIMARY KEY, email VARCHAR(190))",
          "CREATE TABLE b (id INT PRIMARY KEY, email VARCHAR(190))",
          "INSERT INTO a VALUES (1,'same@axro.de')", "INSERT INTO b VALUES (1,'same@axro.de')")
    rules = [Rule(id=1, pattern="email", kind="email")]
    for table in ("a", "b"):
        assert _anonymize(dst, table, rules)[0]
    assert _rows(DST_DB, "SELECT email FROM a")[0][0] == _rows(DST_DB, "SELECT email FROM b")[0][0]


def test_a_unique_column_stays_unique(databases):
    dst = _profile("local", DST_DB)
    values = ",".join(f"({i},'kunde{i}@axro.de')" for i in range(1, 201))
    _exec(DST_DB, "DROP TABLE IF EXISTS viele",
          "CREATE TABLE viele (id INT PRIMARY KEY, email VARCHAR(190) UNIQUE)",
          f"INSERT INTO viele VALUES {values}")

    ok, _affected, message = _anonymize(dst, "viele", [Rule(id=1, pattern="email", kind="email")])
    assert ok, message  # a collision would surface here as a duplicate key error
    assert _rows(DST_DB, "SELECT COUNT(DISTINCT email) FROM viele")[0][0] == 200


def test_a_matching_column_of_the_wrong_type_is_left_alone(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS numerisch",
          "CREATE TABLE numerisch (id INT PRIMARY KEY, telefon BIGINT)",
          "INSERT INTO numerisch VALUES (1, 491234567)")
    ok, affected, message = _anonymize(dst, "numerisch", [Rule(id=1, pattern="telefon", kind="phone")])
    assert ok and affected == 0, message
    assert _rows(DST_DB, "SELECT telefon FROM numerisch")[0][0] == 491234567


def test_a_table_the_destination_cannot_see_is_an_error_not_an_empty_plan(databases):
    # Returning (True, []) made the app report success while nothing was anonymized.
    service = _service()
    dst = _profile("local", DST_DB)
    ok, columns, message = asyncio.run(service.table_columns(dst, "gibt_es_nicht"))
    assert not ok and not columns
    assert "gibt_es_nicht" in message


def test_a_replacement_is_cut_to_the_column_length(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS eng",
          "CREATE TABLE eng (id INT PRIMARY KEY, email VARCHAR(12), ort VARCHAR(6))",
          "INSERT INTO eng VALUES (1,'a@axro.de','Kiel')")
    ok, affected, message = _anonymize(dst, "eng", [Rule(id=1, pattern="email", kind="email"),
                                                    Rule(id=2, pattern="ort", kind="city")])
    assert ok, message  # a too-long replacement used to abort the whole transfer
    row = _rows(DST_DB, "SELECT email, ort FROM eng")[0]
    assert len(row[0]) <= 12 and len(row[1]) <= 6
    assert "axro.de" not in row[0]


def test_names_do_not_collapse_onto_a_handful_of_values(databases):
    dst = _profile("local", DST_DB)
    values = ",".join(f"({i},'Person {i}')" for i in range(1, 201))
    _exec(DST_DB, "DROP TABLE IF EXISTS namen",
          "CREATE TABLE namen (id INT PRIMARY KEY, name VARCHAR(190))",
          f"INSERT INTO namen VALUES {values}")
    ok, _affected, message = _anonymize(dst, "namen", [Rule(id=1, pattern="name", kind="fullname")])
    assert ok, message
    distinct = _rows(DST_DB, "SELECT COUNT(DISTINCT name) FROM namen")[0][0]
    assert distinct >= 40, f"only {distinct} distinct names over 200 rows"


def test_in_place_replace_survives_generated_columns(databases):
    # Shopware's `order` has order_date as a stored generated column, and
    # "INSERT INTO final SELECT * FROM temp" died on it with error 3105.
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS gen_tmp", "DROP TABLE IF EXISTS gen",
          "CREATE TABLE gen (id INT PRIMARY KEY, email VARCHAR(190),"
          " order_date_time DATETIME NOT NULL,"
          " order_date DATE AS (CAST(order_date_time AS DATE)) STORED,"
          " tax_status VARCHAR(32) AS (CONCAT('t-', id)) VIRTUAL)",
          "INSERT INTO gen (id, email, order_date_time) VALUES (1,'alt@axro.de','2026-01-01 10:00:00')",
          # the temp table is what apitap leaves behind: plain columns, values included
          "CREATE TABLE gen_tmp (id INT PRIMARY KEY, email VARCHAR(190), order_date_time DATETIME NOT NULL,"
          " order_date DATE, tax_status VARCHAR(32))",
          "INSERT INTO gen_tmp VALUES (2,'neu@axro.de','2026-02-02 11:00:00','2026-02-02','t-2')")

    ok, message = asyncio.run(service.mysql_replace_final_from_temp(dst, temp_table="gen_tmp", final_table="gen"))
    assert ok, message
    rows = _rows(DST_DB, "SELECT id, email, order_date, tax_status FROM gen")
    assert len(rows) == 1 and rows[0][0] == 2
    assert str(rows[0][2]) == "2026-02-02"      # recomputed by the database
    assert rows[0][3] == "t-2"


def test_a_generated_column_is_never_anonymized(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS gen2",
          "CREATE TABLE gen2 (id INT PRIMARY KEY, email VARCHAR(190),"
          " kontakt_mail VARCHAR(220) AS (CONCAT(email, '.test')) STORED)",
          "INSERT INTO gen2 (id, email) VALUES (1,'alt@axro.de')")

    ok, affected, message = _anonymize(dst, "gen2", [Rule(id=1, pattern="*mail*", kind="email")])
    assert ok, message  # an UPDATE on a generated column would fail outright
    row = _rows(DST_DB, "SELECT email, kontakt_mail FROM gen2")[0]
    assert row[0].endswith("@example.invalid")
    assert row[1] == row[0] + ".test"           # followed along, computed by the database


def test_generated_columns_of_the_source_are_read_in_one_go(databases):
    service = _service()
    src = _profile("remote", SRC_DB)
    _exec(SRC_DB, "DROP TABLE IF EXISTS gen_a", "DROP TABLE IF EXISTS gen_b",
          "CREATE TABLE gen_a (id INT PRIMARY KEY, zeit DATETIME NOT NULL,"
          " tag DATE AS (CAST(zeit AS DATE)) STORED, kennung VARCHAR(16) AS (CONCAT('a-', id)) VIRTUAL)",
          "CREATE TABLE gen_b (id INT PRIMARY KEY, name VARCHAR(50))")

    ok, generated, message = asyncio.run(
        service.source_generated_columns(src, [f"{SRC_DB}.gen_a", f"{SRC_DB}.gen_b"])
    )
    assert ok, message
    assert generated.get("gen_a") == ["tag", "kennung"]
    assert "gen_b" not in generated  # a table without generated columns is simply absent


def test_json_paths_are_discovered_level_by_level(databases):
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS mitjson",
          "CREATE TABLE mitjson (id INT PRIMARY KEY, custom_fields JSON)",
          """INSERT INTO mitjson VALUES
             (1, '{"email":"a@axro.de","adresse":{"ort":"Hamburg","nummer":7},"positionen":[{"email":"x@y.de"}]}'),
             (2, '{"email":"b@axro.de","notiz":"ohne"}'),
             (3, NULL)""")

    ok, nodes, message = asyncio.run(service.json_paths(dst, "mitjson", "custom_fields"))
    assert ok, message
    found = {node.path: node.types for node in nodes}
    assert found[("email",)] == frozenset({"STRING"})
    assert found[("adresse",)] == frozenset({"OBJECT"})
    assert found[("adresse", "ort")] == frozenset({"STRING"})
    assert found[("adresse", "nummer")] == frozenset({"INTEGER"})
    assert found[("positionen",)] == frozenset({"ARRAY"})
    assert ("positionen", "email") not in found   # arrays are not followed


def _questions(database: str) -> int:
    return int(_rows(database, "SHOW SESSION STATUS LIKE 'Questions'")[0][1])


def test_json_discovery_costs_a_handful_of_queries_not_one_per_key(databases):
    # The old shape ran one query per parent path: a document with 30 objects, each
    # with 6 keys, cost over 200 round trips, every one of them a full column scan.
    service = _service()
    dst = _profile("local", DST_DB)
    document = {f"gruppe{i}": {f"feld{j}": "x" for j in range(6)} for i in range(30)}
    _exec(DST_DB, "DROP TABLE IF EXISTS breit",
          "CREATE TABLE breit (id INT PRIMARY KEY, daten JSON)")
    conn = pymysql.connect(**_conn_args(), database=DST_DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO breit VALUES (1, %s)", (json.dumps(document),))
    finally:
        conn.close()

    before = _questions(DST_DB)
    ok, nodes, message = asyncio.run(service.json_paths(dst, "breit", "daten"))
    after = _questions(DST_DB)
    assert ok, message
    assert len([n for n in nodes if len(n.path) == 2]) == 180
    # one validity check, one root type, one for level 1, two batches for level 2
    assert after - before <= 8, f"{after - before} queries for 30 objects"


def test_a_text_column_that_is_not_json_is_refused_cleanly(databases):
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS keinjson",
          "CREATE TABLE keinjson (id INT PRIMARY KEY, notiz LONGTEXT)",
          "INSERT INTO keinjson VALUES (1, 'das ist kein json')")
    ok, nodes, message = asyncio.run(service.json_paths(dst, "keinjson", "notiz"))
    assert not ok and not nodes
    assert "json" in message.lower()


def test_values_inside_json_are_replaced_and_the_structure_survives(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS jsonkunde",
          "CREATE TABLE jsonkunde (id INT PRIMARY KEY, email VARCHAR(190), custom_fields JSON,"
          " daten LONGTEXT)",
          """INSERT INTO jsonkunde VALUES
             (1,'max@axro.de','{"email":"max@axro.de","adresse":{"ort":"Hamburg"},"umsatz":42,
                                "positionen":[{"email":"x@y.de"}]}','{"email":"max@axro.de"}'),
             (2,'erika@axro.de','{"notiz":"ohne mail"}','{}'),
             (3,'leer@axro.de', NULL, NULL)""")

    rules = [Rule(id=1, pattern="*mail*", kind="email"), Rule(id=2, pattern="ort", kind="city"),
             Rule(id=3, pattern="daten", kind="json")]
    ok, affected, message = _anonymize(dst, "jsonkunde", rules)
    assert ok, message

    rows = _rows(DST_DB, "SELECT id, email, custom_fields, daten FROM jsonkunde ORDER BY id")
    first = json.loads(rows[0][2])
    assert first["email"].endswith("@example.invalid")
    assert first["email"] == rows[0][1]              # same original, same fake, column and JSON
    assert first["umsatz"] == 42                     # untouched sibling
    assert first["adresse"]["ort"] != "Hamburg"      # nested replaced
    assert first["positionen"] == [{"email": "x@y.de"}]  # arrays stay, and are reported
    assert json.loads(rows[0][3])["email"] == first["email"]  # the longtext column too
    assert json.loads(rows[1][2]) == {"notiz": "ohne mail"}
    assert rows[2][2] is None and rows[2][3] is None
    assert "max@axro.de" not in str(rows) and "Hamburg" not in str(rows)


def test_a_text_column_marked_as_json_but_holding_prose_is_reported(databases):
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS prosa",
          "CREATE TABLE prosa (id INT PRIMARY KEY, daten LONGTEXT)",
          "INSERT INTO prosa VALUES (1, 'kein json, nur text')")
    service = _service()
    ok, columns, _ = asyncio.run(service.table_columns(dst, "prosa"))
    plan = plan_table("prosa", columns, [Rule(id=1, pattern="daten", kind="json")])
    assert plan.json_columns == (("daten", "longtext"),)
    ok, nodes, message = asyncio.run(service.json_paths(dst, "prosa", "daten"))
    assert not ok and "json" in message.lower()


def test_a_column_that_is_one_big_array_is_reported(databases):
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS nurliste",
          "CREATE TABLE nurliste (id INT PRIMARY KEY, daten JSON)",
          """INSERT INTO nurliste VALUES (1, '[{"email":"x@axro.de"}]')""")
    ok, nodes, message = asyncio.run(service.json_paths(dst, "nurliste", "daten"))
    assert ok, message
    assert [(n.path, sorted(n.types)) for n in nodes] == [((), ["ARRAY"])]


def test_nesting_past_the_limit_is_reported(databases):
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS tief",
          "CREATE TABLE tief (id INT PRIMARY KEY, daten JSON)",
          """INSERT INTO tief VALUES (1, '{"a":{"b":{"c":{"d":{"email":"x@axro.de"}}}}}')""")
    ok, nodes, message = asyncio.run(service.json_paths(dst, "tief", "daten"))
    assert ok, message
    deep = [n for n in nodes if "TOO_DEEP" in n.types]
    assert [n.path for n in deep] == [("a", "b", "c", "d")]


def test_table_definitions_are_read_for_every_table_in_scope(databases):
    service = _service()
    src = _profile("remote", SRC_DB)
    _exec(SRC_DB, "DROP TABLE IF EXISTS def_a", "DROP TABLE IF EXISTS def_b",
          """CREATE TABLE def_a (
               id INT PRIMARY KEY,
               lauf BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE,
               zeit DATETIME NOT NULL,
               tag DATE AS (CAST(zeit AS DATE)) STORED,
               betrag DECIMAL(10,2) DEFAULT 1.00,
               geprueft INT,
               CONSTRAINT chk_a CHECK (geprueft > 0))""",
          "CREATE TABLE def_b (id INT PRIMARY KEY, name VARCHAR(20))",
          "INSERT INTO def_a (id, zeit, geprueft) VALUES (1,'2026-01-01 10:00:00',3)")

    ok, definitions, message = asyncio.run(
        service.table_definitions(src, [f"{SRC_DB}.def_a", f"{SRC_DB}.def_b"])
    )
    assert ok, message
    assert "def_b" not in definitions                 # nothing to restore there
    before, after = definitions["def_a"]
    joined = " | ".join(before)
    assert "GENERATED ALWAYS AS" in joined and "DEFAULT '1.00'" in joined
    assert joined.index("GENERATED") < joined.index("AUTO_INCREMENT = ")
    assert after == ["ADD CONSTRAINT `chk_a` CHECK ((`geprueft` > 0))"]


def _normalised_ddl(database: str, table: str) -> str:
    ddl = _rows(database, f"SHOW CREATE TABLE {table}")[0][1]
    ddl = re.sub(r"AUTO_INCREMENT=\d+", "AUTO_INCREMENT=N", ddl)
    return re.sub(r"CONSTRAINT `[^`]+` CHECK", "CONSTRAINT `chk` CHECK", ddl)


def test_the_copy_carries_the_source_definition(databases):
    _exec(SRC_DB, "DROP TABLE IF EXISTS voll",
          """CREATE TABLE voll (
               id INT PRIMARY KEY,
               lauf BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE,
               zeit DATETIME NOT NULL,
               tag DATE AS (CAST(zeit AS DATE)) STORED,
               betrag DECIMAL(10,2) DEFAULT 1.00,
               geprueft INT CHECK (geprueft > 0),
               KEY idx_tag (tag, betrag))""",
          "INSERT INTO voll (id, zeit, geprueft) VALUES (1,'2026-01-01 10:00:00',3)")
    _exec(DST_DB, "DROP TABLE IF EXISTS voll")

    _run_table_like_the_app("voll")

    assert _normalised_ddl(DST_DB, "voll") == _normalised_ddl(SRC_DB, "voll")

    # and it behaves like the original
    _exec(DST_DB, "INSERT INTO voll (id, zeit, geprueft) VALUES (2,'2026-02-02 11:00:00',5)")
    row = _rows(DST_DB, "SELECT tag, betrag FROM voll WHERE id = 2")[0]
    assert str(row[0]) == "2026-02-02", "the generated column must recompute"
    assert float(row[1]) == 1.00, "the default must apply"
    with pytest.raises(Exception):
        _exec(DST_DB, "INSERT INTO voll (id, zeit, geprueft) VALUES (3,'2026-03-03 12:00:00',0)")


HOSTILE_SCHEMA = """CREATE TABLE schwierig (
  id INT PRIMARY KEY,
  lauf BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE,
  angelegt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  wort VARCHAR(20) NOT NULL DEFAULT 'null',
  klammer VARCHAR(20) DEFAULT '(none)',
  coll VARCHAR(20) COLLATE utf8mb4_bin,
  zeit DATETIME NOT NULL,
  tag DATE AS (CAST(zeit AS DATE)) STORED,
  mit_text VARCHAR(40) COLLATE utf8mb4_bin AS (CONCAT('x-', coll)) STORED NOT NULL,
  virtuell VARCHAR(40) AS (CONCAT('v-', coll)) VIRTUAL,
  betrag DECIMAL(10,2) DEFAULT 1.00,
  art ENUM('a','b') DEFAULT 'a',
  geprueft INT,
  CONSTRAINT chk_positiv CHECK (geprueft > 0),
  CONSTRAINT chk_wort CHECK (wort <> 'bad'),
  KEY idx_tag (tag, betrag)
)"""


def test_the_copy_carries_a_hostile_source_definition(databases):
    # Everything that broke the first attempt: a function default, string literals in
    # expressions and checks, a VIRTUAL column, NOT NULL and COLLATE on a generated
    # column, an enum, and defaults that read like keywords.
    _exec(SRC_DB, "DROP TABLE IF EXISTS schwierig", HOSTILE_SCHEMA,
          "INSERT INTO schwierig (id, coll, zeit, geprueft) VALUES (1,'eins','2026-01-01 10:00:00',3)")
    _exec(DST_DB, "DROP TABLE IF EXISTS schwierig")

    _run_table_like_the_app("schwierig")

    assert _normalised_ddl(DST_DB, "schwierig") == _normalised_ddl(SRC_DB, "schwierig")

    _exec(DST_DB, "INSERT INTO schwierig (id, coll, zeit, geprueft) VALUES (2,'zwei','2026-02-02 11:00:00',5)")
    row = _rows(DST_DB, "SELECT tag, mit_text, virtuell, betrag, wort, klammer, art, lauf FROM schwierig WHERE id = 2")[0]
    assert str(row[0]) == "2026-02-02"      # stored generated column recomputes
    assert row[1] == "x-zwei"               # with NOT NULL and its own collation
    assert row[2] == "v-zwei"               # virtual column too
    assert float(row[3]) == 1.00            # numeric default
    assert row[4] == "null"                 # a string that reads like a keyword
    assert row[5] == "(none)"               # a string that reads like an expression
    assert row[6] == "a"                    # enum default
    assert int(row[7]) > 1                  # auto_increment counted on
    with pytest.raises(Exception):
        _exec(DST_DB, "INSERT INTO schwierig (id, coll, zeit, geprueft) VALUES (3,'d','2026-03-03 12:00:00',0)")
    with pytest.raises(Exception):
        _exec(DST_DB, "INSERT INTO schwierig (id, wort, coll, zeit, geprueft) VALUES (4,'bad','d','2026-03-03 12:00:00',1)")


def test_in_place_replace_keeps_columns_with_a_function_default(databases):
    # DEFAULT_GENERATED sits in the same information_schema column as STORED GENERATED.
    # Reading it as "generated" dropped created_at-style columns from the copy without
    # a word, and a table made only of such columns failed with "no columns to copy".
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS fn_tmp", "DROP TABLE IF EXISTS fn",
          "CREATE TABLE fn (id INT PRIMARY KEY, angelegt DATETIME DEFAULT CURRENT_TIMESTAMP,"
          " geaendert DATETIME DEFAULT (NOW()), tag DATE AS (CAST(angelegt AS DATE)) STORED)",
          "INSERT INTO fn (id, angelegt, geaendert) VALUES (1,'2020-01-01 08:00:00','2020-01-01 08:00:00')",
          "CREATE TABLE fn_tmp (id INT PRIMARY KEY, angelegt DATETIME, geaendert DATETIME, tag DATE)",
          "INSERT INTO fn_tmp VALUES (2,'2026-06-06 09:00:00','2026-06-07 10:00:00','2026-06-06')")

    ok, message = asyncio.run(service.mysql_replace_final_from_temp(dst, temp_table="fn_tmp", final_table="fn"))
    assert ok, message
    row = _rows(DST_DB, "SELECT id, angelegt, geaendert, tag FROM fn")[0]
    assert row[0] == 2
    assert str(row[1]) == "2026-06-06 09:00:00", "a function default does not make a column generated"
    assert str(row[2]) == "2026-06-07 10:00:00"
    assert str(row[3]) == "2026-06-06", "the real generated column is recomputed by the database"


def test_a_table_with_inbound_fk_gets_its_definition_back_too(databases):
    # The in-place path replaces rows and keeps the table, so a destination created by
    # an older version keeps its plain columns forever unless the definition is checked.
    _exec(SRC_DB, "DROP TABLE IF EXISTS kind", "DROP TABLE IF EXISTS eltern",
          """CREATE TABLE eltern (
               id INT PRIMARY KEY,
               zeit DATETIME NOT NULL,
               tag DATE AS (CAST(zeit AS DATE)) STORED,
               betrag DECIMAL(10,2) DEFAULT 1.00)""",
          "CREATE TABLE kind (id INT PRIMARY KEY, eltern_id INT, CONSTRAINT fk_k FOREIGN KEY (eltern_id) REFERENCES eltern(id))",
          "INSERT INTO eltern (id, zeit) VALUES (1,'2026-01-01 10:00:00')")
    # the destination as an older version left it: plain columns, and a table pointing at it
    _exec(DST_DB, "DROP TABLE IF EXISTS kind", "DROP TABLE IF EXISTS eltern",
          "CREATE TABLE eltern (id INT PRIMARY KEY, zeit DATETIME NOT NULL, tag DATE, betrag DECIMAL(10,2))",
          "CREATE TABLE kind (id INT PRIMARY KEY, eltern_id INT, CONSTRAINT fk_k FOREIGN KEY (eltern_id) REFERENCES eltern(id))",
          "INSERT INTO eltern VALUES (9,'2020-01-01 08:00:00','2020-01-01',5.00)")

    _run_table_like_the_app("eltern")

    assert _normalised_ddl(DST_DB, "eltern") == _normalised_ddl(SRC_DB, "eltern")
    _exec(DST_DB, "INSERT INTO eltern (id, zeit) VALUES (2,'2026-02-02 11:00:00')")
    row = _rows(DST_DB, "SELECT tag, betrag FROM eltern WHERE id = 2")[0]
    assert str(row[0]) == "2026-02-02" and float(row[1]) == 1.00


def test_the_auto_increment_counter_continues_in_the_copy(databases):
    # The DDL comparison normalises AUTO_INCREMENT=n away, so the counter needs its
    # own check: without it the copy would hand out ids the source already used.
    _exec(SRC_DB, "DROP TABLE IF EXISTS zaehler",
          # a signed key: apitap reads the cursor column as i64 and refuses BIGINT UNSIGNED
          "CREATE TABLE zaehler (id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, name VARCHAR(20))",
          "INSERT INTO zaehler (name) VALUES ('a'),('b'),('c')")
    _exec(DST_DB, "DROP TABLE IF EXISTS zaehler")

    _run_table_like_the_app("zaehler")

    source_next = _rows(SRC_DB, "SELECT auto_increment FROM information_schema.tables "
                                f"WHERE table_schema='{SRC_DB}' AND table_name='zaehler'")[0][0]
    copy_next = _rows(DST_DB, "SELECT auto_increment FROM information_schema.tables "
                              f"WHERE table_schema='{DST_DB}' AND table_name='zaehler'")[0][0]
    assert copy_next == source_next, "the counter must survive the swap, not restart"
    _exec(DST_DB, "INSERT INTO zaehler (name) VALUES ('d')")
    assert _rows(DST_DB, "SELECT id FROM zaehler WHERE name='d'")[0][0] == source_next


def test_auto_increment_without_its_key_is_reported_not_fatal(databases):
    # AUTO_INCREMENT needs a key on its column. If the index step could not build it,
    # the column stays plain and the run goes on; aborting here would turn a warning
    # into a lost transfer.
    service = _service()
    dst = _profile("local", DST_DB)
    _exec(DST_DB, "DROP TABLE IF EXISTS ohne_key",
          "CREATE TABLE ohne_key (id INT PRIMARY KEY, lauf BIGINT NOT NULL)")

    ok, failed, message = asyncio.run(service.apply_definition_clauses(
        dst, "ohne_key",
        ["MODIFY COLUMN `lauf` bigint NOT NULL AUTO_INCREMENT", "AUTO_INCREMENT = 7"],
        tolerate_failures=True,
    ))
    assert ok, message
    assert len(failed) == 1 and "auto column" in failed[0]


def test_the_scope_database_decides_which_tables_are_listed(databases):
    """A whole-database scope names its own database; the profile's is only the fallback.

    Everything after the listing (indexes, definitions) derives the schema from the
    table name, so a bare name from the wrong database silently restores nothing.
    """
    service = _service()

    # the usual case: profile and scope name the same database
    ok, tables, message = asyncio.run(service.list_tables(_profile("remote", SRC_DB), schema_hint=SRC_DB))
    assert ok, message
    assert "items" in tables

    # the profile has no database of its own
    ok, tables, message = asyncio.run(service.list_tables(_profile("remote", ""), schema_hint=SRC_DB))
    assert ok, message
    assert f"{SRC_DB}.items" in tables, tables

    # the profile points somewhere else
    ok, tables, message = asyncio.run(service.list_tables(_profile("remote", DST_DB), schema_hint=SRC_DB))
    assert ok, message
    assert f"{SRC_DB}.items" in tables, tables


def test_one_unreadable_table_does_not_cost_the_others_their_definitions(databases, monkeypatch):
    """A table whose DDL cannot be read is named and skipped, the run goes on.

    Real finding: one table in a customer database returned bytes that are not UTF-8
    ('utf-8' codec can't decode byte 0xa9), and that one table silently cost every
    other table its generated columns, defaults and checks.
    """
    _exec(SRC_DB,
          "CREATE TABLE zaehler (id INT PRIMARY KEY, lauf INT NOT NULL AUTO_INCREMENT, UNIQUE KEY u (lauf))",
          "CREATE TABLE sperrig (id INT PRIMARY KEY, lauf INT NOT NULL AUTO_INCREMENT, UNIQUE KEY u (lauf))")

    from sqltransfer_app import transfer as transfer_module

    echt = transfer_module.parse_mysql_table_definition

    def kaputt(ddl: str, codec: str = "utf-8"):
        if "`sperrig`" in ddl:
            raise UnicodeDecodeError("utf-8", b"\xa9", 0, 1, "invalid start byte")
        return echt(ddl, codec=codec)

    monkeypatch.setattr(transfer_module, "parse_mysql_table_definition", kaputt)

    ok, definitions, message = asyncio.run(
        _service().table_definitions(_profile("remote", SRC_DB), ["sperrig", "zaehler"])
    )
    assert ok, message
    assert "zaehler" in definitions, "the readable table must keep its definition"
    assert "sperrig" in message and "0xa9" in message, message


def test_a_definition_in_a_charset_the_connection_cannot_decode_is_still_read(databases):
    """MySQL writes the DDL in the table's own charset, not the connection's."""
    _exec(SRC_DB, "CREATE TABLE marke (id INT PRIMARY KEY, zeichen VARCHAR(20) CHARACTER SET latin1 "
                   "DEFAULT _latin1 0xA9, lauf INT NOT NULL AUTO_INCREMENT, UNIQUE KEY u (lauf)) DEFAULT CHARSET=latin1")

    ok, definitions, message = asyncio.run(_service().table_definitions(_profile("remote", SRC_DB), ["marke"]))
    assert ok, message
    before, _after = definitions["marke"]
    assert any("`zeichen`" in clause for clause in before), before


def test_leftover_temp_tables_can_be_dropped(databases):
    """An aborted run used to leave its temp table in the destination for good."""
    _exec(DST_DB, "CREATE TABLE apx_rest_1 (id INT PRIMARY KEY)")

    ok, dropped, error = asyncio.run(
        _service().mysql_drop_tables(_profile("local", DST_DB), ["apx_rest_1", "gibt_es_nicht"])
    )
    assert ok, error
    assert dropped == 1

    conn = pymysql.connect(**_conn_args(), database=DST_DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'apx\\_%'")
            assert cur.fetchall() == ()
    finally:
        conn.close()


def test_a_binary_default_is_restored_as_the_same_bytes(databases):
    """The hex clause the parser builds has to be accepted and byte-exact.

    The quoted form in the fixture is what a Shopware server returned for
    category.cms_page_version_id; this server writes hex, so the parser is fed the
    customer's form and only the applying is measured here.
    """
    from sqltransfer_app.transfer import parse_mysql_table_definition

    roh = bytes([0x20, 0xC2, 0xA9, 0x20, 0xE3, 0xA9, 0x6A, 0x4B,
                 0x41, 0xBE, 0x4B, 0xD9, 0xCE, 0x75, 0x2C, 0x34])
    ddl = (
        "CREATE TABLE `kategorie` (\n"
        "  `id` binary(16) NOT NULL,\n"
        "  `cms_page_version_id` binary(16) NOT NULL DEFAULT '" + roh.decode("latin-1") + "',\n"
        "  PRIMARY KEY (`id`)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    before, _after = parse_mysql_table_definition(ddl, codec="latin-1")

    _exec(DST_DB, "CREATE TABLE kategorie (id BINARY(16) NOT NULL PRIMARY KEY, "
                  "cms_page_version_id BINARY(16) NOT NULL)")
    ok, failed, error = asyncio.run(
        _service().apply_definition_clauses(_profile("local", DST_DB), "kategorie", before)
    )
    assert ok, error
    assert failed == []

    # What counts is the value a row gets when it leaves the column out.
    conn = pymysql.connect(**_conn_args(), database=DST_DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO kategorie (id) VALUES (0x0102030405060708090A0B0C0D0E0F10)")
            cur.execute("SELECT HEX(cms_page_version_id) FROM kategorie")
            gespeichert = cur.fetchone()[0]
    finally:
        conn.close()
    assert str(gespeichert).upper() == roh.hex().upper(), gespeichert


def test_an_empty_listing_says_which_schema_it_read(databases):
    """The field overrides the profile, so an empty list has to name where it looked."""
    service = _service()
    ok, tables, message = asyncio.run(
        service.list_tables(_profile("remote", SRC_DB), schema_hint="gibt_es_nicht")
    )
    assert ok, message
    assert tables == []
    assert "gibt_es_nicht" in message and "Schema or database" in message, message

    ok, tables, message = asyncio.run(service.list_tables(_profile("remote", SRC_DB)))
    assert ok and tables, message
    assert SRC_DB in message, message
