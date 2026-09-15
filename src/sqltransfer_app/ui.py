"""Presentation helpers for the Flet UI.

Only formatting and widget assembly lives here, no transfer logic, so the pieces
below can be unit tested without a database or a running window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable

import flet as ft

MONO = "Menlo"

LEVEL_COLORS: dict[str, str] = {
    "INFO": ft.Colors.ON_SURFACE_VARIANT,
    "WARN": ft.Colors.TERTIARY,
    "ERROR": ft.Colors.ERROR,
}

_ERROR_MARKERS = ("failed", "error", "exception", "blocked", "could not", "no output table")
_WARN_MARKERS = ("deferred", "skip", "fallback", "retry", "warning", "inbound fk")


def infer_log_level(line: str) -> str:
    text = line.lower()
    if any(marker in text for marker in _ERROR_MARKERS):
        return "ERROR"
    if any(marker in text for marker in _WARN_MARKERS):
        return "WARN"
    return "INFO"


def format_rows(rows: int | None) -> str:
    if not rows:
        return "0"
    return f"{rows:,}".replace(",", ".")


def format_duration(elapsed_ms: int | None) -> str:
    ms = int(elapsed_ms or 0)
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def format_started_at(value: str | None) -> str:
    """Render a stored UTC timestamp in local time.

    Older rows were written without an offset, so a bare timestamp is read as UTC.
    """
    if not value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M:%S")


def truncate(text: str | None, limit: int = 160) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def status_tone(status: str | None) -> tuple[str, str, str]:
    """Return (color, on_color, icon) for a run status."""
    if status == "success":
        return ft.Colors.TERTIARY_CONTAINER, ft.Colors.ON_TERTIARY_CONTAINER, ft.Icons.CHECK_CIRCLE_OUTLINE
    if status in {"cancelled", "canceled"}:
        return ft.Colors.SURFACE_CONTAINER_HIGHEST, ft.Colors.ON_SURFACE_VARIANT, ft.Icons.CANCEL_OUTLINED
    return ft.Colors.ERROR_CONTAINER, ft.Colors.ON_ERROR_CONTAINER, ft.Icons.ERROR_OUTLINE


def status_chip(status: str | None) -> ft.Control:
    bgcolor, fgcolor, icon = status_tone(status)
    return ft.Container(
        bgcolor=bgcolor,
        border_radius=20,
        padding=ft.Padding(left=8, right=10, top=3, bottom=3),
        content=ft.Row(
            [ft.Icon(icon, size=14, color=fgcolor), ft.Text(status or "unknown", size=11, color=fgcolor, weight=ft.FontWeight.W_600)],
            spacing=4,
            tight=True,
        ),
    )


def section_card(
    title: str,
    content: ft.Control,
    subtitle: str | None = None,
    icon: ft.IconData | None = None,
    trailing: ft.Control | None = None,
) -> ft.Container:
    heading = ft.Row(
        [
            ft.Icon(icon or ft.Icons.FOLDER_OPEN, size=20, color=ft.Colors.PRIMARY),
            ft.Column(
                [
                    ft.Text(title, size=17, weight=ft.FontWeight.W_600),
                    *([ft.Text(subtitle, color=ft.Colors.ON_SURFACE_VARIANT, size=12)] if subtitle else []),
                ],
                spacing=1,
                tight=True,
                expand=True,
            ),
            *([trailing] if trailing else []),
        ],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    return ft.Container(
        content=ft.Column([heading, ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT), content], tight=True, spacing=12),
        padding=16,
        border_radius=12,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
    )


def field_row(*controls: ft.Control) -> ft.Control:
    """Lay fields out responsively instead of with hard pixel widths."""
    return ft.ResponsiveRow(controls=list(controls), spacing=10, run_spacing=10)


class LogPanel:
    """Auto-scrolling, level-coloured transfer log."""

    def __init__(self, on_copy: Callable[[str], None], height: int = 260) -> None:
        self._lines: list[str] = []
        self._on_copy = on_copy
        self._list = ft.Column(spacing=1, scroll=ft.ScrollMode.AUTO, auto_scroll=True, expand=True)
        self._empty = ft.Text(
            "No output yet. Preview a plan or start a transfer.",
            italic=True,
            color=ft.Colors.ON_SURFACE_VARIANT,
            size=12,
        )
        self._list.controls = [self._empty]
        self.control = ft.Container(
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_radius=10,
            padding=12,
            height=height,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Row(
                                [ft.Icon(ft.Icons.TERMINAL, size=16, color=ft.Colors.ON_SURFACE_VARIANT),
                                 ft.Text("Transfer log", weight=ft.FontWeight.W_600, size=13)],
                                spacing=6,
                            ),
                            ft.Row(
                                [
                                    ft.TextButton("Copy", icon=ft.Icons.COPY_ALL_OUTLINED, on_click=self._copy),
                                    ft.TextButton("Clear", icon=ft.Icons.DELETE_OUTLINE, on_click=self._clear),
                                ],
                                spacing=0,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Container(content=self._list, expand=True),
                ],
                spacing=6,
                expand=True,
            ),
        )

    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    def append(self, line: str, level: str | None = None, stamped: bool = True) -> None:
        final_level = level or infer_log_level(line)
        stamp = datetime.now().strftime("%H:%M:%S")
        rendered = f"[{stamp}] [{final_level}] {line}" if stamped else line
        self._lines.append(rendered)
        if self._empty in self._list.controls:
            self._list.controls = []
        self._list.controls.append(
            ft.Text(
                rendered,
                font_family=MONO,
                size=12,
                selectable=True,
                color=LEVEL_COLORS.get(final_level, ft.Colors.ON_SURFACE_VARIANT),
            )
        )

    def replace(self, text: str, level: str | None = None) -> None:
        self.clear()
        for line in str(text).splitlines():
            if line.strip():
                self.append(line, level)

    def clear(self) -> None:
        self._lines = []
        self._list.controls = [self._empty]

    def _copy(self, _: ft.Event) -> None:
        self._on_copy(self.text)

    def _clear(self, _: ft.Event) -> None:
        self.clear()
        self.control.update()


def run_history_row(record: dict, on_reuse: Callable[[dict], None] | None = None) -> ft.Control:
    scope = record.get("scope_value") or record.get("table_name") or ""
    header = ft.Row(
        [
            status_chip(record.get("status")),
            ft.Text(format_started_at(record.get("started_at")), size=11, color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
            *(
                [ft.IconButton(
                    ft.Icons.REPLAY,
                    icon_size=16,
                    tooltip="Load this scope into the form",
                    on_click=lambda _, r=record: on_reuse(r),
                )]
                if on_reuse
                else []
            ),
        ],
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    route = ft.Row(
        [
            ft.Text(str(record.get("source_name") or "?"), size=13, weight=ft.FontWeight.W_600),
            ft.Icon(ft.Icons.ARROW_FORWARD, size=13, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(str(record.get("destination_name") or "?"), size=13, weight=ft.FontWeight.W_600),
        ],
        spacing=6,
        wrap=True,
    )
    facts = ft.Row(
        [
            _fact(ft.Icons.TABLE_CHART_OUTLINED, f"{record.get('scope_mode') or '?'}: {truncate(scope, 40)}", expand=True),
            _fact(ft.Icons.STORAGE, f"{format_rows(record.get('rows_copied'))} rows"),
            _fact(ft.Icons.TIMER_OUTLINED, format_duration(record.get("elapsed_ms"))),
        ],
        spacing=14,
        wrap=True,
    )
    children: list[ft.Control] = [header, route, facts]
    message = truncate(record.get("message"), 220)
    if message:
        # max_lines alone clips mid-word without a marker; ELLIPSIS puts the marker
        # wherever the line actually ends, whatever the card width turns out to be.
        children.append(
            ft.Text(
                message,
                size=11,
                color=ft.Colors.ON_SURFACE_VARIANT,
                tooltip=record.get("message"),
                max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
        )
    return ft.Container(
        padding=12,
        border_radius=10,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        content=ft.Column(children, spacing=6, tight=True),
    )


def _fact(icon: ft.IconData, text: str, expand: bool = False) -> ft.Control:
    return ft.Row(
        [
            ft.Icon(icon, size=13, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(
                text,
                size=11,
                color=ft.Colors.ON_SURFACE_VARIANT,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
                expand=expand or None,
            ),
        ],
        spacing=4,
        tight=not expand,
    )


def profile_row(title: str, subtitle: str, icon: ft.IconData, on_edit: Callable[[], None], on_delete: Callable[[], None]) -> ft.Control:
    return ft.Container(
        border_radius=8,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        padding=ft.Padding(left=12, right=6, top=4, bottom=4),
        content=ft.Row(
            [
                ft.Icon(icon, size=16, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Column(
                    [
                        ft.Text(title, size=13, weight=ft.FontWeight.W_600),
                        ft.Text(subtitle, size=11, color=ft.Colors.ON_SURFACE_VARIANT, font_family=MONO),
                    ],
                    spacing=0,
                    tight=True,
                    expand=True,
                ),
                ft.IconButton(ft.Icons.EDIT_OUTLINED, icon_size=16, tooltip="Edit", on_click=lambda _: on_edit()),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=16, tooltip="Delete", on_click=lambda _: on_delete()),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def empty_hint(text: str) -> ft.Control:
    return ft.Container(
        padding=12,
        content=ft.Text(text, italic=True, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
    )
