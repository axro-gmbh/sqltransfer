from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import socket
from uuid import uuid4

import flet as ft

from . import __version__, ui
from .models import DBProfile, SSHProfile, TransferResult
from .profiles import (
    DestinationWarning,
    badges,
    destination_warning,
    endpoint_label,
    filter_db_profiles,
    filter_ssh_profiles,
    is_local,
    ssh_label,
)
from .scope import ScopeError, resolve_scope, summarize_notes
from .secrets import SecretStore
from .storage import DuplicateProfileName, ProfileGone, Storage
from .tls import TLS_MODES, TlsError, validate_tls
from .transfer import TransferService
from .tunnel import TunnelManager

SCOPE_CHOICES = {
    "single": ("One table", ft.Icons.TABLE_ROWS_OUTLINED),
    "multi": ("Selected tables", ft.Icons.CHECKLIST),
    "all": ("Whole database", ft.Icons.ALL_INBOX_OUTLINED),
}


HISTORY_LIMIT = 20
HISTORY_COLLAPSED = 5


def _app_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "sqltransfer"


def _new_secret_key(kind: str, profile_name: str, what: str) -> str:
    """A fresh keychain key for a new profile.

    The random part matters after a rename: the old key keeps working for the
    renamed profile, and a later profile that takes the free name gets its own
    entry instead of reading somebody else's password.
    """
    return f"{kind}:{profile_name}:{uuid4().hex[:8]}:{what}"


async def main(page: ft.Page) -> None:
    page.title = "SQL Transfer"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.scroll = ft.ScrollMode.AUTO
    page.window.width = 1240
    page.window.height = 900
    page.window.min_width = 980
    page.window.min_height = 640
    page.padding = 16
    page.spacing = 12

    storage = Storage(_app_data_dir() / "profiles.db")
    secrets = SecretStore()
    transfer_service = TransferService(storage, secrets, TunnelManager())

    # Mutable run state shared between the UI callbacks and the running transfer task.
    run_state = {"running": False, "cancel": False}

    # ------------------------------------------------------------------ feedback

    def notify(message: str, tone: str = "info") -> None:
        palette = {
            "error": (ft.Colors.ERROR_CONTAINER, ft.Colors.ON_ERROR_CONTAINER),
            "ok": (ft.Colors.INVERSE_SURFACE, ft.Colors.ON_INVERSE_SURFACE),
            "info": (ft.Colors.INVERSE_SURFACE, ft.Colors.ON_INVERSE_SURFACE),
        }
        icons = {
            "error": ft.Icons.ERROR_OUTLINE,
            "ok": ft.Icons.CHECK_CIRCLE_OUTLINE,
            "info": ft.Icons.INFO_OUTLINE,
        }
        bgcolor, fgcolor = palette[tone]
        page.show_dialog(
            ft.SnackBar(
                content=ft.Row(
                    [
                        ft.Icon(icons[tone], color=fgcolor, size=18),
                        ft.Text(message, color=fgcolor, expand=True, max_lines=3),
                    ],
                    spacing=8,
                ),
                bgcolor=bgcolor,
                behavior=ft.SnackBarBehavior.FLOATING,
                margin=ft.Margin.all(16),
                shape=ft.RoundedRectangleBorder(radius=10),
                show_close_icon=True,
                close_icon_color=fgcolor,
                duration=7000 if tone == "error" else 3000,
            )
        )

    def notify_error(message: str) -> None:
        notify(message, "error")

    def notify_ok(message: str) -> None:
        notify(message, "ok")

    def confirm(title: str, message: str, confirm_label: str, on_confirm) -> None:
        def close(_: object = None) -> None:
            page.pop_dialog()

        def accept(_: object = None) -> None:
            page.pop_dialog()
            on_confirm()

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(title),
                content=ft.Text(message),
                actions=[
                    ft.TextButton("Cancel", on_click=close),
                    ft.FilledButton(confirm_label, on_click=accept),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )

    async def copy_text_task(text: str) -> None:
        # Clipboard().set is a coroutine; without await nothing reaches the clipboard.
        # page.clipboard is deprecated since Flet 0.80 and goes away in 0.90.
        try:
            await ft.Clipboard().set(text)
            notify_ok("Copied to clipboard")
        except Exception as exc:  # noqa: BLE001
            notify_error(f"Copy failed: {exc}")

    def copy_to_clipboard(text: str) -> None:
        if not text.strip():
            notify_error("Nothing to copy yet")
            return
        page.run_task(copy_text_task, text)

    log = ui.LogPanel(on_copy=copy_to_clipboard, height=280)

    # ------------------------------------------------------------------ controls

    ssh_name = ft.TextField(label="Profile name", col={"sm": 12, "md": 4})
    ssh_host = ft.TextField(label="SSH host", col={"sm": 12, "md": 3})
    ssh_port = ft.TextField(
        label="SSH port",
        value="22",
        input_filter=ft.NumbersOnlyInputFilter(),
        col={"sm": 6, "md": 2},
    )
    ssh_user = ft.TextField(label="SSH username", col={"sm": 6, "md": 3})
    ssh_key = ft.TextField(label="Private key path", col={"sm": 12, "md": 6})
    ssh_passphrase = ft.TextField(
        label="Key passphrase",
        password=True,
        can_reveal_password=True,
        helper="Leave empty to keep the stored one",
        col={"sm": 12, "md": 6},
    )
    ssh_test_button = ft.OutlinedButton("Test SSH connection", icon=ft.Icons.NETWORK_CHECK)
    ssh_save_button = ft.FilledButton("Save", icon=ft.Icons.SAVE_OUTLINED)
    ssh_rows = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)
    ssh_search = ft.TextField(
        label="Search",
        prefix_icon=ft.Icons.SEARCH,
        hint_text="name, host, user",
        expand=True,
        height=44,
        content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
    )
    ssh_new_button = ft.FilledTonalButton("New SSH profile", icon=ft.Icons.ADD)
    ssh_dialog_title = ft.Text("New SSH profile", size=17, weight=ft.FontWeight.BOLD)
    ssh_form_state: dict[str, int | None] = {"id": None}

    db_name = ft.TextField(label="Profile name", col={"sm": 12, "md": 8})
    db_type = ft.Dropdown(
        label="Database",
        value="mysql",
        options=[ft.dropdown.Option("mysql", "MySQL"), ft.dropdown.Option("postgres", "PostgreSQL")],
        col={"sm": 12, "md": 4},
    )
    db_host = ft.TextField(label="DB host", helper="Reachable from the SSH server", col={"sm": 12, "md": 4})
    db_port = ft.TextField(label="DB port", input_filter=ft.NumbersOnlyInputFilter(), col={"sm": 6, "md": 2})
    db_database = ft.TextField(label="Database name", col={"sm": 6, "md": 3})
    db_user = ft.TextField(label="DB username", col={"sm": 6, "md": 3})
    db_password = ft.TextField(
        label="DB password",
        password=True,
        can_reveal_password=True,
        helper="Leave empty to keep the stored one",
        col={"sm": 12, "md": 6},
    )
    db_use_ssh = ft.Checkbox(label="Use SSH tunnel")
    db_ssh_profile = ft.Dropdown(label="SSH profile", col={"sm": 12, "md": 6})
    db_tls_mode = ft.Dropdown(
        label="Encryption",
        value="auto",
        options=[ft.dropdown.Option(key, label) for key, label in TLS_MODES],
        col={"sm": 12, "md": 6},
    )
    db_tls_ca = ft.TextField(
        label="CA certificate (PEM), PostgreSQL only",
        helper="Optional, for an internal certificate authority",
        col={"sm": 12, "md": 6},
    )
    db_save_button = ft.FilledButton("Save", icon=ft.Icons.SAVE_OUTLINED)
    db_test_form_button = ft.OutlinedButton("Test connection", icon=ft.Icons.NETWORK_CHECK)
    db_test_tunnel_button = ft.OutlinedButton("Test through tunnel", icon=ft.Icons.VPN_KEY_OUTLINED)
    db_rows = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)
    db_search = ft.TextField(
        label="Search",
        prefix_icon=ft.Icons.SEARCH,
        hint_text="name, host, database, user",
        expand=True,
        height=44,
        content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
    )
    db_new_button = ft.FilledTonalButton("New database profile", icon=ft.Icons.ADD)
    # Both editors live in a dialog, so "new" and "edit" can never share a state:
    # the form used to keep the last profile's values, and saving under a name
    # that was still in the field overwrote that profile.
    db_dialog_title = ft.Text("New database profile", size=17, weight=ft.FontWeight.BOLD)
    db_form_state: dict[str, int | None] = {"id": None}

    source_profile = ft.Dropdown(label="Source", col={"sm": 12, "md": 5})
    destination_profile = ft.Dropdown(label="Destination", col={"sm": 12, "md": 5})
    destination_hint = ft.Container(visible=False, border_radius=8, padding=10)
    source_test_button = ft.TextButton("Test source", icon=ft.Icons.NETWORK_CHECK)
    destination_test_button = ft.TextButton("Test destination", icon=ft.Icons.NETWORK_CHECK)

    scope_choice = ft.SegmentedButton(
        segments=[
            ft.Segment(value=key, label=ft.Text(label), icon=ft.Icon(icon))
            for key, (label, icon) in SCOPE_CHOICES.items()
        ],
        selected=["single"],
        allow_multiple_selection=False,
        allow_empty_selection=False,
    )

    single_table = ft.Dropdown(
        label="Table",
        editable=True,
        enable_filter=True,
        expand=True,
        hint_text="Load tables or type a name",
        col={"sm": 12, "md": 8},
    )
    all_scope = ft.TextField(
        label="Schema or database",
        helper="Leave empty to use the source profile's own database",
        col={"sm": 12, "md": 6},
    )
    table_checks = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)
    checked_counter = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    load_tables_button = ft.OutlinedButton("Load tables", icon=ft.Icons.REFRESH)
    check_all_button = ft.TextButton("Select all")
    clear_checked_button = ft.TextButton("Clear")

    source_schema_hint = ft.TextField(label="Source schema hint", value="public", col={"sm": 6, "md": 4})
    transfer_parallel = ft.TextField(
        label="Parallel pipes",
        hint_text="auto",
        input_filter=ft.NumbersOnlyInputFilter(),
        helper="1 when tunneled",
        col={"sm": 6, "md": 4},
    )

    preview_button = ft.OutlinedButton("Preview plan", icon=ft.Icons.FACT_CHECK_OUTLINED)
    run_button = ft.FilledButton("Run transfer", icon=ft.Icons.PLAY_ARROW)
    cancel_button = ft.OutlinedButton("Cancel", icon=ft.Icons.STOP_CIRCLE_OUTLINED, visible=False)

    status_icon = ft.Icon(ft.Icons.INFO_OUTLINE, size=16, color=ft.Colors.ON_SURFACE_VARIANT)
    status_text = ft.Text("Idle", size=13, color=ft.Colors.ON_SURFACE_VARIANT, expand=True)
    progress_bar = ft.ProgressBar(value=0, bar_height=4, visible=False)
    progress_label = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT, visible=False)

    runs_list = ft.Column(spacing=8)
    history_state = {"expanded": False}

    busy_controls = [
        run_button,
        preview_button,
        source_test_button,
        destination_test_button,
        load_tables_button,
        scope_choice,
        source_profile,
        destination_profile,
    ]

    # ------------------------------------------------------------------ status

    def set_status(message: str, tone: str = "info") -> None:
        icons = {
            "info": (ft.Icons.INFO_OUTLINE, ft.Colors.ON_SURFACE_VARIANT),
            "busy": (ft.Icons.HOURGLASS_TOP, ft.Colors.PRIMARY),
            "ok": (ft.Icons.CHECK_CIRCLE_OUTLINE, ft.Colors.TERTIARY),
            "error": (ft.Icons.ERROR_OUTLINE, ft.Colors.ERROR),
        }
        icon, color = icons[tone]
        status_icon.icon = icon
        status_icon.color = color
        status_text.value = message
        status_text.color = color if tone in {"ok", "error"} else ft.Colors.ON_SURFACE_VARIANT

    def set_progress(done: int | None = None, total: int | None = None, label: str = "") -> None:
        if done is None or not total:
            progress_bar.visible = bool(label)
            progress_bar.value = None if label else 0
        else:
            progress_bar.visible = True
            progress_bar.value = min(1.0, done / total)
        progress_label.visible = bool(label)
        progress_label.value = label

    def set_running(running: bool) -> None:
        run_state["running"] = running
        for control in busy_controls:
            control.disabled = running
        cancel_button.visible = running
        cancel_button.disabled = False
        cancel_button.text = "Cancel"
        if not running:
            set_progress()

    def on_cancel(_: object) -> None:
        run_state["cancel"] = True
        cancel_button.disabled = True
        cancel_button.text = "Stopping…"
        log.append("Cancel requested, finishing the current table first", "WARN")
        page.update()

    # ------------------------------------------------------------------ refresh

    def _list_height(shown: int, rows_until_scroll: int, row_height: int) -> int | None:
        # Only cap the height once the list would push everything else off screen.
        return row_height * rows_until_scroll if shown > rows_until_scroll else None

    def refresh_ssh_options() -> None:
        profiles = storage.list_ssh_profiles()
        db_ssh_profile.options = [ft.dropdown.Option(str(p.id), p.name) for p in profiles]
        shown = filter_ssh_profiles(profiles, ssh_search.value)
        ssh_rows.height = _list_height(len(shown), 4, 56)
        ssh_rows.controls = [
            ui.profile_row(
                p.name,
                f"{p.username}@{p.host}:{p.port}",
                ft.Icons.LOCK_OUTLINE,
                on_edit=lambda pid=p.id: open_ssh_dialog(pid),
                on_delete=lambda pid=p.id, name=p.name: ask_delete_ssh(pid, name),
            )
            for p in shown
        ] or [ui.empty_hint("No SSH profiles yet." if not profiles else "No SSH profile matches the search.")]

    def refresh_db_options() -> None:
        profiles = storage.list_db_profiles()
        options = [ft.dropdown.Option(str(p.id), p.name) for p in profiles]
        # Every profile can be either end now; what protects the remote ones is the
        # warning below the destination and the question before the run.
        source_profile.options = options
        destination_profile.options = list(options)
        known = {str(p.id) for p in profiles}
        for dropdown in (source_profile, destination_profile):
            if dropdown.value not in known:
                dropdown.value = None
        shown = filter_db_profiles(profiles, db_search.value)
        db_rows.height = _list_height(len(shown), 5, 56)
        db_rows.controls = [
            ui.profile_row(
                p.name,
                endpoint_label(p),
                ft.Icons.STORAGE,
                on_edit=lambda pid=p.id: open_db_dialog(pid),
                on_delete=lambda pid=p.id, name=p.name: ask_delete_db(pid, name),
                marks=badges(p),
            )
            for p in shown
        ] or [ui.empty_hint("No database profiles yet." if not profiles else "No profile matches the search.")]
        refresh_destination_hint()

    def refresh_destination_hint() -> None:
        """Say it before the run starts: this destination is not on this machine."""
        warning = selected_destination_warning()
        destination_hint.visible = warning is not None
        if warning is None:
            return
        lines = [f"Destination is {warning.reason}: {warning.target}"]
        if warning.tunnel:
            lines.append(f"through {warning.tunnel}")
        lines.append("Its tables are replaced. You will be asked once more before the transfer starts.")
        destination_hint.bgcolor = ft.Colors.ERROR_CONTAINER
        destination_hint.content = ft.Row(
            [
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=18, color=ft.Colors.ON_ERROR_CONTAINER),
                ft.Column(
                    [ft.Text(line, size=12, color=ft.Colors.ON_ERROR_CONTAINER) for line in lines],
                    spacing=2,
                    tight=True,
                    expand=True,
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

    def selected_destination_warning() -> DestinationWarning | None:
        if not destination_profile.value:
            return None
        profile = storage.get_db_profile(int(destination_profile.value))
        if profile is None:
            return None
        tunnel = storage.get_ssh_profile(profile.ssh_profile_id) if profile.ssh_profile_id else None
        return destination_warning(profile, tunnel)

    def refresh_run_history() -> None:
        rows = [dict(row) for row in storage.list_recent_runs(limit=HISTORY_LIMIT)]
        if not rows:
            runs_list.controls = [ui.empty_hint("No transfers yet.")]
            return
        expanded = history_state["expanded"]
        shown = rows if expanded else rows[:HISTORY_COLLAPSED]
        controls: list[ft.Control] = [ui.run_history_row(row, on_reuse=reuse_run) for row in shown]
        hidden = len(rows) - HISTORY_COLLAPSED
        if hidden > 0:
            controls.append(
                ft.TextButton(
                    "Show less" if expanded else f"Show {hidden} more",
                    icon=ft.Icons.EXPAND_LESS if expanded else ft.Icons.EXPAND_MORE,
                    on_click=toggle_history,
                )
            )
        runs_list.controls = controls

    def toggle_history(_: object) -> None:
        # Kept across refreshes, so a finished run does not collapse the list under you.
        history_state["expanded"] = not history_state["expanded"]
        refresh_run_history()
        page.update()

    def reuse_run(record: dict) -> None:
        mode = record.get("scope_mode")
        value = record.get("scope_value") or record.get("table_name") or ""
        if mode == "table":
            scope_choice.selected = ["single"]
            single_table.value = value
        elif mode == "tables":
            scope_choice.selected = ["multi"]
            wanted = {part.strip() for part in value.split(",") if part.strip()}
            for control in table_checks.controls:
                if isinstance(control, ft.Checkbox):
                    control.value = control.label in wanted
        else:
            scope_choice.selected = ["all"]
            all_scope.value = value
        refresh_scope_ui()
        notify_ok("Scope loaded from history")
        page.update()

    # ------------------------------------------------------------------ profiles

    def clear_errors(*fields: ft.TextField) -> None:
        for field in fields:
            field.error = None

    def require(*pairs: tuple[ft.TextField, str]) -> bool:
        missing = []
        for field, label in pairs:
            if not (field.value or "").strip():
                field.error = "Required"
                missing.append(label)
            else:
                field.error = None
        if missing:
            notify_error("Missing: " + ", ".join(missing))
            page.update()
            return False
        return True

    def open_ssh_dialog(profile_id: int | None = None) -> None:
        profile = storage.get_ssh_profile(profile_id) if profile_id else None
        if profile_id and profile is None:
            notify_error("SSH profile no longer exists")
            refresh_ssh_options()
            page.update()
            return
        clear_errors(ssh_name, ssh_host, ssh_user, ssh_key)
        ssh_form_state["id"] = profile.id if profile else None
        ssh_dialog_title.value = f"Edit SSH profile '{profile.name}'" if profile else "New SSH profile"
        ssh_name.value = profile.name if profile else ""
        ssh_host.value = profile.host if profile else ""
        ssh_port.value = str(profile.port) if profile else "22"
        ssh_user.value = profile.username if profile else ""
        ssh_key.value = profile.private_key_path if profile else ""
        ssh_passphrase.value = ""
        ssh_passphrase.helper = "Leave empty to keep the stored one" if profile else "Only for an encrypted key"
        page.show_dialog(build_ssh_dialog())
        page.update()

    def ask_delete_ssh(profile_id: int, name: str) -> None:
        def do_delete() -> None:
            profile = storage.get_ssh_profile(profile_id)
            if profile:
                secrets.delete_secret(profile.passphrase_secret_key)
                storage.delete_ssh_profile(profile_id)
            refresh_ssh_options()
            refresh_db_options()
            notify_ok(f"Deleted SSH profile '{name}'")
            page.update()

        confirm(
            "Delete SSH profile?",
            f"'{name}' and its keychain passphrase will be removed. Database profiles using it will lose their tunnel.",
            "Delete",
            do_delete,
        )

    def open_db_dialog(profile_id: int | None = None) -> None:
        profile = storage.get_db_profile(profile_id) if profile_id else None
        if profile_id and profile is None:
            notify_error("DB profile no longer exists")
            refresh_db_options()
            page.update()
            return
        clear_errors(db_name, db_host, db_database, db_user)
        db_ssh_profile.error_text = None
        db_form_state["id"] = profile.id if profile else None
        db_dialog_title.value = f"Edit database profile '{profile.name}'" if profile else "New database profile"
        db_name.value = profile.name if profile else ""
        db_type.value = profile.db_type if profile else "mysql"
        db_host.value = profile.host if profile else ""
        db_port.value = str(profile.port) if profile else ""
        db_database.value = profile.database if profile else ""
        db_user.value = profile.username if profile else ""
        db_password.value = ""
        db_password.helper = "Leave empty to keep the stored one" if profile else "Stored in the macOS Keychain"
        db_use_ssh.value = bool(profile.use_ssh) if profile else False
        db_ssh_profile.value = str(profile.ssh_profile_id) if (profile and profile.ssh_profile_id) else None
        db_tls_mode.value = (profile.tls_mode or "auto") if profile else "auto"
        db_tls_ca.value = (profile.tls_ca_path or "") if profile else ""
        db_tls_ca.error = None
        page.show_dialog(build_db_dialog())
        page.update()

    def ask_delete_db(profile_id: int, name: str) -> None:
        def do_delete() -> None:
            profile = storage.get_db_profile(profile_id)
            if profile:
                secrets.delete_secret(profile.password_secret_key)
                storage.delete_db_profile(profile_id)
            refresh_db_options()
            notify_ok(f"Deleted database profile '{name}'")
            page.update()

        confirm(
            "Delete database profile?",
            f"'{name}' and its keychain password will be removed.",
            "Delete",
            do_delete,
        )

    def save_ssh_profile(_: object) -> None:
        if not require((ssh_name, "profile name"), (ssh_host, "host"), (ssh_user, "username"), (ssh_key, "key path")):
            return
        try:
            profile_name = ssh_name.value.strip()
            editing = storage.get_ssh_profile(ssh_form_state["id"]) if ssh_form_state["id"] else None
            passphrase_key = editing.passphrase_secret_key if editing else None
            if ssh_passphrase.value:
                passphrase_key = passphrase_key or _new_secret_key("ssh", profile_name, "passphrase")
                secrets.set_secret(passphrase_key, ssh_passphrase.value)

            storage.save_ssh_profile(
                SSHProfile(
                    id=ssh_form_state["id"],
                    name=profile_name,
                    host=ssh_host.value.strip(),
                    port=int(ssh_port.value or "22"),
                    username=ssh_user.value.strip(),
                    private_key_path=ssh_key.value.strip(),
                    passphrase_secret_key=passphrase_key,
                )
            )
            page.pop_dialog()
            refresh_ssh_options()
            refresh_db_options()
            notify_ok(f"Saved SSH profile '{profile_name}'")
            page.update()
        except DuplicateProfileName as exc:
            ssh_name.error = str(exc)
            notify_error(str(exc))
            page.update()
        except ProfileGone:
            notify_error("That SSH profile was deleted meanwhile")
            page.pop_dialog()
            refresh_ssh_options()
            page.update()
        except Exception as exc:  # noqa: BLE001
            notify_error(f"SSH save failed: {exc}")

    def save_db_profile(_: object) -> None:
        if not require((db_name, "profile name"), (db_host, "host"), (db_database, "database"), (db_user, "username")):
            return
        if db_use_ssh.value and not db_ssh_profile.value:
            db_ssh_profile.error_text = "Pick an SSH profile"
            notify_error("Select an SSH profile when the tunnel is enabled")
            page.update()
            return
        db_ssh_profile.error_text = None
        if not tls_form_ok():
            return
        try:
            profile_name = db_name.value.strip()
            editing = storage.get_db_profile(db_form_state["id"]) if db_form_state["id"] else None
            password_secret_key = editing.password_secret_key if editing else None
            if db_password.value:
                password_secret_key = password_secret_key or _new_secret_key("db", profile_name, "password")
                secrets.set_secret(password_secret_key, db_password.value)

            storage.save_db_profile(
                DBProfile(
                    id=db_form_state["id"],
                    name=profile_name,
                    db_type=db_type.value,
                    host=db_host.value.strip(),
                    port=int(db_port.value or ("5432" if db_type.value == "postgres" else "3306")),
                    database=db_database.value.strip(),
                    username=db_user.value.strip(),
                    password_secret_key=password_secret_key,
                    use_ssh=bool(db_use_ssh.value),
                    ssh_profile_id=int(db_ssh_profile.value) if (db_use_ssh.value and db_ssh_profile.value) else None,
                    tls_mode=db_tls_mode.value or "auto",
                    tls_ca_path=form_ca_path(),
                )
            )
            page.pop_dialog()
            refresh_db_options()
            notify_ok(f"Saved database profile '{profile_name}'")
            page.update()
        except DuplicateProfileName as exc:
            db_name.error = str(exc)
            notify_error(str(exc))
            page.update()
        except ProfileGone:
            notify_error("That database profile was deleted meanwhile")
            page.pop_dialog()
            refresh_db_options()
            page.update()
        except Exception as exc:  # noqa: BLE001
            notify_error(f"DB save failed: {exc}")

    # ------------------------------------------------------------------ tests

    def form_ca_path() -> str | None:
        value = (db_tls_ca.value or "").strip()
        return str(Path(value).expanduser()) if value else None

    def tls_form_ok() -> bool:
        ca_path = form_ca_path()
        try:
            validate_tls(db_type.value, db_tls_mode.value, ca_path)
            if ca_path and not Path(ca_path).is_file():
                raise TlsError(f"CA certificate not found: {ca_path}")
        except TlsError as exc:
            db_tls_ca.error = str(exc)
            notify_error(str(exc))
            page.update()
            return False
        db_tls_ca.error = None
        return True

    def _form_db_profile() -> DBProfile:
        name = db_name.value.strip() or "unsaved-profile"
        # Testing a saved profile leaves the password field empty (it lives in the
        # keychain); a real login needs it, so carry the stored secret over.
        existing = storage.get_db_profile(db_form_state["id"]) if db_form_state["id"] else None
        return DBProfile(
            id=None,
            name=name,
            db_type=db_type.value,
            host=db_host.value.strip(),
            port=int(db_port.value or ("5432" if db_type.value == "postgres" else "3306")),
            database=db_database.value.strip(),
            username=db_user.value.strip(),
            use_ssh=bool(db_use_ssh.value),
            ssh_profile_id=int(db_ssh_profile.value) if db_ssh_profile.value else None,
            password_secret_key=existing.password_secret_key if existing else None,
            tls_mode=db_tls_mode.value or "auto",
            tls_ca_path=form_ca_path(),
        )

    async def test_db_form_task() -> None:
        if not require((db_name, "profile name"), (db_host, "host"), (db_database, "database"), (db_user, "username")):
            return
        if not tls_form_ok():
            return
        ssh_profile = None
        if db_use_ssh.value:
            if not db_ssh_profile.value:
                notify_error("Select an SSH profile when the tunnel is enabled")
                return
            ssh_profile = storage.get_ssh_profile(int(db_ssh_profile.value))
            if not ssh_profile:
                notify_error("Selected SSH profile no longer exists")
                return

        db_test_form_button.disabled = True
        set_status("Testing database connection…", "busy")
        set_progress(label="Connecting")
        page.update()
        ok, message = await transfer_service.test_profile_connection_from_values(
            _form_db_profile(),
            password=db_password.value.strip() or None,
            ssh_profile=ssh_profile,
        )
        set_status(f"Database test: {'ok' if ok else 'failed'}", "ok" if ok else "error")
        set_progress()
        log.replace(message)
        notify(message, "ok" if ok else "error")
        db_test_form_button.disabled = False
        page.update()

    async def test_db_tunnel_task() -> None:
        if not require((db_name, "profile name"), (db_host, "host"), (db_database, "database"), (db_user, "username")):
            return
        if not tls_form_ok():
            return
        if not db_use_ssh.value:
            notify_error("Enable 'Use SSH tunnel' first")
            return
        if not db_ssh_profile.value:
            notify_error("Select an SSH profile when the tunnel is enabled")
            return
        ssh_profile = storage.get_ssh_profile(int(db_ssh_profile.value))
        if not ssh_profile:
            notify_error("Selected SSH profile no longer exists")
            return

        db_test_tunnel_button.disabled = True
        set_status("Testing SSH tunnel to the database…", "busy")
        set_progress(label="Opening tunnel")
        page.update()
        profile = _form_db_profile()
        profile.use_ssh = True
        ok, message = await transfer_service.test_profile_connection_from_values(
            profile,
            password=db_password.value.strip() or None,
            ssh_profile=ssh_profile,
        )
        set_status(f"Tunnel test: {'ok' if ok else 'failed'}", "ok" if ok else "error")
        set_progress()
        log.replace(message)
        notify(message, "ok" if ok else "error")
        db_test_tunnel_button.disabled = False
        page.update()

    async def test_ssh_form_task() -> None:
        if not require((ssh_name, "profile name"), (ssh_host, "host"), (ssh_user, "username"), (ssh_key, "key path")):
            return
        key_path = Path(ssh_key.value.strip()).expanduser()
        if not key_path.exists():
            ssh_key.error = "File not found"
            set_status("SSH test: failed", "error")
            log.replace(f"Missing SSH key file: {key_path}")
            notify_error(f"SSH key file not found: {key_path}")
            page.update()
            return
        ssh_key.error = None

        ssh_test_button.disabled = True
        set_status("Testing SSH connection…", "busy")
        set_progress(label="Authenticating")
        log.replace("Checking SSH host reachability and key-based auth")
        page.update()

        host = ssh_host.value.strip()
        port = int(ssh_port.value or "22")
        username = ssh_user.value.strip()
        passphrase = ssh_passphrase.value.strip() or None

        try:
            await asyncio.to_thread(socket.create_connection, (host, port), 4.0)
            tm = TunnelManager()
            probe = tm.open_tunnel(
                SSHProfile(
                    id=None,
                    name=ssh_name.value.strip() or "probe",
                    host=host,
                    port=port,
                    username=username,
                    private_key_path=str(key_path),
                    passphrase_secret_key=None,
                ),
                remote_host="127.0.0.1",
                remote_port=22,
                passphrase=passphrase,
            )
            tm.close_tunnel(probe)
            set_status("SSH test: ok", "ok")
            log.append(f"SSH auth succeeded for {username}@{host}:{port}")
            notify_ok("SSH connection successful")
        except Exception as exc:  # noqa: BLE001
            set_status("SSH test: failed", "error")
            log.append(f"SSH test failed: {exc}", "ERROR")
            notify_error(f"SSH test failed: {exc}")
        finally:
            ssh_test_button.disabled = False
            set_progress()
            page.update()

    async def run_connection_test(profile_control: ft.Dropdown, label: str) -> None:
        if not profile_control.value:
            notify_error(f"Pick a {label} profile first")
            return
        profile = storage.get_db_profile(int(profile_control.value))
        if not profile:
            notify_error(f"Selected {label} profile no longer exists")
            return
        set_running(True)
        cancel_button.visible = False
        set_status(f"Testing {label} connection…", "busy")
        set_progress(label=f"Connecting to {profile.name}")
        page.update()
        ok, message = await transfer_service.test_profile_connection(profile)
        set_status(f"{label.capitalize()} test: {'ok' if ok else 'failed'}", "ok" if ok else "error")
        log.replace(message)
        notify(message, "ok" if ok else "error")
        set_running(False)
        page.update()

    # ------------------------------------------------------------------ tables

    def checked_tables() -> list[str]:
        return [
            control.label
            for control in table_checks.controls
            if isinstance(control, ft.Checkbox) and control.value and control.label
        ]

    def update_checked_counter() -> None:
        count = len(checked_tables())
        total = sum(1 for c in table_checks.controls if isinstance(c, ft.Checkbox))
        checked_counter.value = f"{count} of {total} selected" if total else ""
        if not total:
            table_checks.controls = [ui.empty_hint("Pick a source profile, then press 'Load tables'.")]
            table_checks.height = None
        else:
            table_checks.height = 190
        check_all_button.disabled = not total
        clear_checked_button.disabled = not total

    def on_check_change(_: object) -> None:
        update_checked_counter()
        page.update()

    def set_all_checks(value: bool) -> None:
        found = False
        for control in table_checks.controls:
            if isinstance(control, ft.Checkbox):
                control.value = value
                found = True
        if not found:
            notify_error("Load the source tables first")
            return
        update_checked_counter()
        page.update()

    async def load_source_tables_task() -> None:
        if not source_profile.value:
            notify_error("Pick a source profile first")
            return
        src = storage.get_db_profile(int(source_profile.value))
        if src is None:
            notify_error("Selected source profile no longer exists")
            return
        set_running(True)
        cancel_button.visible = False
        set_status("Loading source tables…", "busy")
        set_progress(label=f"Reading metadata from {src.name}")
        page.update()

        schema_hint = source_schema_hint.value.strip() if source_schema_hint.value else None
        ok, tables, message = await transfer_service.list_tables(src, schema_hint=schema_hint)
        if ok:
            single_table.options = [ft.dropdown.Option(name) for name in tables]
            table_checks.controls = [
                ft.Checkbox(label=name, value=False, on_change=on_check_change) for name in tables
            ]
            set_status(f"{len(tables)} tables loaded", "ok")
            log.append(message)
        else:
            single_table.options = []
            single_table.value = None
            table_checks.controls = []
            set_status("Loading tables failed", "error")
            log.append(message, "ERROR")
            notify_error(message)
        update_checked_counter()
        set_running(False)
        page.update()

    # ------------------------------------------------------------------ scope

    def current_choice() -> str:
        return next(iter(scope_choice.selected), "single")

    def refresh_scope_ui() -> None:
        choice = current_choice()
        single_row.visible = choice == "single"
        multi_column.visible = choice == "multi"
        all_row.visible = choice == "all"
        update_checked_counter()

    def on_scope_change(_: object) -> None:
        refresh_scope_ui()
        page.update()

    def resolve_current_scope(src: DBProfile) -> tuple[str, str]:
        return resolve_scope(
            current_choice(),
            single_table=single_table.value,
            multi_tables=checked_tables(),
            all_scope=all_scope.value,
            db_type=src.db_type,
            database=src.database,
            schema_hint=source_schema_hint.value,
        )

    def selected_endpoints() -> tuple[DBProfile, DBProfile] | None:
        if not source_profile.value or not destination_profile.value:
            notify_error("Pick a source and a destination profile")
            set_status("Pick source and destination first", "error")
            page.update()
            return None
        src = storage.get_db_profile(int(source_profile.value))
        dst = storage.get_db_profile(int(destination_profile.value))
        if src is None or dst is None:
            notify_error("A selected profile no longer exists")
            set_status("Selected profile no longer exists", "error")
            refresh_db_options()
            page.update()
            return None
        if src.id == dst.id:
            notify_error("Source and destination are the same profile")
            set_status("Source and destination are the same profile", "error")
            page.update()
            return None
        return src, dst

    async def ask_confirmation(title: str, lines: list[str], confirm_label: str) -> bool:
        """A modal question inside a running task: the answer is awaited, not guessed."""
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def settle(value: bool) -> None:
            page.pop_dialog()
            if not answer.done():
                answer.set_result(value)

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Row(
                    [ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.ERROR), ft.Text(title)],
                    spacing=8,
                ),
                content=ft.Column(
                    [ft.Text(line, size=13) for line in lines],
                    spacing=6,
                    tight=True,
                    width=460,
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: settle(False)),
                    ft.FilledButton(
                        confirm_label,
                        icon=ft.Icons.WARNING_AMBER_ROUNDED,
                        style=ft.ButtonStyle(bgcolor=ft.Colors.ERROR, color=ft.Colors.ON_ERROR),
                        on_click=lambda _: settle(True),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
                on_dismiss=lambda _: settle(False),
            )
        )
        page.update()
        return await answer

    async def destination_confirmed(dst: DBProfile, mode: str, scope: str) -> bool:
        """Ask before writing anywhere that is not this machine. Local stays quiet."""
        tunnel = storage.get_ssh_profile(dst.ssh_profile_id) if dst.ssh_profile_id else None
        warning = destination_warning(dst, tunnel)
        if warning is None:
            return True
        _mode, value, count = transfer_service.preview_scope(mode, scope)
        what = {
            "schema": f"Every table in '{value}' is replaced.",
            "tables": f"{count} tables are replaced: {ui.truncate(value, 120)}",
        }.get(_mode, f"The table '{value}' is replaced.")
        lines = [
            f"'{dst.name}' is {warning.reason}, not a database on this machine.",
            f"Target: {warning.target}",
        ]
        if warning.tunnel:
            lines.append(f"Tunnel: {warning.tunnel}")
        lines += [what, "Existing data there is lost. This cannot be undone."]
        return await ask_confirmation(warning.headline, lines, f"Overwrite on {warning.host}")

    def parsed_parallel(src: DBProfile, dst: DBProfile) -> int | None:
        raw = (transfer_parallel.value or "").strip()
        if raw:
            value = int(raw)  # digits only, enforced by the input filter
            if value < 1:
                raise ScopeError("Parallel pipes must be 1 or higher.")
            return value
        if src.use_ssh or dst.use_ssh:
            # SSH forwarding can hit channel limits; start conservative unless overridden.
            return 1
        return None

    async def _scope_tables_for_check(src: DBProfile, mode: str, scope: str) -> tuple[bool, list[str], str]:
        if mode == "table":
            return True, [scope], "single table"
        if mode == "tables":
            return True, [part.strip() for part in scope.split(",") if part.strip()], "table list"
        ok, tables, message = await transfer_service.list_tables(src, schema_hint=scope, limit=5000)
        if not ok:
            return False, [], message
        return True, tables, message

    # ------------------------------------------------------------------ preview

    async def preview_plan_task() -> None:
        endpoints = selected_endpoints()
        if not endpoints:
            return
        src, dst = endpoints
        try:
            mode, scope = resolve_current_scope(src)
            parallel = parsed_parallel(src, dst)
        except ScopeError as exc:
            notify_error(str(exc))
            set_status(str(exc), "error")
            page.update()
            return

        set_running(True)
        cancel_button.visible = False
        set_status("Building preview…", "busy")
        set_progress(label="Checking scope")
        page.update()

        parsed_mode, parsed_value, count = transfer_service.preview_scope(mode, scope)

        log.clear()
        log.append(f"Source: {src.name} ({src.db_type}) via {'SSH' if src.use_ssh else 'direct'}")
        log.append(f"Destination: {dst.name} ({dst.db_type}) via {'SSH' if dst.use_ssh else 'direct'}")
        if not is_local(dst):
            log.append(
                f"Destination is not on this machine: {endpoint_label(dst)}. Its tables will be replaced.",
                "WARN",
            )
        if parsed_mode == "tables":
            log.append(f"Scope: {count} tables: {parsed_value}")
        elif parsed_mode == "schema":
            log.append(f"Scope: whole schema/database '{parsed_value}'")
        else:
            log.append(f"Scope: single table '{parsed_value}'")
        log.append(f"Parallel pipes: {parallel if parallel is not None else 'auto'}")
        set_status("Preview ready", "ok")
        set_running(False)
        page.update()

    # ------------------------------------------------------------------ transfer

    async def run_transfer_task() -> None:
        endpoints = selected_endpoints()
        if not endpoints:
            return
        src, dst = endpoints
        try:
            mode, scope = resolve_current_scope(src)
            parallel = parsed_parallel(src, dst)
        except ScopeError as exc:
            notify_error(str(exc))
            set_status(str(exc), "error")
            page.update()
            return

        if not await destination_confirmed(dst, mode, scope):
            set_status("Transfer cancelled: destination not confirmed", "info")
            log.replace("Cancelled before anything was written: the destination is not on this machine.")
            page.update()
            return

        run_state["cancel"] = False
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        set_running(True)
        set_status("Transfer running…", "busy")
        set_progress(label="Opening tunnels")
        log.clear()
        log.append(f"Source profile: {src.name} ({src.db_type})")
        log.append(f"Destination profile: {dst.name} ({dst.db_type})")
        log.append(f"Scope: {mode}={scope}")
        page.update()

        def fail(message: str) -> None:
            set_status("Transfer failed", "error")
            log.append(message, "ERROR")
            notify_error(message)
            set_running(False)
            page.update()

        def store_run(result: TransferResult) -> None:
            storage.insert_transfer_run(
                started_at=started,
                source_profile_id=int(source_profile.value),
                destination_profile_id=int(destination_profile.value),
                table_name=scope,
                scope_mode=mode,
                scope_value=scope,
                status=result.status,
                rows_copied=result.rows,
                elapsed_ms=result.elapsed_ms,
                message=result.message,
            )
            refresh_run_history()

        try:
            if dst.db_type == "mysql":
                scope_ok, scoped_tables, scoped_msg = await _scope_tables_for_check(src, mode, scope)
                if not scope_ok:
                    fail(f"Could not resolve table scope: {scoped_msg}")
                    return

                notes: list[str] = []
                deferred_fk_sql: list[str] = []
                total_rows = 0
                total_elapsed = 0
                used_parallel = parallel or 0
                cancelled = False

                log.append("MySQL destination detected: using per-table temp/finalize flow")

                # apitap creates tables with columns and primary key only; the other
                # indexes come from the source, read once for the whole run.
                source_indexes: dict[str, dict[str, str]] = {}
                if src.db_type == "mysql":
                    idx_ok, source_indexes, idx_msg = await transfer_service.mysql_source_index_clauses(
                        src, scoped_tables
                    )
                    if idx_ok:
                        log.append(f"Read secondary indexes of {len(source_indexes)} source table(s)")
                    else:
                        log.append(
                            f"Could not read source indexes, tables get their primary key only: {idx_msg}", "WARN"
                        )
                else:
                    log.append("Source is not MySQL: tables get their primary key only, no secondary indexes", "WARN")

                # Foreign keys are lost the same way; they are read now and restored
                # after the loop, once every outgoing table has released its key names.
                source_fks: dict[str, dict[str, str]] = {}
                landed_tables: list[str] = []
                if src.db_type == "mysql":
                    fk_ok, source_fks, fk_skipped, fk_msg = await transfer_service.mysql_source_fk_clauses(
                        src, scoped_tables
                    )
                    if fk_ok:
                        log.append(f"Read foreign keys of {len(source_fks)} source table(s)")
                        if fk_skipped:
                            log.append(
                                "Foreign keys into other schemas are not copied: " + ", ".join(fk_skipped), "WARN"
                            )
                    else:
                        log.append(f"Could not read source foreign keys, none will be restored: {fk_msg}", "WARN")

                async def apply_source_indexes(target_table: str, final_name: str) -> None:
                    clauses = source_indexes.get(final_name, {})
                    if not clauses:
                        return
                    log.append(f"Building {len(clauses)} index(es) for {final_name}")
                    page.update()
                    ok, applied, failed, idx_err = await transfer_service.mysql_apply_index_clauses(
                        dst, target_table=target_table, clauses=clauses
                    )
                    if not ok:
                        log.append(f"Indexes for {final_name} could not be applied: {idx_err}", "WARN")
                        notes.append(f"index_failed={final_name}")
                        return
                    if applied:
                        notes.append(f"indexed={final_name}")
                    for failure in failed:
                        log.append(f"Index for {final_name} not created: {failure}", "WARN")
                    if failed:
                        notes.append(f"index_failed={final_name}")

                total = len(scoped_tables)
                notes.append(f"tables_total={total}")
                for index, full_name in enumerate(scoped_tables, start=1):
                    if run_state["cancel"]:
                        cancelled = True
                        log.append(f"Cancelled before table {index} of {total}", "WARN")
                        break

                    final_name = full_name.split(".")[-1].strip('`"')
                    temp_name = transfer_service.build_mysql_temp_name(final_name)
                    set_progress(index - 1, total, f"Table {index} of {total}: {final_name}")
                    log.append(f"Transferring table: {full_name} -> temp {temp_name}")
                    page.update()

                    item_result = await transfer_service.transfer_single_table(
                        src, dst, full_name, dest_table=temp_name, parallel=parallel
                    )
                    if item_result.status != "success":
                        fail(f"Table transfer failed ({final_name}): {item_result.message}")
                        return

                    log.append(
                        f"Transfer result for {final_name}: status={item_result.status}, "
                        f"rows={item_result.rows}, ms={item_result.elapsed_ms}"
                    )

                    temp_exists, temp_err = await transfer_service.mysql_table_exists(dst, temp_name)
                    if temp_err:
                        fail(f"Could not verify temp table for {final_name}: {temp_err}")
                        return

                    if temp_exists:
                        inbound_ok, has_inbound_fk, inbound_msg = await transfer_service.mysql_table_has_inbound_fk(
                            dst, final_name
                        )
                        if not inbound_ok:
                            fail(f"Could not inspect FK dependencies for {final_name}: {inbound_msg}")
                            return

                        if has_inbound_fk:
                            log.append(
                                f"Inbound FK detected for {final_name}; replacing data in place from {temp_name}",
                                "WARN",
                            )
                            replaced, replace_msg = await transfer_service.mysql_replace_final_from_temp(
                                dst, temp_table=temp_name, final_table=final_name
                            )
                            if not replaced:
                                fail(f"In-place replace failed for {final_name}: {replace_msg}")
                                return
                            log.append(f"In-place replace done for {final_name}: {replace_msg}")
                            notes.append(f"inplace={final_name}")
                            # The table stays in place; this repairs indexes an earlier swap removed.
                            await apply_source_indexes(final_name, final_name)
                        else:
                            # Index the temp table before the swap, so the table that gets
                            # published is complete from its first moment.
                            await apply_source_indexes(temp_name, final_name)
                            swapped, swap_msg = await transfer_service.mysql_swap_temp_to_final(
                                dst, temp_table=temp_name, final_table=final_name
                            )
                            if not swapped:
                                fail(f"Swap failed for {final_name}: {swap_msg}")
                                return
                            log.append(f"Swapped {temp_name} into {final_name}")
                            notes.append(f"swapped={final_name}")
                    else:
                        final_exists, final_err = await transfer_service.mysql_table_exists(dst, final_name)
                        if final_err:
                            fail(f"Could not verify final table for {final_name}: {final_err}")
                            return
                        log.append(f"No temp table after transfer; final_exists={final_exists} for {final_name}")
                        if not final_exists:
                            count_ok, source_count, count_err = await transfer_service.source_table_row_count(
                                src, full_name
                            )
                            if count_ok and source_count == 0:
                                created, create_msg, deferred_sql = (
                                    await transfer_service.ensure_empty_mysql_table_from_source(
                                        src, dst, source_table=full_name, final_table=final_name
                                    )
                                )
                                if created:
                                    notes.append(f"empty_source_created={final_name}")
                                    if deferred_sql:
                                        deferred_fk_sql.extend(deferred_sql)
                                        notes.append(f"fk_deferred={final_name}")
                                    log.append(
                                        f"Source table {final_name} is empty -> {create_msg}", "WARN"
                                    )
                                    # The column-only fallback of that path creates no indexes.
                                    await apply_source_indexes(final_name, final_name)
                                    landed_tables.append(final_name)
                                    continue
                                fail(
                                    f"Source table {final_name} is empty and transfer produced no output table. "
                                    f"Automatic empty-table creation failed: {create_msg}"
                                )
                                return
                            detail = (
                                f"Transfer for {final_name} reported success, but neither temp table "
                                f"'{temp_name}' nor final table '{final_name}' exists."
                            )
                            detail += (
                                f" Source row count={source_count}."
                                if count_ok
                                else f" Source row count check failed: {count_err}."
                            )
                            fail(detail)
                            return
                        # apitap published nothing: its 0-row guard leaves an existing
                        # destination untouched, which for a copy means stale rows.
                        # Clear the table once the source is confirmed empty.
                        count_ok, source_count, count_err = await transfer_service.source_table_row_count(
                            src, full_name
                        )
                        if not count_ok:
                            log.append(
                                f"Could not count rows of source {final_name}, destination left unchanged: {count_err}",
                                "WARN",
                            )
                            notes.append(f"fallback_skip_swap={final_name}")
                        elif source_count == 0:
                            emptied, deleted, empty_err = await transfer_service.mysql_empty_table(dst, final_name)
                            if not emptied:
                                fail(f"Source table {final_name} is empty, but clearing the destination failed: {empty_err}")
                                return
                            if deleted:
                                log.append(
                                    f"Source table {final_name} is empty -> removed {deleted} stale row(s) from destination",
                                    "WARN",
                                )
                                notes.append(f"emptied={final_name}")
                            else:
                                log.append(f"Source table {final_name} is empty, destination already empty")
                        else:
                            # The source gained rows after the transfer had read it.
                            log.append(
                                f"Source table {final_name} had no rows during transfer but has {source_count} now; "
                                "destination left unchanged, run this table again",
                                "WARN",
                            )
                            notes.append(f"fallback_skip_swap={final_name}")
                        await apply_source_indexes(final_name, final_name)

                    landed_tables.append(final_name)
                    total_rows += item_result.rows
                    total_elapsed += item_result.elapsed_ms
                    used_parallel = item_result.parallel or used_parallel
                    set_progress(index, total, f"Table {index} of {total}: {final_name}")

                if deferred_fk_sql and not cancelled:
                    log.append(f"Applying {len(deferred_fk_sql)} deferred FK statement(s)", "WARN")
                    set_progress(label="Applying deferred foreign keys")
                    page.update()
                    second_ok, second_failures = await transfer_service.mysql_run_second_pass_sql(dst, deferred_fk_sql)
                    if not second_ok:
                        fail("Second pass FK apply failed:\n" + "\n".join(second_failures[:10]))
                        return
                    notes.append("fk_second_pass=ok")
                    log.append("Deferred foreign keys applied")

                # Restore the foreign keys the swap dropped. Runs after a cancel too:
                # the tables copied so far lost their keys either way. Keys that
                # already exist (in-place tables, the deferred pass above) are skipped.
                fk_todo = {table: source_fks[table] for table in landed_tables if table in source_fks}
                if fk_todo:
                    log.append(f"Restoring foreign keys of {len(fk_todo)} table(s)")
                    set_progress(label="Restoring foreign keys")
                    page.update()
                    fk_ok, fk_applied, fk_failed, fk_err = await transfer_service.mysql_apply_fk_clauses(dst, fk_todo)
                    # summarize_notes counts notes per key, so one note per table / failure.
                    if not fk_ok:
                        log.append(f"Foreign keys could not be restored: {fk_err}", "WARN")
                        notes.append("fk_restore_error=1")
                    else:
                        for table in fk_applied:
                            notes.append(f"fk_restored={table}")
                        for failure in fk_failed:
                            log.append(f"Foreign key not restored: {failure}", "WARN")
                            notes.append(f"fk_restore_failed={failure.split(':', 1)[0]}")

                summary = summarize_notes(notes)
                result = TransferResult(
                    status="cancelled" if cancelled else "success",
                    rows=total_rows,
                    elapsed_ms=total_elapsed,
                    parallel=used_parallel,
                    message=(
                        "Cancelled after " + summary if cancelled else "MySQL per-table finalize flow | " + summary
                    ),
                )
            else:
                log.append(f"Running transfer mode={mode}, scope={scope}")
                set_progress(label=f"Transferring {scope}")
                page.update()
                result = await transfer_service.transfer_scope(src, dst, mode, scope, parallel=parallel)

            store_run(result)
            log.append(f"Summary: {result.message}", "INFO" if result.status != "failed" else "ERROR")
            log.append(
                f"rows={ui.format_rows(result.rows)}, duration={ui.format_duration(result.elapsed_ms)}, "
                f"parallel={result.parallel}, requested={parallel if parallel is not None else 'auto'}"
            )
            if result.status == "success":
                set_status(
                    f"Done: {ui.format_rows(result.rows)} rows in {ui.format_duration(result.elapsed_ms)}", "ok"
                )
                notify_ok("Transfer finished")
            elif result.status == "cancelled":
                set_status("Transfer cancelled", "error")
                notify("Transfer cancelled", "info")
            else:
                set_status("Transfer failed", "error")
                notify_error("Transfer failed")
            set_running(False)
            page.update()
        except Exception as exc:  # noqa: BLE001
            fail(f"Unhandled error: {exc}")

    # ------------------------------------------------------------------ wiring

    def run_task(coro_fn, *args):
        # page.run_task rejects anything that is not a coroutine function itself,
        # so extra arguments are forwarded instead of captured in a lambda.
        def handler(_: object) -> None:
            page.run_task(coro_fn, *args)

        return handler

    run_button.on_click = run_task(run_transfer_task)
    preview_button.on_click = run_task(preview_plan_task)
    load_tables_button.on_click = run_task(load_source_tables_task)
    db_test_form_button.on_click = run_task(test_db_form_task)
    db_test_tunnel_button.on_click = run_task(test_db_tunnel_task)
    ssh_test_button.on_click = run_task(test_ssh_form_task)
    source_test_button.on_click = run_task(run_connection_test, source_profile, "source")
    destination_test_button.on_click = run_task(run_connection_test, destination_profile, "destination")
    cancel_button.on_click = on_cancel
    ssh_save_button.on_click = save_ssh_profile
    db_save_button.on_click = save_db_profile
    ssh_new_button.on_click = lambda _: open_ssh_dialog(None)
    db_new_button.on_click = lambda _: open_db_dialog(None)

    def on_search_change(refresh) -> None:
        refresh()
        page.update()

    ssh_search.on_change = lambda _: on_search_change(refresh_ssh_options)
    db_search.on_change = lambda _: on_search_change(refresh_db_options)
    destination_profile.on_change = lambda _: on_search_change(refresh_destination_hint)
    check_all_button.on_click = lambda _: set_all_checks(True)
    clear_checked_button.on_click = lambda _: set_all_checks(False)
    scope_choice.on_change = on_scope_change

    # ------------------------------------------------------------------ layout

    single_row = ui.field_row(single_table)
    multi_column = ft.Column(
        [
            ft.Row([check_all_button, clear_checked_button, checked_counter], spacing=8),
            ft.Container(
                content=table_checks,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=8,
                padding=ft.Padding(left=8, right=8, top=4, bottom=4),
            ),
        ],
        spacing=6,
    )
    all_row = ui.field_row(all_scope)

    advanced_tile = ft.ExpansionTile(
        title=ft.Text("Advanced", size=13),
        leading=ft.Icons.TUNE,
        dense=True,
        controls=[ft.Container(content=ui.field_row(transfer_parallel, source_schema_hint), padding=8)],
    )

    transfer_section = ui.section_card(
        "Transfer",
        ft.Column(
            [
                ui.field_row(
                    source_profile,
                    ft.Container(
                        content=ft.Icon(ft.Icons.ARROW_FORWARD, color=ft.Colors.ON_SURFACE_VARIANT),
                        alignment=ft.Alignment.CENTER,
                        col={"sm": 12, "md": 2},
                    ),
                    destination_profile,
                ),
                destination_hint,
                ft.Row([source_test_button, destination_test_button, load_tables_button], spacing=8, wrap=True),
                ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),
                ft.Text("What should be transferred?", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                scope_choice,
                single_row,
                multi_column,
                all_row,
                advanced_tile,
                ft.Row([run_button, preview_button, cancel_button], spacing=10),
                ft.Container(
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    border_radius=8,
                    padding=10,
                    content=ft.Column(
                        [
                            ft.Row([status_icon, status_text], spacing=8),
                            progress_bar,
                            progress_label,
                        ],
                        spacing=6,
                        tight=True,
                    ),
                ),
                log.control,
            ],
            spacing=12,
        ),
        "Pick the endpoints, choose what to copy, then run.",
        ft.Icons.SWAP_HORIZ,
    )

    def close_dialog(_: object = None) -> None:
        page.pop_dialog()
        page.update()

    def dialog(title: ft.Text, body: list[ft.Control], save_button: ft.Control) -> ft.AlertDialog:
        """A fresh dialog per opening.

        Reusing one instance breaks: pop_dialog only marks a dialog closed, and it
        leaves the stack when the close animation reports back. Showing the same
        object again before that raises "Dialog is already opened". The fields
        inside are the same controls either way, so no state is lost.
        """
        return ft.AlertDialog(
            modal=True,
            title=title,
            # tight, so the dialog is as tall as its fields: a scrolling column
            # would claim the whole window height and leave the buttons floating
            # at the bottom of a mostly empty sheet.
            content=ft.Container(width=720, content=ft.Column(body, spacing=12, tight=True)),
            actions=[ft.TextButton("Cancel", on_click=close_dialog), save_button],
            actions_alignment=ft.MainAxisAlignment.END,
        )

    def build_ssh_dialog() -> ft.AlertDialog:
        return dialog(
            ssh_dialog_title,
            [
                ui.field_row(ssh_name, ssh_host, ssh_port, ssh_user),
                ui.field_row(ssh_key, ssh_passphrase),
                ft.Row([ssh_test_button], spacing=8, wrap=True),
            ],
            ssh_save_button,
        )

    def build_db_dialog() -> ft.AlertDialog:
        return dialog(
            db_dialog_title,
            [
                ui.field_row(db_name, db_type),
                ui.field_row(db_host, db_port, db_database, db_user),
                ui.field_row(db_password, db_ssh_profile),
                ui.field_row(db_tls_mode, db_tls_ca),
                ft.Text(
                    "Automatic: off for localhost and SSH tunnels (SSH already encrypts), "
                    "encrypted and verified for any other host.",
                    size=11,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
                ft.Row([db_use_ssh, db_test_form_button, db_test_tunnel_button], spacing=8, wrap=True),
            ],
            db_save_button,
        )

    def profile_tile(title: str, subtitle: str, icon: ft.IconData, search: ft.Control, new_button: ft.Control, rows: ft.Control) -> ft.ExpansionTile:
        return ft.ExpansionTile(
            title=ft.Text(title),
            subtitle=ft.Text(subtitle, size=11),
            leading=icon,
            controls=[
                ft.Container(
                    padding=12,
                    content=ft.Column([ft.Row([search, new_button], spacing=8), rows], spacing=10),
                )
            ],
        )

    ssh_tile = profile_tile(
        "SSH profiles", "Jump hosts used for tunneling", ft.Icons.LOCK_OUTLINE, ssh_search, ssh_new_button, ssh_rows
    )
    db_tile = profile_tile(
        "Database profiles", "Every profile can be source or destination", ft.Icons.STORAGE, db_search, db_new_button, db_rows
    )

    profiles_section = ui.section_card(
        "Profiles",
        ft.Column([ssh_tile, db_tile], spacing=6),
        "Open only when you need to add or change a connection.",
        ft.Icons.SETTINGS,
    )

    history_section = ui.section_card(
        "Recent runs",
        runs_list,
        f"The last {HISTORY_LIMIT} transfers.",
        ft.Icons.HISTORY,
    )

    refresh_ssh_options()
    refresh_db_options()
    refresh_run_history()
    refresh_scope_ui()
    set_status("Idle")

    page.add(
        ft.Row(
            [
                ft.Icon(ft.Icons.SYNC_ALT, color=ft.Colors.PRIMARY, size=22),
                ft.Text("SQL Transfer", size=22, weight=ft.FontWeight.BOLD),
                ft.Text(f"v{__version__}", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(
                    "MySQL and Postgres between remote and local, optionally through SSH",
                    size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    expand=True,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        ft.ResponsiveRow(
            controls=[
                ft.Container(
                    content=ft.Column([transfer_section, profiles_section], spacing=14),
                    col={"sm": 12, "md": 7, "lg": 8},
                ),
                ft.Container(
                    content=ft.Column([history_section], spacing=14),
                    col={"sm": 12, "md": 5, "lg": 4},
                ),
            ],
            spacing=14,
            run_spacing=14,
        ),
    )
