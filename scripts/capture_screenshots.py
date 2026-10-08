"""Render README screenshots from the real interface.

File names in the file lists are examples. The disk summary is this Mac.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from pathlib import Path

from textual.widgets import TabbedContent

from diskclosure.app import DiskclosureApp, FileScan, Item, TrashConfirm
from diskclosure.scanner import describe_path, display_path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"

GB = 1_000_000_000
FOLDER_SIZES = {
    "Desktop": int(6.4 * GB),
    "Documents": int(12.8 * GB),
    "Downloads": int(42.0 * GB),
    "Movies": int(18.6 * GB),
    "Music": int(4.2 * GB),
    "Pictures": int(96.0 * GB),
    "Photos Library": int(88.4 * GB),
    "Public": int(0.2 * GB),
    "Your Applications": int(1.1 * GB),
    "Home folder": int(410.0 * GB),
    "Library": int(186.0 * GB),
    "Caches": int(22.4 * GB),
    "Logs": int(0.4 * GB),
    "Application Support": int(48.0 * GB),
    "Containers": int(27.5 * GB),
    "Group Containers": int(6.1 * GB),
    "Mail": int(3.8 * GB),
    "Messages": int(5.2 * GB),
    "iCloud Drive": int(24.0 * GB),
    "iOS Backups": int(16.7 * GB),
    "Trash": int(2.4 * GB),
    "Developer": int(71.0 * GB),
    "Xcode DerivedData": int(28.0 * GB),
    "Xcode Archives": int(9.4 * GB),
    "iOS DeviceSupport": int(11.2 * GB),
    "Simulator": int(19.8 * GB),
    "Docker": int(22.0 * GB),
    "npm cache": int(1.6 * GB),
    "pnpm store": int(3.1 * GB),
    "pip cache": int(0.8 * GB),
    "Cargo": int(2.2 * GB),
    "Gradle": int(1.4 * GB),
    "Tool cache": int(0.6 * GB),
    "Applications": int(34.0 * GB),
    "System library": int(8.5 * GB),
    "Homebrew": int(7.9 * GB),
    "Local software": int(1.2 * GB),
}


def _file(path: Path, size: int, when: str, copies: int = 0, checksum: str = "") -> Item:
    return Item(
        key=f"{'dupe' if copies else 'file'}:{path}",
        label=display_path(path),
        path=path,
        size=size,
        kind="file",
        hint=describe_path(path, "file"),
        mtime=datetime.fromisoformat(when).timestamp(),
        copies=copies,
        checksum=checksum,
    )


def _sample_files() -> tuple[list[Item], list[Item]]:
    home = Path.home()
    largest = [
        _file(home / "Library/Developer/Xcode/iOS DeviceSupport/iPhone", int(11.2 * GB), "2026-08-02 14:10"),
        _file(home / "Downloads/Xcode_16.4.dmg", int(8.6 * GB), "2026-07-18 09:41"),
        _file(home / "Library/Containers/com.docker.docker/Data/vms/0/data.raw", int(22.0 * GB), "2026-09-12 21:06"),
        _file(home / "Library/Application Support/MobileSync/Backup/device/Manifest.db", int(6.4 * GB), "2026-03-01 11:20"),
        _file(home / "Movies/Screen Recording.mov", int(4.8 * GB), "2026-05-12 16:44"),
        _file(home / "Downloads/Docker.dmg", int(1.7 * GB), "2025-11-02 08:15"),
        _file(home / "Library/Caches/com.apple.Music/Cache.db", int(1.1 * GB), "2026-09-30 19:02"),
    ]
    dupes = [
        _file(home / "Downloads/Xcode_16.4.dmg", int(8.6 * GB), "2026-07-18 09:41", copies=2, checksum="identical"),
        _file(home / "Downloads/Installers/Xcode_16.4.dmg", int(8.6 * GB), "2026-07-18 09:44", copies=2, checksum="identical"),
        _file(home / "Movies/Talk.mov", int(4.8 * GB), "2026-05-12 16:44", copies=2, checksum="different"),
        _file(home / "Desktop/Talk-export.mov", int(4.8 * GB), "2026-05-12 16:50", copies=2, checksum="different"),
        _file(home / "Downloads/node-v22.pkg", 80 * 1_000_000, "2026-01-09 12:00", copies=2, checksum="—"),
        _file(home / "Downloads/Archive/node-v22.pkg", 80 * 1_000_000, "2026-01-09 12:05", copies=2, checksum="—"),
    ]
    return largest, dupes


async def _capture() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = DiskclosureApp(auto_scan=False)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.resize_terminal(140, 42)
        await pilot.pause()
        missing = [item.label for item in app._stack[0].items if item.label not in FOLDER_SIZES]
        if missing:
            raise SystemExit(f"Add screenshot sizes for: {', '.join(missing)}")
        used = app._disk_used or (500 * GB)
        scale = (used * 0.78) / FOLDER_SIZES["Home folder"]
        for item in app._stack[0].items:
            item.size = int(FOLDER_SIZES[item.label] * scale)
        app._catalog_sorted = True
        app._measuring = False
        app._render_folders()
        app._paint_status()
        await pilot.pause()
        _write(app, "folders.png")

        largest, dupes = _sample_files()
        app._scan = FileScan(
            root=Path.home(),
            seen=184_320,
            done=True,
            running=False,
            largest=largest,
            dupes=dupes,
        )
        app.query_one(TabbedContent).active = "files"
        await pilot.pause()
        app._paint_status()
        await pilot.pause()
        _write(app, "largest-files.png")

        app.query_one(TabbedContent).active = "dupes"
        await pilot.pause()
        app._paint_status()
        await pilot.pause()
        _write(app, "same-size.png")

        app.push_screen(TrashConfirm(largest[1]))
        await pilot.pause()
        _write(app, "trash.png")


def _write(app: DiskclosureApp, name: str) -> None:
    destination = OUT / name.replace(".svg", ".png")
    _render_png(app, destination)
    print(destination)


def _render_png(app: DiskclosureApp, destination: Path) -> None:
    """Draw the live screen with Menlo so the PNG keeps box-drawing characters."""

    from PIL import Image, ImageDraw, ImageFont
    from rich.cells import cell_len
    from rich.console import Console

    width = app.size.width
    height = app.size.height
    console = Console(
        width=width,
        height=height,
        force_terminal=True,
        color_system="truecolor",
    )
    rendered = app.screen._compositor.render_update(
        full=True, screen_stack=app._background_screens, simplify=False
    )
    lines = [list(line) for line in console.render_lines(rendered, pad=True)]
    font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 18, index=0)
    bold = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 18, index=1)
    cell_w = max(1, math.ceil(font.getlength("M")), math.ceil(font.getlength("█")))
    ascent, descent = font.getmetrics()
    cell_h = ascent + descent + 2
    columns = max((_line_columns(line) for line in lines), default=width)
    pad = 18
    image = Image.new(
        "RGB",
        (pad * 2 + columns * cell_w, pad * 2 + len(lines) * cell_h),
        (18, 20, 24),
    )
    draw = ImageDraw.Draw(image)
    for row, segments in enumerate(lines):
        column = 0
        for segment in segments:
            if segment.style is None or not segment.text or segment.text.startswith("\x1b"):
                continue
            style = segment.style
            background = _rgb(style.bgcolor, (18, 20, 24))
            foreground = _rgb(style.color, (232, 234, 237))
            if style.reverse:
                foreground, background = background, foreground
            face = bold if style.bold else font
            for char in segment.text:
                cols = max(1, cell_len(char))
                x = pad + column * cell_w
                y = pad + row * cell_h
                draw.rectangle((x, y, x + cols * cell_w, y + cell_h), fill=background)
                draw.text((x, y), char, font=face, fill=foreground)
                column += cols
    image.save(destination, "PNG")


def _line_columns(segments) -> int:
    from rich.cells import cell_len

    total = 0
    for segment in segments:
        if segment.style is None or not segment.text or segment.text.startswith("\x1b"):
            continue
        total += cell_len(segment.text)
    return total


def _rgb(color, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if color is None or getattr(color, "is_default", False):
        return default
    try:
        triplet = color.get_truecolor()
    except Exception:
        return default
    return (triplet.red, triplet.green, triplet.blue)


def main() -> None:
    asyncio.run(_capture())


if __name__ == "__main__":
    main()
