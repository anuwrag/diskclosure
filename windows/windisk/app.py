"""Terminal UI for seeing what is using space on Windows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Static, TabbedContent, TabPane

from windisk.scanner import (
    ChildEntry,
    FileHit,
    Place,
    ScanCancelled,
    TrashError,
    catalog,
    describe_path,
    disk_root,
    display_path,
    format_size,
    hash_file,
    is_reparse,
    list_children,
    measure_path,
    move_to_trash,
    scan_files,
    share_text,
    tree_sizes,
    volume_name,
)

_GROUP_ORDER = ("Home", "App data", "Developer", "Drives", "This PC")


@dataclass
class Item:
    key: str
    label: str
    path: Path | None
    size: int | None
    kind: str
    hint: str = ""
    group: str = ""
    mtime: float | None = None
    copies: int = 0
    checksum: str = ""
    order: int = 0


@dataclass
class Level:
    title: str
    path: Path | None
    items: list[Item]
    total: int | None = None


@dataclass
class FileScan:
    root: Path
    seen: int = 0
    done: bool = False
    running: bool = True
    current: str = ""
    largest: list[Item] = field(default_factory=list)
    dupes: list[Item] = field(default_factory=list)
    error: str = ""


class BrowserTable(DataTable):
    """Folder and file table. Enter opens the row in File Explorer."""

    BINDINGS = [Binding("enter", "open_row", "Open")]

    def action_open_row(self) -> None:
        app = self.app
        if isinstance(app, DiskclosureApp):
            app.action_open_selected()


class TrashConfirm(ModalScreen[bool]):
    """Ask before Windows moves a file or folder to the Recycle Bin."""

    def __init__(self, item: Item) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        detail = format_size(self.item.size) if self.item.size and self.item.size > 0 else "Size unknown"
        if self.item.kind == "folder":
            body = (
                f"{detail}. This folder and everything inside it will move to the Recycle Bin. "
                "You can put it back from File Explorer."
            )
        else:
            body = f"{detail}. You can put it back from the Recycle Bin."
        with Vertical(id="dialog"):
            yield Static("Move this to the Recycle Bin?", id="dialog-title")
            yield Static(display_path(self.item.path) if self.item.path else self.item.label, id="dialog-path")
            yield Static(body, id="dialog-detail")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Move to Recycle Bin", id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class DiskclosureApp(App):
    """Browse folder sizes, the largest files, and same-size copies."""

    TITLE = "Diskclosure"
    CSS = """
    Screen {
        background: #121418;
        color: #e8eaed;
    }
    #banner {
        height: 5;
        padding: 1 2 0 2;
        background: #1b1f27;
    }
    #crumb {
        height: 1;
        padding: 0 2;
        color: #9aa3b2;
        background: #121418;
    }
    #filter {
        margin: 0 1;
        background: #1b1f27;
        border: tall #2c3340;
    }
    #filter:focus {
        border: tall #3d9cf0;
    }
    TabbedContent {
        height: 1fr;
        background: #121418;
    }
    TabPane {
        height: 1fr;
        padding: 0;
    }
    DataTable {
        height: 1fr;
        background: #121418;
    }
    DataTable > .datatable--header {
        background: #1b1f27;
        color: #9aa3b2;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #24558a;
        color: #ffffff;
    }
    DataTable > .datatable--even-row {
        background: #171b22;
    }
    #status {
        height: 1;
        padding: 0 2;
        color: #9aa3b2;
        background: #1b1f27;
    }
    Footer {
        background: #1b1f27;
    }
    TrashConfirm {
        align: center middle;
    }
    #dialog {
        width: 78;
        height: auto;
        padding: 1 2;
        background: #1b1f27;
        border: round #e0a106;
    }
    #dialog-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #dialog-path {
        color: #ffb020;
        margin-bottom: 1;
    }
    #dialog-detail {
        margin-bottom: 1;
    }
    #dialog-buttons {
        height: auto;
        align: right middle;
    }
    #dialog-buttons Button {
        margin-left: 1;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "open_selected", "Open"),
        Binding("l", "into", "Inside"),
        Binding("backspace", "back", "Back"),
        Binding("t", "trash", "Recycle"),
        Binding("s", "scan_folder", "This folder"),
        Binding("c", "checksum", "Checksum"),
        Binding("r", "rescan", "Rescan"),
        Binding("slash,/", "focus_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear", show=False),
    ]

    def __init__(self, auto_scan: bool = True) -> None:
        super().__init__()
        self.auto_scan = auto_scan
        self._places: list[Place] = []
        self._stack: list[Level] = []
        self._filter = ""
        self._disk_used = 0
        self._disk_name = "Windows"
        self._measuring = False
        self._measure_started = 0.0
        self._last_du_path = ""
        self._catalog_sorted = False
        self._scan: FileScan | None = None
        self._scan_override: Path | None = None
        self._places_cancel = threading.Event()
        self._drill_cancel = threading.Event()
        self._file_cancel = threading.Event()
        self._hash_cancel = threading.Event()
        self._drill_token = 0
        self._file_token = 0
        self._hash_token = 0
        self._ticks = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="banner")
        yield Static("", id="crumb")
        yield Input(placeholder="Filter names and paths", id="filter")
        with TabbedContent(id="modes"):
            with TabPane("Common folders", id="folders"):
                yield BrowserTable(id="folder-table")
            with TabPane("Largest files", id="files"):
                yield BrowserTable(id="file-table")
            with TabPane("Same size", id="dupes"):
                yield BrowserTable(id="dupe-table")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._disk_name = volume_name()
        self._setup_table(
            "folder-table",
            [("Name", "name", 32), ("Size", "size", 12), ("Share", "share", 16), ("Note", "note", 48)],
        )
        self._setup_table(
            "file-table",
            [("Size", "size", 12), ("Modified", "modified", 18), ("Kind", "kind", 14), ("Path", "path", 72)],
        )
        self._setup_table(
            "dupe-table",
            [
                ("Size", "size", 12),
                ("Copies", "copies", 8),
                ("Checksum", "checksum", 14),
                ("Path", "path", 72),
            ],
        )
        self._places = catalog()
        self._stack = [Level("Common folders", None, self._items_from_places(self._places))]
        self._render_folders()
        self._refresh_banner()
        self._paint_status()
        self.query_one("#folder-table", DataTable).focus()
        if self.auto_scan:
            self._start_catalog_scan()
        self.set_interval(0.5, self._tick)

    def _setup_table(self, table_id: str, columns: list[tuple[str, str, int]]) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.fixed_columns = 1
        for label, key, width in columns:
            table.add_column(label, key=key, width=width)

    def _items_from_places(self, places: list[Place]) -> list[Item]:
        items: list[Item] = []
        for index, place in enumerate(places):
            items.append(
                Item(
                    key=f"dir:{place.path}",
                    label=place.label,
                    path=place.path,
                    size=None,
                    kind="folder",
                    hint=place.hint,
                    group=place.group,
                    order=index,
                )
            )
        return items

    def _start_catalog_scan(self) -> None:
        self._places_cancel.set()
        cancel = threading.Event()
        self._places_cancel = cancel
        self._measuring = True
        self._catalog_sorted = False
        self._measure_started = time.monotonic()
        self._last_du_path = ""
        for item in self._stack[0].items:
            item.size = None
        self._render_folders()
        self.run_worker(
            partial(self._measure_catalog, cancel),
            thread=True,
            exclusive=True,
            group="places",
            exit_on_error=False,
        )

    def _measure_catalog(self, cancel: threading.Event) -> None:
        home = Path.home()
        home_key = _path_key(home)
        inside: list[Place] = []
        outside: list[Place] = []
        for place in self._places:
            place_key = _path_key(place.path)
            if place_key == home_key or place_key.startswith(home_key + os.sep):
                inside.append(place)
            else:
                outside.append(place)
        finished = False

        def on_progress(directory: str) -> None:
            self._last_du_path = display_path(Path(directory))

        try:
            totals = tree_sizes(home, cancel, on_progress)
            batch: list[tuple[str, int]] = []
            for place in inside:
                size = totals.get(_path_key(place.path))
                batch.append((f"dir:{place.path}", -1 if size is None else size))
            if batch:
                self.call_from_thread(self._apply_sizes, batch)
            for place in outside:
                if cancel.is_set():
                    raise ScanCancelled()
                self._last_du_path = display_path(place.path)
                size = measure_path(place.path, cancel)
                self.call_from_thread(
                    self._apply_sizes,
                    [(f"dir:{place.path}", -1 if size is None else size)],
                )
            finished = True
        except ScanCancelled:
            return
        except Exception as exc:
            finished = True
            self.notify(str(exc), title="Could not measure folders", severity="error")
        finally:
            if finished:
                try:
                    self.call_from_thread(self._catalog_finished)
                except RuntimeError:
                    pass

    def _apply_sizes(self, batch: list[tuple[str, int]]) -> None:
        table = self.query_one("#folder-table", DataTable)
        showing_catalog = len(self._stack) == 1 and self._pane() == "folders"
        base = self._disk_used
        for key, size in batch:
            for level in self._stack:
                for item in level.items:
                    if item.key == key:
                        item.size = size
            if showing_catalog and key in table.rows:
                table.update_cell(key, "size", format_size(size))
                table.update_cell(key, "share", share_text(size, base))

    def _catalog_finished(self) -> None:
        self._measuring = False
        self._catalog_sorted = True
        if self._stack and self._stack[-1].path is None:
            self._render_folders()
        self._paint_status()

    def _load_children(self, path: Path, cancel: threading.Event, token: int) -> None:
        try:
            total, children = list_children(path, cancel)
        except ScanCancelled:
            return
        except Exception as exc:
            self.call_from_thread(self._drill_failed, token, str(exc))
            return
        self.call_from_thread(self._drill_ready, token, total, children)

    def _drill_failed(self, token: int, message: str) -> None:
        if token != self._drill_token:
            return
        self.notify(message, title="Could not list that folder", severity="error")

    def _drill_ready(self, token: int, total: int | None, children: list[ChildEntry]) -> None:
        if token != self._drill_token or not self._stack:
            return
        level = self._stack[-1]
        level.total = total
        level.items = [_child_item(child) for child in children]
        level.items.sort(key=_size_key)
        self._render_folders()
        self._paint_status()

    def _scan_worker(self, root: Path, cancel: threading.Event, token: int) -> None:
        def on_progress(seen: int, directory: str, largest: list[FileHit]) -> None:
            self.call_from_thread(self._file_progress, token, seen, directory, largest)

        try:
            largest, groups, seen = scan_files(root, cancel, on_progress)
        except ScanCancelled:
            return
        except Exception as exc:
            self.call_from_thread(self._file_failed, token, str(exc))
            return
        self.call_from_thread(self._file_finished, token, largest, groups, seen)

    def _file_progress(self, token: int, seen: int, directory: str, largest: list[FileHit]) -> None:
        if token != self._file_token or self._scan is None:
            return
        self._scan.seen = seen
        self._scan.current = display_path(Path(directory))
        self._scan.largest = [_file_item(hit) for hit in largest]
        if self._pane() == "files":
            self._render_files()
        self._paint_status()

    def _file_failed(self, token: int, message: str) -> None:
        if token != self._file_token or self._scan is None:
            return
        self._scan.running = False
        self._scan.error = message
        self.notify(message, title="File scan stopped", severity="error")
        self._paint_status()

    def _file_finished(
        self,
        token: int,
        largest: list[FileHit],
        groups: list[list[FileHit]],
        seen: int,
    ) -> None:
        if token != self._file_token or self._scan is None:
            return
        self._scan.seen = seen
        self._scan.done = True
        self._scan.running = False
        self._scan.largest = [_file_item(hit) for hit in largest]
        self._scan.dupes = _dupe_items(groups)
        if self._pane() == "files":
            self._render_files()
        elif self._pane() == "dupes":
            self._render_dupes()
        self._paint_status()

    def _hash_worker(self, paths: list[Path], cancel: threading.Event, token: int) -> None:
        found: dict[str, str | None] = {}
        for path in paths:
            if cancel.is_set():
                return
            self.call_from_thread(self._hash_status, token, display_path(path))
            found[str(path)] = hash_file(path, cancel)
        self.call_from_thread(self._apply_hashes, token, found)

    def _hash_status(self, token: int, path: str) -> None:
        if token != self._hash_token:
            return
        self._last_du_path = path
        self._paint_status()

    def _apply_hashes(self, token: int, found: dict[str, str | None]) -> None:
        if token != self._hash_token or self._scan is None:
            return
        by_digest: dict[str, list[Item]] = {}
        for item in self._scan.dupes:
            if item.path is None or str(item.path) not in found:
                continue
            digest = found[str(item.path)]
            if not digest:
                item.checksum = "unreadable"
                continue
            by_digest.setdefault(digest, []).append(item)
        identical_files = 0
        for items in by_digest.values():
            label = "identical" if len(items) >= 2 else "different"
            if label == "identical":
                identical_files += len(items)
            for item in items:
                item.checksum = label
        self._render_dupes()
        if identical_files:
            self.notify(
                f"{identical_files} files matched. The copies you do not need can go to the Recycle Bin.",
                title="Identical copies",
            )
        else:
            self.notify("Those files are the same size, but the contents differ.", title="Not the same file")

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane.id or "folders"
        if pane in {"files", "dupes"}:
            root = self._scan_override or self._file_root(selected_only=False)
            force = self._scan_override is not None
            self._scan_override = None
            self._begin_file_scan(root, force=force)
        table_ids = {"folders": "folder-table", "files": "file-table", "dupes": "dupe-table"}
        if pane in table_ids and not isinstance(self.focused, Input):
            self.query_one(f"#{table_ids[pane]}", DataTable).focus()
        self._paint_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter":
            return
        self._filter = event.value.strip().lower()
        self._render_active()

    def exit(self, result=None, return_code: int = 0, message=None) -> None:
        self._stop_scans()
        super().exit(result, return_code=return_code, message=message)

    def action_quit(self) -> None:
        self.exit()

    def _stop_scans(self) -> None:
        self._places_cancel.set()
        self._drill_cancel.set()
        self._file_cancel.set()
        self._hash_cancel.set()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_clear_filter(self) -> None:
        if isinstance(self.focused, Input) or self._filter:
            field_input = self.query_one("#filter", Input)
            field_input.value = ""
            self._filter = ""
            self._render_active()
            self._focus_active_table()
            return
        self.action_back()

    def action_open_selected(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if isinstance(self.focused, Input):
            self._focus_active_table()
            return
        item = self._selected_item()
        if item is None or item.path is None or item.kind == "header":
            self.notify("Select a file or folder first.")
            return
        if not item.path.exists():
            self.notify("That item is no longer there.", severity="warning")
            return
        _reveal(item.path, item.kind)
        if item.kind == "folder":
            self.notify(f"Opened {display_path(item.path)} in File Explorer")
        else:
            self.notify(f"Showing {item.path.name} in File Explorer")

    def action_into(self) -> None:
        if isinstance(self.focused, Input) or self._pane() != "folders":
            return
        item = self._selected_item()
        if item is None or item.kind != "folder" or item.path is None:
            self.notify("Select a folder, then press l to list what is inside it.")
            return
        if is_reparse(item.path):
            self.notify("That is a shortcut. Enter shows it in File Explorer.")
            return
        self._drill_token += 1
        token = self._drill_token
        self._drill_cancel.set()
        cancel = threading.Event()
        self._drill_cancel = cancel
        self._stack.append(Level(item.label, item.path, [], None))
        self._render_folders()
        self._paint_status()
        self.run_worker(
            partial(self._load_children, item.path, cancel, token),
            thread=True,
            exclusive=True,
            group="drill",
            exit_on_error=False,
        )

    def action_back(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self._pane() != "folders" or len(self._stack) <= 1:
            return
        self._drill_cancel.set()
        self._drill_token += 1
        self._stack.pop()
        self._render_folders()
        self._paint_status()

    def action_scan_folder(self) -> None:
        if isinstance(self.focused, Input):
            return
        self._scan_override = self._file_root(selected_only=True)
        tabs = self.query_one(TabbedContent)
        if tabs.active != "files":
            tabs.active = "files"
        else:
            root = self._scan_override
            self._scan_override = None
            self._begin_file_scan(root, force=True)

    def action_rescan(self) -> None:
        if isinstance(self.focused, Input):
            return
        pane = self._pane()
        if pane == "folders":
            if self._stack and self._stack[-1].path is None:
                self._start_catalog_scan()
            elif self._stack and self._stack[-1].path is not None:
                path = self._stack[-1].path
                self._stack[-1].items = []
                self._render_folders()
                self._drill_token += 1
                token = self._drill_token
                self._drill_cancel.set()
                cancel = threading.Event()
                self._drill_cancel = cancel
                self.run_worker(
                    partial(self._load_children, path, cancel, token),
                    thread=True,
                    exclusive=True,
                    group="drill",
                    exit_on_error=False,
                )
            return
        root = self._scan.root if self._scan else self._file_root(selected_only=False)
        self._begin_file_scan(root, force=True)

    def action_checksum(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self._pane() != "dupes" or self._scan is None:
            self.notify("Open Same size, select a file, then press c. That reads the copies and checks they match.")
            return
        item = self._selected_item()
        if item is None or item.path is None:
            return
        peers = [other for other in self._scan.dupes if other.size == item.size and other.path is not None]
        if len(peers) < 2:
            self.notify("Nothing else in this list has the same size.")
            return
        if len(peers) > 12:
            peers = peers[:12]
            self.notify("Comparing the first 12 copies of this size.")
        self._hash_cancel.set()
        cancel = threading.Event()
        self._hash_cancel = cancel
        self._hash_token += 1
        token = self._hash_token
        for peer in peers:
            peer.checksum = "reading"
        self._render_dupes()
        self.run_worker(
            partial(self._hash_worker, [peer.path for peer in peers if peer.path], cancel, token),
            thread=True,
            exclusive=True,
            group="hash",
            exit_on_error=False,
        )

    def action_trash(self) -> None:
        if isinstance(self.focused, Input) or isinstance(self.screen, ModalScreen):
            return
        item = self._selected_item()
        if item is None or item.path is None or item.kind == "header":
            self.notify("Select the file or folder you want to remove.")
            return
        self.push_screen(TrashConfirm(item), self._trash_confirmed)

    def _trash_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        item = self._selected_item()
        if item is None or item.path is None:
            return
        path = item.path
        self.run_worker(
            partial(self._trash_worker, path),
            thread=True,
            exclusive=False,
            group="trash",
            exit_on_error=False,
        )

    def _trash_worker(self, path: Path) -> None:
        try:
            move_to_trash(path)
        except TrashError as exc:
            self.call_from_thread(self.notify, str(exc), title="Left in place", severity="warning")
            return
        self.call_from_thread(self._after_trash, path)

    def _after_trash(self, path: Path) -> None:
        self._drop_path(path)
        self._refresh_banner()
        self.notify(
            "Moved to the Recycle Bin. Press r if you want the folder sizes measured again.",
            title="Recycle Bin",
        )

    def _drop_path(self, path: Path) -> None:
        target = _norm(path)
        for level in self._stack:
            level.items = [item for item in level.items if item.path is None or _norm(item.path) != target]
        if self._scan is not None:
            self._scan.largest = [item for item in self._scan.largest if item.path is None or _norm(item.path) != target]
            self._scan.dupes = [item for item in self._scan.dupes if item.path is None or _norm(item.path) != target]
        self._render_active()

    def _begin_file_scan(self, root: Path, force: bool = False) -> None:
        if (
            not force
            and self._scan is not None
            and self._scan.root == root
            and (self._scan.running or self._scan.done)
        ):
            self._render_active()
            return
        self._file_cancel.set()
        cancel = threading.Event()
        self._file_cancel = cancel
        self._file_token += 1
        token = self._file_token
        self._scan = FileScan(root=root, running=True)
        self._render_active()
        self._paint_status()
        self.run_worker(
            partial(self._scan_worker, root, cancel, token),
            thread=True,
            exclusive=True,
            group="files",
            exit_on_error=False,
        )

    def _file_root(self, selected_only: bool) -> Path:
        if selected_only and self._pane() == "folders":
            item = self._selected_item()
            if item and item.kind == "folder" and item.path is not None:
                return item.path
        if self._stack and self._stack[-1].path is not None:
            return self._stack[-1].path
        return Path.home()

    def _pane(self) -> str:
        try:
            active = self.query_one(TabbedContent).active
        except Exception:
            return "folders"
        return active or "folders"

    def _tick(self) -> None:
        self._ticks += 1
        if self._ticks % 10 == 0:
            self._refresh_banner()
        self._paint_status()

    def _refresh_banner(self) -> None:
        usage = shutil.disk_usage(disk_root())
        self._disk_used = usage.used
        total = usage.total or 1
        fraction = usage.used / total
        width = 36
        filled = round(fraction * width)
        if fraction >= 0.9:
            color = "#ff5a5f"
        elif fraction >= 0.75:
            color = "#e0a106"
        else:
            color = "#3d9cf0"
        bar = f"[{color}]{'█' * filled}{'░' * (width - filled)}[/]"
        banner = (
            f"[b]Diskclosure[/b]  [dim]{self._disk_name}[/]\n"
            f"{bar}  {fraction:.0%} full\n"
            f"{format_size(usage.used)} used    {format_size(usage.free)} free    {format_size(usage.total)} total"
        )
        self.query_one("#banner", Static).update(banner)

    def _paint_status(self) -> None:
        self.query_one("#crumb", Static).update(self._crumb_text())
        self.query_one("#status", Static).update(self._status_text())

    def _crumb_text(self) -> str:
        if not self._stack:
            return ""
        parts = [level.title for level in self._stack]
        text = "  ›  ".join(parts)
        if len(self._stack) == 1:
            text += "     AppData is inside your profile, so those sizes overlap"
        return text

    def _status_text(self) -> str:
        pane = self._pane()
        if pane == "folders":
            if self._measuring:
                elapsed = int(time.monotonic() - self._measure_started)
                where = f" · {self._last_du_path}" if self._last_du_path else ""
                return f"Measuring folders · {elapsed}s{where}"
            if len(self._stack) > 1 and self._stack[-1].path and not self._stack[-1].items:
                return f"Listing {display_path(self._stack[-1].path)}…"
            return "Enter opens the folder in File Explorer · l lists inside · t moves it to the Recycle Bin"
        if self._scan is None:
            return "Largest files and same-size copies appear after a scan of your user profile."
        root = display_path(self._scan.root)
        if self._scan.error:
            return self._scan.error
        if self._scan.running:
            return f"Reading {root} · {self._scan.seen:,} files · {self._scan.current}"
        if pane == "dupes":
            return (
                f"{root} · {self._scan.seen:,} files · "
                "same size is not proof they match · c checksums the selected copies"
            )
        return f"Largest files in {root} · {self._scan.seen:,} files read · Enter shows one in File Explorer"

    def _render_active(self) -> None:
        pane = self._pane()
        if pane == "files":
            self._render_files()
        elif pane == "dupes":
            self._render_dupes()
        else:
            self._render_folders()

    def _render_folders(self) -> None:
        if not self._stack:
            return
        level = self._stack[-1]
        rows = self._folder_rows(level)
        base = self._disk_used if level.path is None else level.total
        cells = []
        for item in rows:
            if item.kind == "header":
                cells.append((item.key, [Text(item.label, style="bold #8eb6ff"), "", "", ""]))
                continue
            cells.append(
                (
                    item.key,
                    [item.label, format_size(item.size), share_text(item.size, base), item.hint],
                )
            )
        self._fill_table("folder-table", cells)

    def _folder_rows(self, level: Level) -> list[Item]:
        items = [item for item in level.items if item.kind != "header"]
        if level.path is not None:
            visible = [item for item in items if _matches(item, self._filter)]
            return sorted(visible, key=_size_key)
        grouped: list[Item] = []
        for group in _GROUP_ORDER:
            members = [item for item in items if item.group == group and _matches(item, self._filter)]
            if not members:
                continue
            if self._catalog_sorted:
                members.sort(key=_size_key)
            else:
                members.sort(key=lambda item: item.order)
            grouped.append(Item(key=f"header:{group}", label=group, path=None, size=None, kind="header"))
            grouped.extend(members)
        return grouped

    def _render_files(self) -> None:
        items = []
        if self._scan is not None:
            items = [item for item in self._scan.largest if _matches(item, self._filter)]
        cells = [
            (
                item.key,
                [format_size(item.size), _format_mtime(item.mtime), _kind_text(item.hint), item.label],
            )
            for item in items
        ]
        self._fill_table("file-table", cells)

    def _render_dupes(self) -> None:
        items = []
        if self._scan is not None:
            items = [item for item in self._scan.dupes if _matches(item, self._filter)]
        cells = [
            (
                item.key,
                [format_size(item.size), str(item.copies), item.checksum or "—", item.label],
            )
            for item in items
        ]
        self._fill_table("dupe-table", cells)

    def _fill_table(self, table_id: str, rows: list[tuple[str, list]]) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        previous = _cursor_key(table)
        table.clear()
        restore = 0
        for index, (key, _cells) in enumerate(rows):
            if not str(key).startswith("header:"):
                restore = index
                break
        for index, (key, cells) in enumerate(rows):
            table.add_row(*cells, key=key)
            if key == previous and not str(key).startswith("header:"):
                restore = index
        if table.row_count:
            table.move_cursor(row=restore)

    def _selected_item(self) -> Item | None:
        table = self.query_one(f"#{self._active_table_id()}", DataTable)
        key = _cursor_key(table)
        if key is None:
            return None
        pane = self._pane()
        if pane == "folders" and self._stack:
            for item in self._stack[-1].items:
                if item.key == key:
                    return item
            if key.startswith("header:"):
                return Item(key=key, label=key, path=None, size=None, kind="header")
            return None
        pool: list[Item] = []
        if self._scan is not None and pane == "files":
            pool = self._scan.largest
        elif self._scan is not None and pane == "dupes":
            pool = self._scan.dupes
        for item in pool:
            if item.key == key:
                return item
        return None

    def _active_table_id(self) -> str:
        return {"folders": "folder-table", "files": "file-table", "dupes": "dupe-table"}.get(
            self._pane(), "folder-table"
        )

    def _focus_active_table(self) -> None:
        self.query_one(f"#{self._active_table_id()}", DataTable).focus()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("This Diskclosure build runs on Windows. Use the macOS app on a Mac.")
    DiskclosureApp().run()


def _child_item(child: ChildEntry) -> Item:
    kind_label = describe_path(child.path, child.kind)
    hint = kind_label if child.kind == "folder" else ""
    if child.kind == "file":
        hint = kind_label
    return Item(
        key=f"dir:{child.path}",
        label=child.path.name,
        path=child.path,
        size=child.size,
        kind=child.kind,
        hint=hint if child.kind != "folder" or kind_label != "folder" else "",
        mtime=child.mtime,
    )


def _file_item(hit: FileHit) -> Item:
    return Item(
        key=f"file:{hit.path}",
        label=display_path(hit.path),
        path=hit.path,
        size=hit.size,
        kind="file",
        hint=describe_path(hit.path, "file"),
        mtime=hit.mtime,
    )


def _dupe_items(groups: list[list[FileHit]]) -> list[Item]:
    rows: list[Item] = []
    for group in groups:
        copies = len(group)
        ordered = sorted(group, key=lambda hit: str(hit.path))
        for hit in ordered:
            rows.append(
                Item(
                    key=f"dupe:{hit.path}",
                    label=display_path(hit.path),
                    path=hit.path,
                    size=hit.size,
                    kind="file",
                    hint=describe_path(hit.path, "file"),
                    mtime=hit.mtime,
                    copies=copies,
                )
            )
        if len(rows) >= 400:
            break
    return rows


def _size_key(item: Item) -> tuple:
    if item.size is None:
        return (1, 0, item.label.lower())
    if item.size < 0:
        return (2, 0, item.label.lower())
    return (0, -item.size, item.label.lower())


def _matches(item: Item, text: str) -> bool:
    if not text:
        return True
    blob = f"{item.label} {item.hint} {item.path or ''}".lower()
    return text in blob


def _format_mtime(moment: float | None) -> str:
    if moment is None:
        return ""
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M")


def _kind_text(kind: str) -> Text | str:
    if kind == "archive":
        return Text("archive", style="#ffb020")
    if kind in {"application", "package"}:
        return Text(kind, style="#8eb6ff")
    return kind


def _cursor_key(table: DataTable) -> str | None:
    if table.row_count == 0:
        return None
    try:
        row_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
    except Exception:
        return None
    value = getattr(row_key, "value", None)
    return value if isinstance(value, str) else str(row_key)


def _reveal(path: Path, kind: str) -> None:
    if kind == "folder":
        command = ["explorer", str(path)]
    else:
        command = ["explorer", f"/select,{path}"]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _norm(path: Path) -> str:
    return _path_key(path)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


if __name__ == "__main__":
    main()
