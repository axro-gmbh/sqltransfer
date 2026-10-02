"""Click-through driver for the real app: no window, no keychain, temp data dir.

Run it by hand after changing the UI; it is not a pytest test because it drives
the whole app, including its dialogs, against a throwaway data directory:

    .venv314/bin/python tests/driver_app.py

It builds the app the way Flet would, then clicks through the flows that are easy
to break and impossible to see in a unit test: the profile dialogs, the guard in
front of a remote destination, and the rule that a failed anonymization must stop
the swap.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import flet as ft

from sqltransfer_app import app as app_module
from sqltransfer_app.anonymize import Column
from sqltransfer_app.models import TransferResult

FAILURES: list[str] = []
BUTTONS = (ft.Button, ft.TextButton, ft.OutlinedButton, ft.FilledButton, ft.FilledTonalButton)


def check(condition: bool, label: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + label)
    if not condition:
        FAILURES.append(label)


class FakeSecrets:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set_secret(self, key, value):
        self.store[key] = value

    def get_secret(self, key):
        return self.store.get(key)

    def delete_secret(self, key):
        self.store.pop(key, None)

    def anonymization_salt(self):
        return "driver-salt"


class FakeWindow:
    width = height = min_width = min_height = top = 0


class FakePage:
    def __init__(self) -> None:
        self.controls: list = []
        self.dialogs: list = []
        self.seen_dialogs: list = []
        self.snacks: list[str] = []
        self.tasks: list = []
        self.window = FakeWindow()

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self, *_a, **_k):
        pass

    def show_dialog(self, dialog):
        if isinstance(dialog, ft.SnackBar):
            self.snacks.append(text_of(dialog))
        else:
            self.dialogs.append(dialog)
            self.seen_dialogs.append(dialog)

    def pop_dialog(self, *_a):
        if self.dialogs:
            self.dialogs.pop()

    def run_task(self, coro_fn, *args):
        self.tasks.append(asyncio.ensure_future(coro_fn(*args)))

    @property
    def dialog(self):
        return self.dialogs[-1] if self.dialogs else None


def walk(node, seen=None):
    """Every control reachable from a node, whatever it is nested in."""
    seen = seen if seen is not None else set()
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from walk(item, seen)
        return
    if not isinstance(node, ft.Control):
        return
    yield node
    for attr in ("controls", "content", "actions", "title", "subtitle", "leading", "segments"):
        yield from walk(getattr(node, attr, None), seen)


def text_of(node) -> str:
    return " ".join(c.value for c in walk(node) if isinstance(c, ft.Text) and c.value)


def find(root, kind, **match):
    for control in walk(root):
        if isinstance(control, kind) and all(getattr(control, k, None) == v for k, v in match.items()):
            return control
    raise AssertionError(f"not found: {kind.__name__} {match}")


def label_of(control) -> str:
    # Flet 1.0 buttons keep their label in `content`, as a plain string.
    for attr in ("text", "content"):
        value = getattr(control, attr, None)
        if isinstance(value, str):
            return value
    return ""


def button(root, text):
    for control in walk(root):
        if isinstance(control, BUTTONS) and label_of(control) == text:
            return control
    raise AssertionError(f"button not found: {text}")


def click(control) -> None:
    control.on_click(None)


def fire(control, event) -> None:
    """Trigger a handler, refusing an event the control does not actually have.

    Assigning to a name Flet never calls (on_change on a Dropdown, say) looks fine
    from the outside; a fresh instance of the same class tells the truth.
    """
    assert hasattr(type(control)(), event), f"{type(control).__name__} has no {event}"
    handler = getattr(control, event, None)
    assert handler, f"{type(control).__name__}.{event} is not wired"
    handler(None)


def log_text(root) -> str:
    """Everything the transfer log shows: the auto-scrolling column is only there."""
    panel = [c for c in walk(root) if isinstance(c, ft.Column) and c.auto_scroll][0]
    return " ".join(c.value for c in walk(panel) if isinstance(c, ft.Text) and c.value)


def rows_of(column) -> list[str]:
    return [text_of(row) for row in column.controls]


def scrolling_column(tile) -> ft.Column:
    return [c for c in walk(tile) if isinstance(c, ft.Column) and c.scroll][0]


def tile_with(root, icon) -> ft.ExpansionTile:
    return [t for t in walk(root) if isinstance(t, ft.ExpansionTile) and t.leading == icon][0]


def destination_hints(root) -> list[str]:
    """The warning under the destination dropdown, if it is showing.

    Matched by its text, not by its colour: the 'remote' badge on a profile row is
    painted in the same warning colour.
    """
    return [
        text_of(c)
        for c in walk(root)
        if isinstance(c, ft.Container) and c.visible and text_of(c).startswith("Destination is")
    ]


def _async(value):
    """A stand-in for an async service method that always answers the same way."""

    async def call(*_a, **_k):
        return value

    return call


def new_db_profile(page, root, name, host, database, user) -> None:
    click(button(root, "New database profile"))
    dialog = page.dialog
    for label, value in (("Profile name", name), ("DB host", host), ("Database name", database),
                         ("DB username", user)):
        find(dialog, ft.TextField, label=label).value = value
    click(button(dialog, "Save"))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sqlt-driver-"))
    app_module._app_data_dir = lambda: tmp
    app_module.SecretStore = FakeSecrets

    page = FakePage()
    await app_module.main(page)
    root = page.controls

    db_tile = tile_with(root, ft.Icons.STORAGE)
    db_rows = scrolling_column(db_tile)

    print("\n1. A new profile is created through the dialog, and the form keeps nothing")
    new_db_profile(page, root, "local-shop", "127.0.0.1", "shop", "root")
    check(page.dialog is None, "the dialog closes after saving")
    check("local-shop" in rows_of(db_rows)[0] and "local" in rows_of(db_rows)[0], "listed and marked local")
    click(button(root, "New database profile"))
    check(find(page.dialog, ft.TextField, label="Profile name").value == "", "the next new profile starts empty")
    click(button(page.dialog, "Cancel"))

    print("\n2. A repeated name is refused and the existing profile survives")
    click(button(root, "New database profile"))
    dialog = page.dialog
    for label, value in (("Profile name", "local-shop"), ("DB host", "other.example.com"),
                         ("Database name", "x"), ("DB username", "y")):
        find(dialog, ft.TextField, label=label).value = value
    click(button(dialog, "Save"))
    check(page.dialog is not None, "the dialog stays open")
    check("already exists" in (find(dialog, ft.TextField, label="Profile name").error or ""), "the field says why")
    click(button(dialog, "Cancel"))
    check(len(rows_of(db_rows)) == 1 and "127.0.0.1" in rows_of(db_rows)[0], "the first profile is untouched")

    print("\n3. Editing loads the profile and renames it in place")
    click([c for c in walk(db_rows) if isinstance(c, ft.IconButton) and c.tooltip == "Edit"][0])
    dialog = page.dialog
    check(find(dialog, ft.Text, size=17).value == "Edit database profile 'local-shop'", "the title names it")
    find(dialog, ft.TextField, label="Profile name").value = "local-shop-renamed"
    click(button(dialog, "Save"))
    check(len(rows_of(db_rows)) == 1 and "local-shop-renamed" in rows_of(db_rows)[0], "renamed, still one")

    print("\n4. Both ends offer every profile, grouped, local first")
    new_db_profile(page, root, "prod", "db.example.com", "shop", "reader")
    source = find(root, ft.Dropdown, label="Source")
    destination = find(root, ft.Dropdown, label="Destination")
    entries = [(o.text, o.disabled) for o in source.options]
    check(entries == [("On this machine", True), ("local-shop-renamed", False), ("Elsewhere", True), ("prod", False)],
          f"grouped, local first: {entries}")
    check(all(a is not b for a, b in zip(source.options, destination.options)), "each dropdown owns its entries")

    print("\n5. The search narrows the list")
    search = find(db_tile, ft.TextField, label="Search")
    search.value = "example.com"
    fire(search, "on_change")
    check(len(rows_of(db_rows)) == 1 and "prod" in rows_of(db_rows)[0], "only the match is shown")
    search.value = ""
    fire(search, "on_change")

    print("\n6. A remote destination warns, and running asks once more")
    destination.value = next(o.key for o in destination.options if o.text == "prod")
    fire(destination, "on_select")
    hints = destination_hints(root)
    check(len(hints) == 1 and "db.example.com" in hints[0], f"the hint names the host: {hints}")
    source.value = next(o.key for o in source.options if o.text == "local-shop-renamed")
    find(root, ft.Dropdown, label="Table").value = "shop.orders"
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.05)
    dialog = page.dialog
    check(dialog is not None and "Overwrite data on db.example.com" in text_of(dialog), "the question names the host")
    check(any(label_of(b).startswith("Overwrite on") for b in walk(dialog) if isinstance(b, BUTTONS)),
          "the confirm button is explicit")
    click(button(dialog, "Cancel"))
    await asyncio.sleep(0.05)
    status = find(root, ft.Text, size=13, expand=True)
    check("cancelled" in status.value.lower(), f"cancelling writes nothing: {status.value!r}")

    print("\n7. A local destination stays quiet, and a heading is not a profile")
    destination.value = next(o.key for o in destination.options if o.text == "local-shop-renamed")
    fire(destination, "on_select")
    check(not destination_hints(root), "no warning for a database on this machine")
    source.value = next(o.key for o in source.options if o.disabled)
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.05)
    check("Pick a source and a destination" in page.snacks[-1], "a heading counts as nothing chosen")

    print("\n8. Anonymization: the switch and the rule list")
    switch = find(root, ft.Switch, label="Anonymize personal data")
    check(switch.value is True, "the switch is on while rules exist")
    rules_tile = tile_with(root, ft.Icons.PRIVACY_TIP)
    rules_rows = scrolling_column(rules_tile)
    before = len(rows_of(rules_rows))
    check(before > 0, f"the defaults are seeded: {before} rules")
    click(button(root, "New rule"))
    dialog = page.dialog
    find(dialog, ft.TextField, label="Column pattern").value = "kundennummer"
    find(dialog, ft.Dropdown, label="Replace with").value = "text"
    click(button(dialog, "Save"))
    rows = rows_of(rules_rows)
    check(len(rows) == before + 1 and any("kundennummer" in row for row in rows), "the new rule is listed")

    print("\n9. A failed anonymization stops the swap")
    calls: list[str] = []

    async def ok_transfer(*_a, **_k):
        calls.append("transfer")
        return TransferResult(status="success", rows=1, elapsed_ms=1, parallel=1, message="ok")

    async def failing_anonymize(*_a, **_k):
        calls.append("anonymize")
        return False, 0, "Data too long for column 'email'"

    async def never_swap(*_a, **_k):
        calls.append("swap")
        return True, ""

    app_module.TransferService.transfer_single_table = ok_transfer
    app_module.TransferService.anonymize_table = failing_anonymize
    app_module.TransferService.mysql_swap_temp_to_final = never_swap
    app_module.TransferService.mysql_table_exists = _async((True, ""))
    app_module.TransferService.mysql_table_has_inbound_fk = _async((True, False, ""))
    app_module.TransferService.table_columns = _async((True, [Column("email", "varchar")], ""))
    app_module.TransferService.mysql_source_index_clauses = _async((True, {}, ""))
    app_module.TransferService.mysql_source_fk_clauses = _async((True, {}, [], ""))

    source.value = next(o.key for o in source.options if o.text == "prod")
    destination.value = next(o.key for o in destination.options if o.text == "local-shop-renamed")
    fire(destination, "on_select")
    find(root, ft.Dropdown, label="Table").value = "shop.kunde"
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.2)
    check("anonymize" in calls and "swap" not in calls, f"no swap after a failed rewrite: {calls}")
    status = find(root, ft.Text, size=13, expand=True)
    check("failed" in status.value.lower(), f"the run reports failure: {status.value!r}")

    print("\n9b. A run with the switch off says so, and the switch follows the rules")
    app_module.TransferService.anonymize_table = _async((True, 0, ""))
    switch.value = False
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.2)
    check("anonymization is off" in log_text(root).lower(), "the log records that nothing was anonymized")

    rules = [r for r in rows_of(rules_rows)]
    rule_edit = [c for c in walk(rules_rows) if isinstance(c, ft.IconButton) and c.tooltip == "Edit"]
    for item in rule_edit:
        click(item)
        dialog = page.dialog
        find(dialog, ft.Checkbox, label="Enabled").value = False
        click(button(dialog, "Save"))
    check(switch.disabled and switch.value is False, "no enabled rule leaves the switch off and disabled")
    click(rule_edit[0])
    dialog = page.dialog
    find(dialog, ft.Checkbox, label="Enabled").value = True
    click(button(dialog, "Save"))
    check(switch.disabled is False and switch.value is True, f"a rule again: switch back on ({switch.value})")

    print("\n9c. The in-place branch anonymizes before the data reaches the final table")
    order: list[str] = []

    async def record_anonymize(_self, _profile, plan):
        order.append(f"anonymize:{plan.table}")
        return True, 1, ""

    async def record_replace(_self, _dst, temp_table, final_table):
        order.append(f"replace:{temp_table}->{final_table}")
        return True, "1 row"

    app_module.TransferService.anonymize_table = record_anonymize
    app_module.TransferService.mysql_replace_final_from_temp = record_replace
    app_module.TransferService.mysql_table_has_inbound_fk = _async((True, True, ""))
    switch.value = True
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.2)
    status_now = find(root, ft.Text, size=13, expand=True).value
    check(len(order) >= 2 and order[0].startswith("anonymize:") and order[1].startswith("replace:"),
          f"anonymize runs before the in-place replace: {order} (status: {status_now!r}, log tail: {log_text(root)[-160:]!r})")
    check("kunde" not in order[0].split(":")[1] or "tmp" in order[0], f"it rewrites the temp table: {order[0]}")

    print("\n9d. A PostgreSQL transfer that failed halfway says the data is real")
    new_db_profile(page, root, "pg-local", "127.0.0.1", "shop", "postgres")
    pg_edit = [c for c in walk(db_rows) if isinstance(c, ft.IconButton) and c.tooltip == "Edit"]
    click(pg_edit[[r.split()[0] for r in rows_of(db_rows)].index("pg-local")])
    find(page.dialog, ft.Dropdown, label="Database").value = "postgres"
    click(button(page.dialog, "Save"))

    async def half_failed(*_a, **_k):
        return TransferResult(status="failed", rows=7, elapsed_ms=5, parallel=1, message="table 2 of 3 failed")

    app_module.TransferService.transfer_scope = half_failed
    destination.value = next(o.key for o in destination.options if o.text == "pg-local")
    fire(destination, "on_select")
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.2)
    log = log_text(root).lower()
    check("not anonymized" in log or "still real" in log, f"the log warns about the real data: {log[-200:]}")

    print("\n9e. A swapped table warns about the generated columns it loses")
    app_module.TransferService.mysql_table_has_inbound_fk = _async((True, False, ""))
    app_module.TransferService.mysql_swap_temp_to_final = _async((True, ""))
    app_module.TransferService.mysql_apply_index_clauses = _async((True, [], ""))
    app_module.TransferService.anonymize_table = _async((True, 0, ""))
    app_module.TransferService.source_generated_columns = _async((True, {"kunde": ["voller_name", "umsatz_brutto"]}, ""))
    destination.value = next(o.key for o in destination.options if o.text == "local-shop-renamed")
    fire(destination, "on_select")
    find(root, ft.Dropdown, label="Table").value = "shop.kunde"
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.2)
    log = log_text(root)
    check("voller_name" in log and "umsatz_brutto" in log, f"the lost columns are named: {log[-200:]}")
    check("recompute" in log.lower() or "static" in log.lower(), "it says what that means")

    print("\n9f. A PostgreSQL destination warns about generated columns too")

    async def pg_ok(*_a, **_k):
        return TransferResult(status="success", rows=3, elapsed_ms=4, parallel=1, message="ok")

    app_module.TransferService.transfer_scope = pg_ok
    app_module.TransferService.source_generated_columns = _async((True, {"public.kunde": ["voller_name"]}, ""))
    destination.value = next(o.key for o in destination.options if o.text == "pg-local")
    fire(destination, "on_select")
    find(root, ft.Dropdown, label="Table").value = "public.kunde"
    click(button(root, "Run transfer"))
    await asyncio.sleep(0.2)
    log = log_text(root)
    check("voller_name" in log, f"the PostgreSQL path names them too: {log[-160:]}")

    print("\n10. Every wired handler is an event its control really has")
    dead = []
    for control in walk([root, page.seen_dialogs]):
        try:
            fresh = type(control)()
        except Exception:  # needs arguments; its own handlers are checked where it is used
            continue
        for attr in dir(control):
            if attr.startswith("on_") and getattr(control, attr, None) is not None and not hasattr(fresh, attr):
                dead.append(f"{type(control).__name__}.{attr}")
    check(not dead, f"no handler hangs on a name Flet never calls: {sorted(set(dead))}")

    for task in page.tasks:
        task.cancel()
    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "all checks passed"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
