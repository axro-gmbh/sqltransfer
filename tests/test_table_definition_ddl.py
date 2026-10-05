"""Turning a table's own DDL back into ALTER clauses.

The fixtures below are verbatim `SHOW CREATE TABLE` output from MySQL 8, not written
by hand: the first attempt at this feature built clauses from information_schema, whose
text is not re-executable (escaped quotes, unparenthesised function defaults), and the
hand-written fixtures agreed with the bug.
"""

from __future__ import annotations

from sqltransfer_app.transfer import parse_mysql_table_definition

HOSTILE_DDL = """CREATE TABLE `schwierig` (
  `id` int NOT NULL,
  `lauf` bigint unsigned NOT NULL AUTO_INCREMENT,
  `angelegt` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `wort` varchar(20) NOT NULL DEFAULT 'null',
  `klammer` varchar(20) DEFAULT '(none)',
  `coll` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL,
  `zeit` datetime NOT NULL,
  `tag` date GENERATED ALWAYS AS (cast(`zeit` as date)) STORED,
  `mit_text` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin GENERATED ALWAYS AS (concat(_utf8mb4'x-',`coll`)) STORED NOT NULL,
  `virtuell` varchar(40) GENERATED ALWAYS AS (concat(_utf8mb4'v-',`coll`)) VIRTUAL,
  `betrag` decimal(10,2) DEFAULT '1.00',
  `art` enum('a','b') DEFAULT 'a',
  `geprueft` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `lauf` (`lauf`),
  KEY `idx_tag` (`tag`,`betrag`),
  CONSTRAINT `chk_positiv` CHECK ((`geprueft` > 0)),
  CONSTRAINT `chk_wort` CHECK ((`wort` <> _utf8mb4'bad'))
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""

PLAIN_DDL = """CREATE TABLE `schlicht` (
  `id` int NOT NULL,
  `name` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""


def test_a_plain_table_needs_nothing():
    # "DEFAULT NULL" is what a nullable column has anyway; restating it is noise.
    assert parse_mysql_table_definition(PLAIN_DDL) == ([], [])


def test_the_column_line_is_taken_as_the_server_wrote_it():
    before, _after = parse_mysql_table_definition(HOSTILE_DDL)
    assert "MODIFY COLUMN `angelegt` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP" in before
    assert "MODIFY COLUMN `wort` varchar(20) NOT NULL DEFAULT 'null'" in before
    assert "MODIFY COLUMN `klammer` varchar(20) DEFAULT '(none)'" in before
    assert "MODIFY COLUMN `betrag` decimal(10,2) DEFAULT '1.00'" in before
    assert "MODIFY COLUMN `art` enum('a','b') DEFAULT 'a'" in before


def test_a_generated_column_keeps_its_collation_and_not_null():
    before, _after = parse_mysql_table_definition(HOSTILE_DDL)
    assert (
        "MODIFY COLUMN `mit_text` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin "
        "GENERATED ALWAYS AS (concat(_utf8mb4'x-',`coll`)) STORED NOT NULL"
    ) in before


def test_a_virtual_column_is_dropped_and_added_in_its_place():
    # MySQL refuses to turn a plain column into a VIRTUAL one (error 3106), so the
    # column is rebuilt where it stood.
    before, _after = parse_mysql_table_definition(HOSTILE_DDL)
    assert "DROP COLUMN `virtuell`" in before
    assert (
        "ADD COLUMN `virtuell` varchar(40) GENERATED ALWAYS AS (concat(_utf8mb4'v-',`coll`)) "
        "VIRTUAL AFTER `mit_text`"
    ) in before
    positions = [i for i, clause in enumerate(before) if "`virtuell`" in clause]
    assert before[positions[0]].startswith("DROP COLUMN") and before[positions[1]].startswith("ADD COLUMN")


def test_untouched_columns_produce_nothing():
    before, _after = parse_mysql_table_definition(HOSTILE_DDL)
    untouched = ("MODIFY COLUMN `id`", "MODIFY COLUMN `zeit`", "MODIFY COLUMN `coll`")
    assert not [clause for clause in before if clause.startswith(untouched)]


def test_auto_increment_comes_with_its_counter_and_after_the_columns():
    before, _after = parse_mysql_table_definition(HOSTILE_DDL)
    assert "MODIFY COLUMN `lauf` bigint unsigned NOT NULL AUTO_INCREMENT" in before
    assert before[-1] == "AUTO_INCREMENT = 7"


def test_checks_are_separate_because_their_names_are_unique_per_database():
    # Adding them before the swap collides with the original table's constraints.
    _before, after = parse_mysql_table_definition(HOSTILE_DDL)
    assert after == [
        "ADD CONSTRAINT `chk_positiv` CHECK ((`geprueft` > 0))",
        "ADD CONSTRAINT `chk_wort` CHECK ((`wort` <> _utf8mb4'bad'))",
    ]


def test_indexes_and_keys_are_left_to_the_index_step():
    before, after = parse_mysql_table_definition(HOSTILE_DDL)
    assert not [clause for clause in before + after if "KEY" in clause]


# A binary default MySQL writes back as a quoted string of raw bytes, not as hex.
# Seen on a Shopware copy: category.cms_page_version_id, binary(16) NOT NULL, whose
# clause came back as DEFAULT ' © ãéjKÂ¾KÙÎu,4%' and was refused with error 1067,
# because those characters re-encoded are more than the 16 bytes the column holds.
BINARY_DEFAULT_BYTES = bytes([0x20, 0xC2, 0xA9, 0x20, 0xE3, 0xA9, 0x6A, 0x4B,
                              0x41, 0xBE, 0x4B, 0xD9, 0xCE, 0x75, 0x2C, 0x34])
BINARY_DDL = (
    "CREATE TABLE `category` (\n"
    "  `id` binary(16) NOT NULL,\n"
    "  `cms_page_version_id` binary(16) NOT NULL DEFAULT '"
    + BINARY_DEFAULT_BYTES.decode("latin-1")
    + "',\n"
    "  `name` varchar(255) DEFAULT 'Kategorie',\n"
    "  PRIMARY KEY (`id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def test_a_binary_default_becomes_a_hex_literal():
    before, _after = parse_mysql_table_definition(BINARY_DDL, codec="latin-1")
    erwartet = "MODIFY COLUMN `cms_page_version_id` binary(16) NOT NULL DEFAULT 0x" + BINARY_DEFAULT_BYTES.hex().upper()
    assert erwartet in before, before


def test_a_text_default_keeps_its_quotes():
    # Only binary columns get the hex treatment; a string default stays a string.
    before, _after = parse_mysql_table_definition(BINARY_DDL, codec="latin-1")
    assert "MODIFY COLUMN `name` varchar(255) DEFAULT 'Kategorie'" in before, before
