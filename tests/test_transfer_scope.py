from __future__ import annotations

import pytest

from sqltransfer_app.transfer import TransferService


def _service() -> TransferService:
    return TransferService(storage=None, secret_store=None, tunnel_manager=None)  # type: ignore[arg-type]


def test_preview_scope_table() -> None:
    mode, value, count = _service().preview_scope("table", " public.events ")
    assert mode == "table"
    assert value == "public.events"
    assert count == 1


def test_preview_scope_tables() -> None:
    mode, value, count = _service().preview_scope("tables", "a, b , c")
    assert mode == "tables"
    assert value == "a, b, c"
    assert count == 3


def test_preview_scope_schema() -> None:
    mode, value, count = _service().preview_scope("schema", " public ")
    assert mode == "schema"
    assert value == "public"
    assert count == 0


def test_preview_scope_validation() -> None:
    with pytest.raises(ValueError):
        _service().preview_scope("tables", "  ,  ")
