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
