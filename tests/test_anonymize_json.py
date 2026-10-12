# tests/test_anonymize_json.py
from __future__ import annotations

from sqltransfer_app.anonymize import JsonNode, JsonTarget, Rule, plan_json_column

RULES = [Rule(id=1, pattern="*mail*", kind="email"), Rule(id=2, pattern="ort", kind="city")]


def _node(path, *types):
    return JsonNode(path=path, types=frozenset(types))


def test_a_string_under_a_matching_key_is_rewritten():
    targets, skipped, descend = plan_json_column("custom_fields", [_node(("email",), "STRING")], RULES)
    assert targets == (JsonTarget("custom_fields", ("email",), "email"),)
    assert skipped == () and descend == ()


def test_an_object_is_descended_into_whether_or_not_it_matches():
    targets, skipped, descend = plan_json_column(
        "custom_fields", [_node(("adresse",), "OBJECT"), _node(("mail_data",), "OBJECT")], RULES
    )
    assert targets == ()
    assert descend == (("adresse",), ("mail_data",))
    assert skipped == ()


def test_an_array_is_reported_once_and_not_followed():
    targets, skipped, descend = plan_json_column("custom_fields", [_node(("positions",), "ARRAY")], RULES)
    assert targets == () and descend == ()
    assert skipped == (("custom_fields$.\"positions\"", "array, not followed"),)


def test_a_non_string_value_under_a_matching_key_is_reported():
    _t, skipped, _d = plan_json_column("custom_fields", [_node(("email",), "INTEGER")], RULES)
    assert skipped == (("custom_fields$.\"email\"", "not a string value (INTEGER)"),)


def test_a_key_seen_as_string_and_as_null_is_still_rewritten():
    # A row where the key is JSON null must not disqualify the key everywhere.
    targets, skipped, _d = plan_json_column("custom_fields", [_node(("email",), "STRING", "NULL")], RULES)
    assert targets == (JsonTarget("custom_fields", ("email",), "email"),)
    assert skipped == ()


def test_a_key_seen_as_string_and_as_object_is_rewritten_and_descended():
    targets, _s, descend = plan_json_column("custom_fields", [_node(("daten",), "STRING", "OBJECT")], RULES)
    assert descend == (("daten",),)
    assert targets == ()  # no rule matches "daten"


def test_nested_paths_keep_their_parents():
    nodes = [_node(("adresse", "ort"), "STRING"), _node(("adresse", "nummer"), "INTEGER")]
    targets, skipped, _d = plan_json_column("custom_fields", nodes, RULES)
    assert targets == (JsonTarget("custom_fields", ("adresse", "ort"), "city"),)
    assert skipped == ()  # "nummer" matches no rule, so its type is nobody's business


# --- findings from the branch review ----------------------------------------


def test_a_json_rule_never_applies_to_a_key():
    # The kind that marks a column must not be used as a replacement for a key:
    # it has no replacement expression and crashed the run.
    rules = [Rule(id=1, pattern="*fields*", kind="json")]
    targets, skipped, _descend = plan_json_column("daten", [_node(("extra_fields",), "STRING")], rules)
    assert targets == ()
    assert skipped == ()


def test_a_root_that_is_not_an_object_is_reported():
    targets, skipped, _d = plan_json_column("daten", [_node((), "ARRAY")], RULES)
    assert targets == ()
    assert skipped == (
        ("daten", "the whole value is an array, not an object, so no keys were searched"),
    )


def test_a_node_the_discovery_stopped_at_is_reported():
    _t, skipped, _d = plan_json_column("daten", [_node(("a", "b", "c", "d"), "TOO_DEEP")], RULES)
    assert skipped == (('daten$."a"."b"."c"."d"', "nested deeper than four levels, not searched"),)


def test_a_key_the_database_could_not_return_is_reported():
    _t, skipped, _d = plan_json_column("daten", [_node(("x", "?"), "UNREADABLE")], RULES)
    assert skipped == (('daten$."x"."?"', "key could not be read"),)


def test_a_root_that_is_not_an_object_names_its_type():
    """The message used to say "the whole value is an value", dropping the one fact
    that mattered: what the value actually is. Seen on axro_rma.rma_data with 25088
    rows, where the type decides what the developer has to do next."""
    from sqltransfer_app.anonymize import JsonNode, Rule, plan_json_column

    regeln = [Rule(id=1, pattern="*mail*", kind="email")]
    for typen, erwartet in (
        (frozenset({"STRING"}), "string"),
        (frozenset({"ARRAY"}), "array"),
        (frozenset({"INTEGER"}), "integer"),
        (frozenset({"NULL"}), "null"),
    ):
        _found, skipped, _descend = plan_json_column(
            "rma_data", [JsonNode(path=(), types=typen)], regeln
        )
        assert skipped, typen
        grund = skipped[0][1]
        assert erwartet in grund, f"{typen} -> {grund}"
        assert "an value" not in grund, grund


def test_a_root_of_mixed_types_names_them_all():
    from sqltransfer_app.anonymize import JsonNode, Rule, plan_json_column

    _found, skipped, _descend = plan_json_column(
        "rma_data", [JsonNode(path=(), types=frozenset({"STRING", "INTEGER"}))],
        [Rule(id=1, pattern="*mail*", kind="email")],
    )
    grund = skipped[0][1]
    assert "integer" in grund and "string" in grund, grund


def test_a_driver_that_returns_bytes_is_decoded():
    """MariaDB handed JSON_TYPE back as bytes, and str(b'OBJECT') is "b'OBJECT'".

    The root check then found no OBJECT, bailed out, and the message said "the whole
    value is an value" for a column that holds 25088 perfectly good objects.
    """
    from sqltransfer_app.transfer import json_text_value

    assert json_text_value(b"OBJECT") == "OBJECT"
    assert json_text_value(bytearray(b"ARRAY")) == "ARRAY"
    assert json_text_value("OBJECT") == "OBJECT"
    assert json_text_value(None) == "NULL"
    # A key name with umlauts survives, bytes or not.
    assert json_text_value("straße".encode()) == "straße"


def test_a_truncated_json_type_is_still_understood():
    """MariaDB 10.6.11 truncates JSON_TYPE to four characters under DISTINCT.

    'OBJE' instead of 'OBJECT' made the root check fail and nested objects invisible,
    so a column of 25088 objects was reported as "the whole value is an value". The
    query asks for a CAST now; this is the belt for a server that still cuts.
    """
    from sqltransfer_app.transfer import canonical_json_type

    assert canonical_json_type("OBJE") == "OBJECT"
    assert canonical_json_type("OBJECT") == "OBJECT"
    assert canonical_json_type("ARRA") == "ARRAY"
    assert canonical_json_type("STRI") == "STRING"
    assert canonical_json_type("INTE") == "INTEGER"
    assert canonical_json_type("DOUB") == "DOUBLE"
    assert canonical_json_type("BOOL") == "BOOLEAN"
    assert canonical_json_type("NULL") == "NULL"
    assert canonical_json_type(b"OBJE") == "OBJECT"
    assert canonical_json_type(None) == "NULL"
    # Something nobody expected stays as it is, rather than becoming a wrong type.
    assert canonical_json_type("WHAT") == "WHAT"
