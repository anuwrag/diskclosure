"""Measure folders and files on macOS without following shortcuts off this disk."""

from __future__ import annotations

import atexit
import hashlib
import heapq
import os
import queue
import stat
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Files at least this large are grouped when another file has the same length.
MIN_SAME_SIZE = 10 * 1024 * 1024
LARGEST_LIMIT = 250
HOME_DU_DEPTH = 4

_ARCHIVE_SUFFIXES = {
    ".dmg",
    ".pkg",
    ".zip",
    ".iso",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".rar",
    ".7z",
}
_PACKAGE_SUFFIXES = {".photoslibrary", ".musiclibrary", ".sparsebundle", ".app", ".bundle"}
_PROTECTED_HOME_NAMES = {
    "Desktop",
    "Documents",
    "Downloads",
    "Library",
    "Movies",
    "Music",
    "Pictures",
    "Public",
    "Applications",
    "Sites",
}

_PROCESSES: list[subprocess.Popen[str]] = []
_PROCESS_LOCK = threading.Lock()


class ScanCancelled(Exception):
    """The user left a listing or quit while a measurement was running."""


class TrashError(Exception):
    """Finder refused the item, or Diskclosure will not move it."""


@dataclass(frozen=True)
class Place:
    """A well-known Mac location shown on the first screen."""

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
    """Format a byte count the way Finder does, in decimal KB/MB/GB."""

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
    """Show a path with the home folder shortened to ~."""

    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    prefix = home + os.sep
    if text.startswith(prefix):
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
    if suffix == ".app":
        return "application"
    if suffix in _PACKAGE_SUFFIXES:
        return "package"
    if suffix in _ARCHIVE_SUFFIXES or path.name.lower().endswith(".tar.gz"):
        return "archive"
    if kind == "folder":
        return "folder"
    if suffix:
        return suffix[1:]
    return "file"


def catalog() -> list[Place]:
    """Common Mac folders that exist for this user. Missing ones are left out."""

    home = Path.home()
    specs: list[tuple[str, Path, str, str]] = [
        ("Desktop", home / "Desktop", "Home", "Files on the desktop"),
        ("Documents", home / "Documents", "Home", "Documents and saved app files"),
        ("Downloads", home / "Downloads", "Home", "Installers and old copies"),
        ("Movies", home / "Movies", "Home", "Videos and TV downloads"),
        ("Music", home / "Music", "Home", "Music library and GarageBand"),
        ("Pictures", home / "Pictures", "Home", "Photos and images"),
        ("Public", home / "Public", "Home", "Shared with other accounts"),
        ("Your Applications", home / "Applications", "Home", "Apps for this user only"),
        ("Home folder", home, "Home", "Everything in your account"),
        ("Library", home / "Library", "Library", "App support, caches, and mail"),
        ("Caches", home / "Library" / "Caches", "Library", "Usually safe to clear"),
        ("Logs", home / "Library" / "Logs", "Library", "Old logs are usually safe to remove"),
        ("Application Support", home / "Library" / "Application Support", "Library", "App data — review each app"),
        ("Containers", home / "Library" / "Containers", "Library", "Sandboxed app data"),
        ("Group Containers", home / "Library" / "Group Containers", "Library", "Data shared by related apps"),
        ("Mail", home / "Library" / "Mail", "Library", "Mailboxes and attachments"),
        ("Messages", home / "Library" / "Messages", "Library", "Messages and attachments"),
        ("iCloud Drive", home / "Library" / "Mobile Documents", "Library", "iCloud files, including app storage"),
        (
            "iOS Backups",
            home / "Library" / "Application Support" / "MobileSync" / "Backup",
            "Library",
            "Old device backups",
        ),
        ("Trash", home / ".Trash", "Library", "Already deleted, still using space"),
        ("Developer", home / "Library" / "Developer", "Developer", "Xcode, simulators, and toolchains"),
        ("Xcode DerivedData", home / "Library" / "Developer" / "Xcode" / "DerivedData", "Developer", "Rebuilds on the next compile"),
        ("Xcode Archives", home / "Library" / "Developer" / "Xcode" / "Archives", "Developer", "Old exported app archives"),
        (
            "iOS DeviceSupport",
            home / "Library" / "Developer" / "Xcode" / "iOS DeviceSupport",
            "Developer",
            "Symbol files for plugged-in devices",
        ),
        ("Simulator", home / "Library" / "Developer" / "CoreSimulator", "Developer", "Simulator devices and runtimes"),
        ("Docker", home / "Library" / "Containers" / "com.docker.docker", "Developer", "Docker images and volumes"),
        ("npm cache", home / ".npm", "Developer", "Cached packages"),
        ("pnpm store", home / "Library" / "pnpm", "Developer", "pnpm content store"),
        ("pip cache", home / "Library" / "Caches" / "pip", "Developer", "Cached Python downloads"),
        ("Cargo", home / ".cargo", "Developer", "Rust toolchain and crates"),
        ("Gradle", home / ".gradle", "Developer", "Gradle caches"),
        ("Tool cache", home / ".cache", "Developer", "Command-line tool caches"),
        ("Applications", Path("/Applications"), "This Mac", "Apps for every user"),
        ("System library", Path("/Library"), "This Mac", "Support files for all accounts"),
        ("Homebrew", Path("/opt/homebrew"), "This Mac", "Homebrew packages"),
        ("Local software", Path("/usr/local"), "This Mac", "Older Homebrew and local software"),
    ]
    places = [
        Place(label, path, group, hint)
        for label, path, group, hint in specs
        if path.exists()
    ]
    pictures = home / "Pictures"
    if pictures.is_dir():
        for library in sorted(pictures.glob("*.photoslibrary")):
            places.append(Place(library.stem, library, "Home", "Photos library"))
    return places


def parse_du_line(line: str) -> tuple[int, Path] | None:
    """Parse one `du -k` line into bytes and a path."""

    text = line.strip("\n")
    if not text:
        return None
    size_text, separator, raw_path = text.partition("\t")
    if not separator:
        parts = text.split(None, 1)
        if len(parts) != 2:
            return None
        size_text, raw_path = parts
    try:
        blocks = int(size_text.strip())
    except ValueError:
        return None
    if not raw_path:
        return None
    return blocks * 1024, Path(raw_path)


def cancel_running_processes() -> None:
    """Stop any `du` Diskclosure still has running. Used when the app quits."""

    with _PROCESS_LOCK:
        processes = list(_PROCESSES)
    for process in processes:
        if process.poll() is None:
            process.kill()


atexit.register(cancel_running_processes)


def stream_du(path: Path, max_depth: int, cancel: threading.Event):
    """Yield `(path, size_bytes)` as `du` prints each directory."""

    if cancel.is_set():
        raise ScanCancelled()
    process = subprocess.Popen(
        ["du", "-d", str(max_depth), "-k", "-x", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    with _PROCESS_LOCK:
        _PROCESSES.append(process)
    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
        except Exception:
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        while True:
            if cancel.is_set():
                raise ScanCancelled()
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            parsed = parse_du_line(line)
            if parsed is None:
                continue
            size, found = parsed
            yield found, size
    finally:
        if process.poll() is None:
            process.kill()
        if process.stdout is not None:
            process.stdout.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        reader.join(timeout=2)
        with _PROCESS_LOCK:
            if process in _PROCESSES:
                _PROCESSES.remove(process)


def measure_path(path: Path, cancel: threading.Event) -> int | None:
    """Allocated size of one folder, or None when it cannot be read."""

    target = path_aliases(path)
    found_size: int | None = None
    for found, size in stream_du(path, 0, cancel):
        if path_aliases(found) & target:
            found_size = size
    return found_size


def list_children(path: Path, cancel: threading.Event) -> tuple[int | None, list[ChildEntry]]:
    """Immediate children of a folder, with folder sizes including their contents."""

    if cancel.is_set():
        raise ScanCancelled()
    measured: dict[str, int] = {}
    total: int | None = None
    target_keys = path_aliases(path)
    for found, size in stream_du(path, 1, cancel):
        found_keys = path_aliases(found)
        if found_keys & target_keys:
            total = size
        else:
            for key in found_keys:
                measured[key] = size

    children: list[ChildEntry] = []
    try:
        entries = list(os.scandir(path))
    except OSError:
        return total, children

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
        if entry.is_symlink():
            children.append(ChildEntry(child, info.st_size, "alias", modified))
            continue
        if entry.is_dir(follow_symlinks=False):
            size = -1
            for key in path_aliases(child):
                if key in measured:
                    size = measured[key]
                    break
            children.append(ChildEntry(child, size, "folder", modified))
        elif entry.is_file(follow_symlinks=False):
            size = info.st_size
            if not size:
                for key in path_aliases(child):
                    if key in measured:
                        size = measured[key]
                        break
            children.append(ChildEntry(child, size, "file", modified))
        else:
            children.append(ChildEntry(child, info.st_size, "other", modified))
    return total, children


def scan_files(
    root: Path,
    cancel: threading.Event,
    on_progress,
    min_same_size: int = MIN_SAME_SIZE,
) -> tuple[list[FileHit], list[list[FileHit]], int]:
    """Walk `root` for the largest files and groups that share a size.

    `on_progress(files_seen, current_directory, largest_so_far)` may be called
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
            by_size[info.st_size].append(
                FileHit(Path(path), info.st_size, info.st_mtime)
            )

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
            if stat.S_ISLNK(info.st_mode) or info.st_dev != root_device:
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = os.path.join(directory, name)
            try:
                info = os.lstat(child)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or info.st_dev != root_device:
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


def trash_block_reason(path: Path, home: Path | None = None) -> str | None:
    """Why Diskclosure will not trash this path, or None when it is allowed."""

    if "\n" in str(path) or "\x00" in str(path):
        return "That path cannot be moved safely."
    try:
        if path.is_symlink():
            return "That item is a shortcut. Remove the shortcut in Finder if you are sure."
        if not path.exists():
            return "That item is already gone."
        real = path.resolve()
    except OSError as exc:
        return str(exc)

    home_root = (home or Path.home()).resolve()
    if real == Path("/") or real == home_root:
        return "That is your home folder or the disk itself."
    if real.parent == home_root and real.name in _PROTECTED_HOME_NAMES:
        return (
            f"{real.name} is a standard Mac folder. "
            "Open it and remove items inside it, rather than the folder itself."
        )
    if real == Path("/Applications"):
        return "The Applications folder itself has to stay."
    if real.name == ".Trash" and real.parent == home_root:
        return "Open Trash and remove items inside it, rather than the Trash folder."

    applications = Path("/Applications")
    under_home = _is_inside(real, home_root)
    under_applications = real != applications and _is_inside(real, applications)
    if not under_home and not under_applications:
        return "Diskclosure only moves items in your home folder or in /Applications to the Trash."
    return None


def move_to_trash(path: Path) -> None:
    """Ask Finder to move a path to the Trash. Raises TrashError on failure."""

    reason = trash_block_reason(path)
    if reason:
        raise TrashError(reason)
    script = (
        'tell application "Finder" to delete (POSIX file '
        + _apple_string(str(path))
        + ")"
    )
    completed = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "Finder could not move it.").strip()
        raise TrashError(detail)


def volume_name() -> str:
    try:
        completed = subprocess.run(
            ["diskutil", "info", "/"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Startup disk"
    for line in completed.stdout.splitlines():
        if "Volume Name:" in line:
            name = line.split(":", 1)[1].strip()
            if name:
                return name
    return "Startup disk"


def local_snapshot_count() -> int | None:
    """How many local Time Machine snapshots exist, if the system will say."""

    try:
        completed = subprocess.run(
            ["tmutil", "listlocalsnapshots", "/"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    count = sum(1 for line in completed.stdout.splitlines() if "com.apple." in line)
    return count


def path_aliases(path: Path) -> set[str]:
    """Path strings that can refer to the same folder on macOS (`/var` and `/private/var`)."""

    names = {os.path.normpath(str(path))}
    try:
        names.add(os.path.normpath(str(path.resolve())))
    except OSError:
        pass
    return names


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _apple_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
