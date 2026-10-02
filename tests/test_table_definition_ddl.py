# tests/test_table_definition_ddl.py
from __future__ import annotations

import pytest

from sqltransfer_app.transfer import build_definition_clauses


def _column(name, type_="varchar(20)", default=None, extra="", expression=None, nullable="YES"):
    return {"name": name, "type": type_, "default": default, "extra": extra,
            "generation_expression": expression, "is_nullable": nullable}


def test_a_plain_table_needs_nothing():
    assert build_definition_clauses([_column("name")], [], None, "mysql") == []


def test_a_generated_column_comes_before_a_default():
    clauses = build_definition_clauses(
        [_column("tag", "date", extra="STORED GENERATED", expression="cast(`zeit` as date)"),
         _column("betrag", "decimal(10,2)", default="1.00")],
        [], None, "mysql")
    assert clauses == [
        "MODIFY COLUMN `tag` date GENERATED ALWAYS AS (cast(`zeit` as date)) STORED",
        "ALTER COLUMN `betrag` SET DEFAULT '1.00'",
    ]


def test_a_virtual_generated_column_keeps_being_virtual():
    clauses = build_definition_clauses(
        [_column("k", "varchar(32)", extra="VIRTUAL GENERATED", expression="concat('k-',`id`)")],
        [], None, "mysql")
    assert clauses == ["MODIFY COLUMN `k` varchar(32) GENERATED ALWAYS AS (concat('k-',`id`)) VIRTUAL"]


def test_a_function_default_is_not_quoted():
    clauses = build_definition_clauses(
        [_column("angelegt", "datetime", default="CURRENT_TIMESTAMP", extra="DEFAULT_GENERATED")],
        [], None, "mysql")
    assert clauses == ["ALTER COLUMN `angelegt` SET DEFAULT CURRENT_TIMESTAMP"]


def test_auto_increment_carries_its_counter_and_comes_after_the_defaults():
    clauses = build_definition_clauses(
        [_column("lauf", "bigint unsigned", extra="auto_increment", nullable="NO"),
         _column("betrag", "decimal(10,2)", default="1.00")],
        [], 42, "mysql")
    assert clauses == [
        "ALTER COLUMN `betrag` SET DEFAULT '1.00'",
        "MODIFY COLUMN `lauf` bigint unsigned NOT NULL AUTO_INCREMENT",
        "AUTO_INCREMENT = 42",
    ]


def test_checks_come_last():
    clauses = build_definition_clauses([_column("betrag", "decimal(10,2)", default="1.00")],
                                       [("probe_chk_1", "(`betrag` > 0)")], None, "mysql")
    assert clauses[-1] == "ADD CONSTRAINT `probe_chk_1` CHECK ((`betrag` > 0))"


def test_postgres_takes_defaults_and_checks_only():
    clauses = build_definition_clauses(
        [_column("tag", "date", extra="", expression="(zeit)::date"),
         _column("betrag", "numeric(10,2)", default="1.00")],
        [("probe_chk_1", "(betrag > 0)")], None, "postgres")
    assert clauses == [
        'ALTER COLUMN "betrag" SET DEFAULT \'1.00\'',
        'ADD CONSTRAINT "probe_chk_1" CHECK ((betrag > 0))',
    ]
