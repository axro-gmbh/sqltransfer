from __future__ import annotations

from sqltransfer_app.ui import (
    LogPanel,
    format_duration,
    format_rows,
    format_started_at,
    truncate,
)


def _panel() -> LogPanel:
    return LogPanel(on_copy=lambda _text: None)


def test_a_table_name_cannot_turn_a_line_into_an_error():
    """The level used to be guessed from the text, and a Shopware installation really
    has a table called axro_seo_error_log: every line naming it came out red."""
    log = _panel()
    log.append("Transferring table: axro_seo_error_log -> temp apx_1")
    log.append("Source table axro_seo_error_log is empty, destination already empty")
    log.append("Building 3 index(es) for sales_channel_skip_list")
    assert [line.split("] [")[1].split("]")[0] for line in log.text.splitlines()] == ["INFO"] * 3


def test_a_stated_level_is_kept():
    log = _panel()
    log.append("Swap failed for order_line_item", "ERROR")
    log.append("Check constraint not restored on category: chk_x", "WARN")
    assert [line.split("] [")[1].split("]")[0] for line in log.text.splitlines()] == ["ERROR", "WARN"]


def test_replace_passes_its_level_on():
    log = _panel()
    log.replace("connect: postgres source: server does not support TLS", "ERROR")
    assert "[ERROR]" in log.text


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
