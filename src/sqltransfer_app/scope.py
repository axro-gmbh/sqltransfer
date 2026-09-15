"""Scope resolution for transfers.

The UI asks a single question ("what should be transferred?") and this module turns
the answer into the (mode, value) pair that TransferService understands.
"""

from __future__ import annotations

from typing import Iterable, Literal, Sequence

ScopeChoice = Literal["single", "multi", "all"]
ScopeMode = Literal["table", "tables", "schema"]

CHOICES: tuple[ScopeChoice, ...] = ("single", "multi", "all")


class ScopeError(ValueError):
    """Raised when the chosen scope cannot be turned into a runnable transfer."""


def resolve_scope(
    choice: str,
    *,
    single_table: str | None = None,
    multi_tables: Sequence[str] | None = None,
    all_scope: str | None = None,
    db_type: str | None = None,
    database: str | None = None,
    schema_hint: str | None = None,
) -> tuple[ScopeMode, str]:
    """Return the transfer mode and scope value for the selected choice."""
    if choice == "single":
        table = (single_table or "").strip()
        if not table:
            raise ScopeError("Pick or type the table you want to transfer.")
        return "table", table

    if choice == "multi":
        tables = _unique_in_order(multi_tables or [])
        if not tables:
            raise ScopeError("Select at least one table.")
        return "tables", ", ".join(tables)

    if choice == "all":
        explicit = (all_scope or "").strip()
        if explicit:
            return "schema", explicit
        if (db_type or "").lower() == "mysql":
            database_name = (database or "").strip()
            if not database_name:
                raise ScopeError("The source profile has no database name to copy.")
            return "schema", database_name
        return "schema", (schema_hint or "").strip() or "public"

    raise ScopeError(f"Unknown scope choice: {choice}")


def summarize_notes(notes: Iterable[str]) -> str:
    """Collapse per-table run notes into counts.

    A schema transfer produces one note per table, which is unreadable in the run
    history. Keep the detail in the transfer log and store the shape of the run here.
    """
    totals: dict[str, int] = {}
    passthrough: dict[str, str] = {}
    for note in notes:
        key, _, value = note.partition("=")
        if key in {"tables_total", "fk_second_pass"}:
            passthrough[key] = note
            continue
        totals[key] = totals.get(key, 0) + 1

    parts = [passthrough[key] for key in ("tables_total",) if key in passthrough]
    parts.extend(f"{key}={count}" for key, count in totals.items())
    parts.extend(passthrough[key] for key in ("fk_second_pass",) if key in passthrough)
    return ", ".join(parts)


def _unique_in_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
