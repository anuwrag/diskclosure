"""Measure folders and files on Windows without following junctions off this disk."""

from __future__ import annotations

import hashlib
import heapq
import os
import stat
import string
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Files at least this large are grouped when another file has the same length.
MIN_SAME_SIZE = 10 * 1024 * 1024
LARGEST_LIMIT = 250
# Reparse points (junctions, symlinks, mount points). Must not be descended.
_REPARSE = 0x400

_ARCHIVE_SUFFIXES = {
    ".zip",
    ".7z",
    ".rar",
    ".iso",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".cab",
    ".msix",
    ".appx",
}
_PROGRAM_SUFFIXES = {".exe", ".msi", ".msix", ".appx"}
_PROTECTED_HOME_NAMES = {
    "desktop",
    "documents",
    "downloads",
    "favorites",
    "links",
    "music",
    "pictures",
    "videos",
    "contacts",
    "searches",
    "saved games",
    "onedrive",
    "appdata",
    "3d objects",
}


class ScanCancelled(Exception):
    """The user left a listing or quit while a measurement was running."""


class TrashError(Exception):
    """Windows refused the item, or Diskclosure will not move it."""


@dataclass(frozen=True)
class Place:
    """A well-known Windows location shown on the first screen."""

    label: str
    path: Path
    group: str
    hint: str


@dataclass(frozen=True)
class ChildEntry:
    path: Path
    size: int
    kind: str
    mtime: float | None


@dataclass(frozen=True)
class FileHit:
    path: Path
    size: int
    mtime: float


def format_size(num: int | None) -> str:
    """Format a byte count the way File Explorer does, in decimal KB/MB/GB."""

    if num is None:
        return "…"
    if num < 0:
        return "no access"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num)
    for unit in units:
        if value < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


def display_path(path: Path) -> str:
    """Show a path with the user profile shortened to ~."""

    text = str(path)
    home = str(Path.home())
    if os.path.normcase(text) == os.path.normcase(home):
        return "~"
    prefix = home + os.sep
    if os.path.normcase(text).startswith(os.path.normcase(prefix)):
        return "~" + text[len(home) :]
    return text


def share_text(size: int | None, base: int | None) -> str:
    """A short bar and percent. Base is the parent folder or the whole disk."""

    if size is None or size < 0 or base is None or base <= 0:
        return ""
    fraction = min(1.0, size / base)
    cells = 8
    filled = round(fraction * cells)
    return f"{'█' * filled}{'░' * (cells - filled)} {fraction * 100:4.0f}%"


def describe_path(path: Path, kind: str) -> str:
    if kind == "alias":
        return "shortcut"
    suffix = path.suffix.lower()
    if suffix == ".exe":
        return "program"
    if suffix in {".msi", ".msix", ".appx"}:
        return "installer"
    if suffix in _ARCHIVE_SUFFIXES or path.name.lower().endswith(".tar.gz"):
        return "archive"
    if suffix in _PROGRAM_SUFFIXES:
        return "program"
    if kind == "folder":
        return "folder"
    if suffix:
        return suffix[1:]
    return "file"


def disk_root() -> Path:
    """Drive that holds the user profile, such as ``C:\\``."""

    return Path(Path.home().anchor)


def catalog() -> list[Place]:
    """Common Windows folders that exist for this user. Missing ones are left out."""

    home = Path.home()
    local = home / "AppData" / "Local"
    roaming = home / "AppData" / "Roaming"
    windows = Path(os.environ.get("SystemRoot", "C:\\Windows"))
    program_files = Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"))
    program_data = Path(os.environ.get("ProgramData", "C:\\ProgramData"))
    specs: list[tuple[str, Path, str, str]] = [
        ("Desktop", home / "Desktop", "Home", "Files on the desktop"),
        ("Documents", home / "Documents", "Home", "Documents and saved app files"),
        ("Downloads", home / "Downloads", "Home", "Installers and old copies"),
        ("Pictures", home / "Pictures", "Home", "Photos and images"),
        ("Music", home / "Music", "Home", "Music and audio"),
        ("Videos", home / "Videos", "Home", "Videos and recordings"),
        ("OneDrive", home / "OneDrive", "Home", "Files synced with OneDrive"),
        ("User profile", home, "Home", "Everything in your account"),
        ("AppData", home / "AppData", "App data", "Application data for this account"),
        ("Local", local, "App data", "Caches and per-user program data"),
        ("Roaming", roaming, "App data", "Settings that follow your account"),
        ("Temp", Path(os.environ.get("TEMP", str(local / "Temp"))), "App data", "Temporary files, usually safe to clear"),
        ("Crash dumps", local / "CrashDumps", "App data", "Old crash reports"),
        ("npm cache", roaming / "npm-cache", "Developer", "Cached npm packages"),
        ("npm cache (home)", home / ".npm", "Developer", "Cached npm packages"),
        ("pnpm store", local / "pnpm", "Developer", "pnpm content store"),
        ("pip cache", local / "pip", "Developer", "Cached Python downloads"),
        ("NuGet", home / ".nuget", "Developer", "NuGet packages"),
        ("Cargo", home / ".cargo", "Developer", "Rust toolchain and crates"),
        ("Gradle", home / ".gradle", "Developer", "Gradle caches"),
        ("Go build cache", local / "go-build", "Developer", "Go build cache"),
        ("VS Code", roaming / "Code", "Developer", "Editor data and caches"),
        ("Docker", roaming / "Docker", "Developer", "Docker Desktop settings"),
        ("Docker data", local / "Docker", "Developer", "Docker Desktop images and volumes"),
        ("Program Files", program_files, "This PC", "Installed programs"),
        ("Program Files (x86)", program_files_x86, "This PC", "32-bit programs"),
        ("ProgramData", program_data, "This PC", "Shared program data"),
        ("Windows Temp", windows / "Temp", "This PC", "System temporary files"),
    ]
    places = [
        Place(label, path, group, hint)
        for label, path, group, hint in specs
        if path.exists()
    ]
    profile_drive = _key(disk_root())
    for drive in fixed_drives():
        if _key(drive) == profile_drive:
            continue
        label = volume_name(drive) or "Local disk"
        places.append(Place(f"{label} ({drive.drive})", drive, "Drives", "Another disk on this PC"))
    return places


def fixed_drives() -> list[Path]:
    """Fixed disks, such as ``C:\\`` and ``D:\\``. Empty off Windows."""

    if sys.platform != "win32":
        return []
    import ctypes

    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    get_type = ctypes.windll.kernel32.GetDriveTypeW
    get_type.argtypes = [ctypes.c_wchar_p]
    get_type.restype = ctypes.c_uint
    drives: list[Path] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not bitmask & (1 << index):
            continue
        root = f"{letter}:\\"
        if get_type(root) == 3:
            drives.append(Path(root))
    return drives


def volume_name(root: Path | None = None) -> str:
    """Volume label for a drive. Falls back to a short name when Windows will not say."""

    target = root or disk_root()
    if sys.platform != "win32":
        return "Windows"
    import ctypes

    label = ctypes.create_unicode_buffer(256)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        str(target),
        label,
        256,
        None,
        None,
        None,
        None,
        0,
    )
    if ok and label.value.strip():
        return label.value.strip()
    drive = target.drive or str(target)
    return f"Drive {drive}"


def tree_sizes(root: Path, cancel: threading.Event, on_progress=None) -> dict[str, int]:
    """Total file bytes for ``root`` and every directory under it.

    Junctions, symlinks, and other volumes are not entered. A directory that
    cannot be read is recorded as ``-1``. Keys are normalized paths.
    """

    if cancel.is_set():
        raise ScanCancelled()
    try:
        root_info = os.lstat(root)
    except OSError:
        return {}
    if _is_link(root_info):
        return {}
    root_device = root_info.st_dev
    root_key = _key(root)
    direct: dict[str, int] = {root_key: 0}
    seen_inodes: set[tuple[int, int]] = set()
    children: dict[str, list[str]] = defaultdict(list)
    failed: set[str] = set()
    last_report = 0.0

    def note(directory: str) -> None:
        nonlocal last_report
        if on_progress is None:
            return
        moment = time.monotonic()
        if moment - last_report < 0.4:
            return
        last_report = moment
        on_progress(directory)

    def on_error(error: OSError) -> None:
        filename = getattr(error, "filename", None)
        if filename:
            failed.add(_key(Path(filename)))

    for directory, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
        if cancel.is_set():
            raise ScanCancelled()
        note(directory)
        current = _key(Path(directory))
        direct.setdefault(current, 0)
        kept: list[str] = []
        for name in dirnames:
            child = Path(directory) / name
            try:
                info = os.lstat(child)
            except OSError:
                failed.add(_key(child))
                continue
            if _is_link(info) or info.st_dev != root_device:
                continue
            kept.append(name)
            child_key = _key(child)
            children[current].append(child_key)
            direct.setdefault(child_key, 0)
        dirnames[:] = kept
        file_bytes = 0
        for name in filenames:
            child = Path(directory) / name
            try:
                info = os.lstat(child)
            except OSError:
                continue
            if _is_link(info) or not stat.S_ISREG(info.st_mode):
                continue
            if info.st_dev != root_device:
                continue
            inode = (info.st_dev, info.st_ino)
            if inode in seen_inodes:
                continue
            seen_inodes.add(inode)
            file_bytes += info.st_size
        direct[current] = direct.get(current, 0) + file_bytes

    totals: dict[str, int] = {}

    def compute(key: str) -> int:
        if key in totals:
            return totals[key]
        if key in failed and direct.get(key, 0) == 0 and not children.get(key):
            totals[key] = -1
            return -1
        size = direct.get(key, 0)
        for child in children.get(key, []):
            child_size = compute(child)
            if child_size > 0:
                size += child_size
        totals[key] = size
        return size

    compute(root_key)
    if on_progress is not None:
        on_progress(str(root))
    return totals


def measure_path(path: Path, cancel: threading.Event) -> int | None:
    """Size of one folder, or None when it cannot be read."""

    if cancel.is_set():
        raise ScanCancelled()
    if not path.exists():
        return None
    totals = tree_sizes(path, cancel)
    size = totals.get(_key(path))
    if size is None or size < 0:
        return None
    return size


def list_children(path: Path, cancel: threading.Event) -> tuple[int | None, list[ChildEntry]]:
    """Immediate children of a folder, with folder sizes including their contents."""

    if cancel.is_set():
        raise ScanCancelled()
    try:
        entries = list(os.scandir(path))
    except OSError:
        return None, []
    totals = tree_sizes(path, cancel)
    total = totals.get(_key(path))
    children: list[ChildEntry] = []
    for entry in entries:
        if cancel.is_set():
            raise ScanCancelled()
        child = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            children.append(ChildEntry(child, -1, "other", None))
            continue
        modified = info.st_mtime
        if _is_link(info):
            children.append(ChildEntry(child, info.st_size, "alias", modified))
            continue
        if entry.is_dir(follow_symlinks=False):
            size = totals.get(_key(child), -1)
            children.append(ChildEntry(child, size, "folder", modified))
        elif entry.is_file(follow_symlinks=False):
            children.append(ChildEntry(child, info.st_size, "file", modified))
        else:
            children.append(ChildEntry(child, info.st_size, "other", modified))
    return total, children


def scan_files(
    root: Path,
    cancel: threading.Event,
    on_progress,
    min_same_size: int = MIN_SAME_SIZE,
) -> tuple[list[FileHit], list[list[FileHit]], int]:
    """Walk ``root`` for the largest files and groups that share a size.

    ``on_progress(files_seen, current_directory, largest_so_far)`` may be called
    from this thread about four times a second.
    """

    if cancel.is_set():
        raise ScanCancelled()
    try:
        root_info = root.stat()
    except OSError as exc:
        raise OSError(f"Cannot read {root}") from exc
    root_device = root_info.st_dev

    largest: list[tuple[int, float, str]] = []
    by_size: dict[int, list[FileHit]] = defaultdict(list)
    seen_inodes: set[tuple[int, int]] = set()
    files_seen = 0
    last_report = 0.0

    def consider(path: str, info: os.stat_result) -> None:
        nonlocal files_seen
        if not stat.S_ISREG(info.st_mode):
            return
        inode = (info.st_dev, info.st_ino)
        if inode in seen_inodes:
            return
        seen_inodes.add(inode)
        files_seen += 1
        hit = (info.st_size, info.st_mtime, path)
        if len(largest) < LARGEST_LIMIT:
            heapq.heappush(largest, hit)
        elif info.st_size > largest[0][0]:
            heapq.heapreplace(largest, hit)
        if info.st_size >= min_same_size:
            by_size[info.st_size].append(FileHit(Path(path), info.st_size, info.st_mtime))

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        if cancel.is_set():
            raise ScanCancelled()
        kept: list[str] = []
        for name in dirnames:
            child = os.path.join(directory, name)
            try:
                info = os.lstat(child)
            except OSError:
                continue
            if _is_link(info) or info.st_dev != root_device:
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = os.path.join(directory, name)
            try:
                info = os.lstat(child)
            except OSError:
                continue
            if _is_link(info) or info.st_dev != root_device:
                continue
            consider(child, info)
        moment = time.monotonic()
        if moment - last_report >= 0.4:
            last_report = moment
            on_progress(files_seen, directory, _largest_hits(largest))

    groups = [hits for hits in by_size.values() if len(hits) >= 2]
    groups.sort(key=lambda hits: hits[0].size * (len(hits) - 1), reverse=True)
    on_progress(files_seen, str(root), _largest_hits(largest))
    return _largest_hits(largest), groups, files_seen


def _largest_hits(rows: list[tuple[int, float, str]]) -> list[FileHit]:
    ordered = sorted(rows, reverse=True)
    return [FileHit(Path(path), size, modified) for size, modified, path in ordered]


def hash_file(path: Path, cancel: threading.Event) -> str | None:
    """SHA-256 of a file, or None if the read is cancelled or fails."""

    digest = hashlib.sha256()
    try:
        handle = path.open("rb")
    except OSError:
        return None
    with handle:
        while True:
            if cancel.is_set():
                return None
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def system_roots() -> list[Path]:
    """Folders Diskclosure will not send to the Recycle Bin."""

    names = [
        os.environ.get("SystemRoot", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramData", ""),
    ]
    roots: list[Path] = []
    for name in names:
        if not name:
            continue
        path = Path(name)
        if path.exists():
            roots.append(path)
    return roots


def trash_block_reason(
    path: Path,
    home: Path | None = None,
    blocked_roots: list[Path] | None = None,
) -> str | None:
    """Why Diskclosure will not recycle this path, or None when it is allowed."""

    if "\n" in str(path) or "\x00" in str(path):
        return "That path cannot be moved safely."
    try:
        info = os.lstat(path)
    except OSError:
        return "That item is already gone."
    if _is_link(info):
        return "That item is a shortcut. Remove the shortcut in File Explorer if you are sure."
    try:
        real = path.resolve()
    except OSError as exc:
        return str(exc)

    if _is_drive_root(real):
        return "That is the disk itself."
    if any(part.lower() == "$recycle.bin" for part in real.parts):
        return "Empty the Recycle Bin from File Explorer. Diskclosure will not remove it directly."

    home_root = (home or Path.home()).resolve()
    if _same(real, home_root):
        return "That is your user profile."
    if _same(real.parent, home_root) and real.name.lower() in _PROTECTED_HOME_NAMES:
        return (
            f"{real.name} is a standard Windows folder. "
            "Open it and remove items inside it, rather than the folder itself."
        )

    roots = system_roots() if blocked_roots is None else blocked_roots
    for root in roots:
        try:
            root_real = root.resolve()
        except OSError:
            root_real = root
        if _same(real, root_real) or _is_inside(real, root_real):
            return f"{root_real} is part of Windows or installed programs, so Diskclosure will not move it."
    return None


def move_to_trash(path: Path) -> None:
    """Send a path to the Windows Recycle Bin. Raises TrashError on failure."""

    reason = trash_block_reason(path)
    if reason:
        raise TrashError(reason)
    if sys.platform != "win32":
        raise TrashError("The Recycle Bin is only available on Windows.")
    _recycle_with_shell(path)


def _recycle_with_shell(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    fo_delete = 3
    fof_allowundo = 0x40
    fof_no_confirmation = 0x10
    fof_no_error_ui = 0x0400
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = fo_delete
    # SHFileOperation expects a double-null-terminated list. A normal string
    # would stop at the first null, so keep an explicit buffer alive for the call.
    text = str(path.resolve())
    source = ctypes.create_unicode_buffer(text, len(text) + 2)
    operation.pFrom = ctypes.cast(source, ctypes.c_wchar_p)
    operation.fFlags = fof_allowundo | fof_no_confirmation | fof_no_error_ui
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise TrashError("Windows could not move that item to the Recycle Bin.")


def is_reparse(path: Path) -> bool:
    """True for symlinks, junctions, and other links that should not be followed."""

    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _is_link(info)


def _is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0) or 0
    return bool(attributes & _REPARSE)


def _is_drive_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return _same(resolved, Path(resolved.anchor))


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _same(path: Path, other: Path) -> bool:
    return _key(path) == _key(other)


def _is_inside(path: Path, parent: Path) -> bool:
    child = _key(path)
    base = _key(parent)
    if child == base:
        return False
    prefix = base if base.endswith(os.sep) else base + os.sep
    return child.startswith(os.path.normcase(prefix))
