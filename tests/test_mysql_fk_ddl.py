from __future__ import annotations

from sqltransfer_app.transfer import build_mysql_fk_clauses


def row(name, column, position, ref_table, ref_column, ref_schema="shop", update="RESTRICT", delete="RESTRICT"):
    # Shape of one joined key_column_usage / referential_constraints row as queried by the service.
    return (name, column, position, ref_schema, ref_table, ref_column, update, delete)


def test_single_column_key_with_rules():
    clauses, skipped = build_mysql_fk_clauses(
        [row("fk_prod_cat", "category_id", 1, "categories", "id", delete="CASCADE", update="NO ACTION")],
        source_schema="shop",
    )
    assert clauses == {
        "fk_prod_cat": "ADD CONSTRAINT `fk_prod_cat` FOREIGN KEY (`category_id`) "
        "REFERENCES `categories` (`id`) ON DELETE CASCADE ON UPDATE NO ACTION",
    }
    assert skipped == []


def test_composite_key_keeps_column_order():
    rows = [
        row("fk_line_order", "order_version", 2, "orders", "version"),
        row("fk_line_order", "order_id", 1, "orders", "id"),
    ]
    clauses, _ = build_mysql_fk_clauses(rows, source_schema="shop")
    assert clauses["fk_line_order"] == (
        "ADD CONSTRAINT `fk_line_order` FOREIGN KEY (`order_id`, `order_version`) "
        "REFERENCES `orders` (`id`, `version`) ON DELETE RESTRICT ON UPDATE RESTRICT"
    )


def test_reference_into_another_schema_is_skipped_not_guessed():
    # The destination has no copy of the other schema, so pointing the key anywhere would be wrong.
    clauses, skipped = build_mysql_fk_clauses(
        [row("fk_user", "user_id", 1, "users", "id", ref_schema="auth")], source_schema="shop"
    )
    assert clauses == {}
    assert skipped == ["fk_user -> auth.users"]


def test_identifiers_with_backticks_are_escaped():
    clauses, _ = build_mysql_fk_clauses([row("fk`x", "col`a", 1, "ref`t", "id`r")], source_schema="shop")
    assert clauses["fk`x"].startswith("ADD CONSTRAINT `fk``x` FOREIGN KEY (`col``a`) REFERENCES `ref``t` (`id``r`)")
