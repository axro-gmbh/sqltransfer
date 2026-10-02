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
