"""Record a short demo of Diskclosure for the README.

Frames are example data, same as the screenshots. Output is docs/demo.mp4.
"""

from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rich.cells import cell_len
from rich.console import Console
from textual.widgets import DataTable, TabbedContent

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from capture_screenshots import FOLDER_SIZES, GB, _sample_files  # noqa: E402
from diskclosure.app import DiskclosureApp, FileScan, TrashConfirm  # noqa: E402
OUT = ROOT / "docs" / "demo.mp4"
FPS = 10
DURATION = 10


def _render_image(app: DiskclosureApp) -> Image.Image:
    width = app.size.width
    height = app.size.height
    console = Console(width=width, height=height, force_terminal=True, color_system="truecolor")
    rendered = app.screen._compositor.render_update(
        full=True, screen_stack=app._background_screens, simplify=False
    )
    lines = [list(line) for line in console.render_lines(rendered, pad=True)]
    font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 16, index=0)
    bold = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 16, index=1)
    cell_w = max(1, math.ceil(font.getlength("M")), math.ceil(font.getlength("█")))
    ascent, descent = font.getmetrics()
    cell_h = ascent + descent + 2
    columns = max((_line_columns(line) for line in lines), default=width)
    pad = 16
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
    return image


def _line_columns(segments) -> int:
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


def _even_size(size: tuple[int, int]) -> tuple[int, int]:
    width, height = size
    return (width - (width % 2), height - (height % 2))


def _save(frame_dir: Path, index: int, image: Image.Image, size: tuple[int, int]) -> None:
    if image.size != size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    even = _even_size(size)
    if image.size != even:
        image = image.crop((0, 0, even[0], even[1]))
    image.save(frame_dir / f"frame_{index:04d}.png")


async def _record(frame_dir: Path) -> tuple[int, int]:
    app = DiskclosureApp(auto_scan=False)
    frames = 0
    canvas: tuple[int, int] | None = None

    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.resize_terminal(140, 42)
        await pilot.pause()

        used = app._disk_used or (500 * GB)
        scale = (used * 0.78) / FOLDER_SIZES["Home folder"]
        for item in app._stack[0].items:
            if item.label in FOLDER_SIZES:
                item.size = int(FOLDER_SIZES[item.label] * scale)
        app._catalog_sorted = True
        app._measuring = False
        app._render_folders()
        app._paint_status()
        await pilot.pause()

        table = app.query_one("#folder-table", DataTable)
        table.focus()
        await pilot.pause()

        # ~2.8s on common folders, cursor walking down.
        for row in range(1, 8):
            table.move_cursor(row=row)
            await pilot.pause()
            img = _render_image(app)
            canvas = canvas or _even_size(img.size)
            for _ in range(4):
                _save(frame_dir, frames, img, canvas)
                frames += 1

        largest, dupes = _sample_files()
        app._scan = FileScan(
            root=Path.home(),
            seen=184_320,
            done=True,
            running=False,
            largest=largest,
            dupes=dupes,
        )
        tabs = app.query_one(TabbedContent)
        tabs.active = "files"
        await pilot.pause()
        app._paint_status()
        await pilot.pause()
        file_table = app.query_one("#file-table", DataTable)
        file_table.focus()

        # ~2.8s on largest files.
        for row in range(0, 5):
            file_table.move_cursor(row=row)
            await pilot.pause()
            img = _render_image(app)
            canvas = canvas or img.size
            for _ in range(5):
                _save(frame_dir, frames, img, canvas)
                frames += 1

        tabs.active = "dupes"
        await pilot.pause()
        app._paint_status()
        await pilot.pause()
        dupe_table = app.query_one("#dupe-table", DataTable)
        dupe_table.focus()

        # ~2.4s on same-size copies.
        for row in (0, 1, 2, 3):
            dupe_table.move_cursor(row=row)
            await pilot.pause()
            img = _render_image(app)
            canvas = canvas or img.size
            for _ in range(6):
                _save(frame_dir, frames, img, canvas)
                frames += 1

        # ~2s trash confirmation.
        app.push_screen(TrashConfirm(largest[1]))
        await pilot.pause()
        img = _render_image(app)
        canvas = canvas or img.size
        hold = max(1, DURATION * FPS - frames)
        for _ in range(hold):
            _save(frame_dir, frames, img, canvas)
            frames += 1

    assert canvas is not None
    return canvas


def _encode(frame_dir: Path) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pattern = str(frame_dir / "frame_%04d.png")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            pattern,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-crf",
            "20",
            str(OUT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # Lightweight GIF for GitHub READMEs that cannot embed mp4.
    gif = OUT.with_suffix(".gif")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(OUT),
            "-vf",
            "fps=8,scale=960:-1:flags=lanczos",
            "-loop",
            "0",
            str(gif),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> None:
    frame_dir = Path(tempfile.mkdtemp(prefix="diskclosure-demo-"))
    try:
        asyncio.run(_record(frame_dir))
        _encode(frame_dir)
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)
    print(OUT)
    print(OUT.with_suffix(".gif"))
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(OUT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"duration {float(probe.stdout.strip()):.1f}s")


if __name__ == "__main__":
    main()
