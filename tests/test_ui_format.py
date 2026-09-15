from __future__ import annotations

from sqltransfer_app.ui import (
    format_duration,
    format_rows,
    format_started_at,
    infer_log_level,
    truncate,
)


def test_log_level_detects_errors():
    assert infer_log_level("Swap failed for order_line_item") == "ERROR"


def test_log_level_detects_warnings():
    assert infer_log_level("Inbound FK detected, applying fallback") == "WARN"


def test_log_level_defaults_to_info():
    assert infer_log_level("Transferring table: sw6.product") == "INFO"


def test_rows_use_german_thousands_separator():
    assert format_rows(5993606) == "5.993.606"
    assert format_rows(0) == "0"
    assert format_rows(None) == "0"


def test_duration_switches_unit_with_size():
    assert format_duration(420) == "420 ms"
    assert format_duration(4200) == "4.2 s"
    assert format_duration(465028) == "7 min 45 s"
    assert format_duration(7_400_000) == "2 h 3 min"


def test_naive_timestamps_are_read_as_utc():
    # stored by older runs without an offset
    assert format_started_at("2026-08-04T08:45:16").endswith("08:45:16") is False


def test_broken_timestamp_is_passed_through():
    assert format_started_at("not-a-date") == "not-a-date"
    assert format_started_at(None) == "unknown"


def test_truncate_adds_ellipsis_only_when_needed():
    assert truncate("short", 10) == "short"
    assert truncate("a" * 20, 10) == "a" * 9 + "…"
