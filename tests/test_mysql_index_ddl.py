from __future__ import annotations

from sqltransfer_app.transfer import build_mysql_index_clauses


def row(name, non_unique, seq, column, sub_part=None, collation="A", index_type="BTREE", expression=None):
    # Shape of one information_schema.statistics row as queried by the service.
    return (name, non_unique, seq, column, sub_part, collation, index_type, expression)


def test_primary_key_is_left_to_apitap():
    assert build_mysql_index_clauses([row("PRIMARY", 0, 1, "id")]) == {}


def test_unique_composite_index_keeps_column_order():
    rows = [row("idx_cat_name", 1, 2, "name"), row("uniq_sku", 0, 1, "sku"), row("idx_cat_name", 1, 1, "category_id")]
    assert build_mysql_index_clauses(rows) == {
        "idx_cat_name": "ADD INDEX `idx_cat_name` (`category_id`, `name`)",
        "uniq_sku": "ADD UNIQUE INDEX `uniq_sku` (`sku`)",
    }


def test_prefix_length_and_descending_order():
    rows = [row("idx_title", 1, 1, "title", sub_part=191), row("idx_title", 1, 2, "created_at", collation="D")]
    assert build_mysql_index_clauses(rows) == {
        "idx_title": "ADD INDEX `idx_title` (`title`(191), `created_at` DESC)",
    }


def test_fulltext_and_spatial_keep_their_kind():
    rows = [row("ft_body", 1, 1, "body", index_type="FULLTEXT"), row("sp_pos", 1, 1, "pos", index_type="SPATIAL")]
    assert build_mysql_index_clauses(rows) == {
        "ft_body": "ADD FULLTEXT INDEX `ft_body` (`body`)",
        "sp_pos": "ADD SPATIAL INDEX `sp_pos` (`pos`)",
    }


def test_functional_key_part_is_wrapped_in_parentheses():
    rows = [row("idx_lower_mail", 1, 1, None, expression="lower(`email`)")]
    assert build_mysql_index_clauses(rows) == {
        "idx_lower_mail": "ADD INDEX `idx_lower_mail` ((lower(`email`)))",
    }


def test_identifiers_with_backticks_are_escaped():
    assert build_mysql_index_clauses([row("odd`name", 1, 1, "col`x")]) == {
        "odd`name": "ADD INDEX `odd``name` (`col``x`)",
    }


# --- generated columns ------------------------------------------------------


def test_insert_columns_leave_out_generated_columns():
    # MySQL refuses a value for a generated column (error 3105), so "INSERT INTO
    # final SELECT * FROM temp" dies on every Shopware order table.
    from sqltransfer_app.transfer import build_mysql_insert_columns

    final = [("id", False), ("price", False), ("order_date", True), ("amount_total", True)]
    temp = ["id", "price", "order_date", "amount_total"]
    assert build_mysql_insert_columns(final, temp) == ["id", "price"]


def test_insert_columns_only_use_what_both_tables_have():
    from sqltransfer_app.transfer import build_mysql_insert_columns

    final = [("id", False), ("price", False), ("legacy", False)]
    assert build_mysql_insert_columns(final, ["id", "price", "extra"]) == ["id", "price"]


def test_insert_columns_refuse_a_table_with_nothing_to_copy():
    from sqltransfer_app.transfer import build_mysql_insert_columns

    import pytest

    with pytest.raises(ValueError, match="no columns"):
        build_mysql_insert_columns([("order_date", True)], ["order_date"])
