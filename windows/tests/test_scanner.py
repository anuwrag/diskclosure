import os
import tempfile
import threading
import unittest
from pathlib import Path

from windisk.scanner import (
    ScanCancelled,
    describe_path,
    format_size,
    list_children,
    scan_files,
    share_text,
    trash_block_reason,
    tree_sizes,
)


class FormatTests(unittest.TestCase):
    def test_decimal_units(self) -> None:
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(999), "999 B")
        self.assertEqual(format_size(1500), "1.5 KB")
        self.assertEqual(format_size(None), "…")
        self.assertEqual(format_size(-1), "no access")

    def test_share_bar_is_bounded(self) -> None:
        text = share_text(50, 100)
        self.assertIn("50%", text)
        self.assertEqual(share_text(None, 100), "")
        self.assertIn("100%", share_text(500, 100))

    def test_describe_windows_kinds(self) -> None:
        self.assertEqual(describe_path(Path("setup.exe"), "file"), "program")
        self.assertEqual(describe_path(Path("setup.msi"), "file"), "installer")
        self.assertEqual(describe_path(Path("photos.zip"), "file"), "archive")
        self.assertEqual(describe_path(Path("notes"), "folder"), "folder")


class TrashPolicyTests(unittest.TestCase):
    def test_blocks_profile_drive_root_and_standard_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            desktop = home / "Desktop"
            desktop.mkdir()
            self.assertIn("user profile", trash_block_reason(home, home=home) or "")
            self.assertIn("standard Windows folder", trash_block_reason(desktop, home=home) or "")
        self.assertIn("disk itself", trash_block_reason(Path(Path.home().anchor)) or "")

    def test_allows_a_file_inside_a_profile_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            downloads = home / "Downloads"
            downloads.mkdir()
            target = downloads / "old-installer.exe"
            target.write_text("safe to consider")
            self.assertIsNone(trash_block_reason(target, home=home, blocked_roots=[]))
            self.assertIsNotNone(trash_block_reason(downloads, home=home, blocked_roots=[]))

    def test_blocks_system_trees_recycle_bin_and_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "Users" / "me"
            windows = root / "Windows"
            program = windows / "System32" / "kernel32.dll"
            program.parent.mkdir(parents=True)
            program.write_text("system")
            home.mkdir(parents=True)
            self.assertIn("Windows", trash_block_reason(program, home=home, blocked_roots=[windows]) or "")

            recycle = root / "$Recycle.Bin" / "S-1" / "gone.txt"
            recycle.parent.mkdir(parents=True)
            recycle.write_text("deleted")
            self.assertIn("Recycle Bin", trash_block_reason(recycle, home=home, blocked_roots=[]) or "")

            link = home / "shortcut"
            link.symlink_to(program)
            self.assertIn("shortcut", trash_block_reason(link, home=home, blocked_roots=[]) or "")

        missing = Path.home() / "Downloads" / "windisk-does-not-exist.txt"
        self.assertIn("already gone", trash_block_reason(missing, blocked_roots=[]) or "")


class WalkTests(unittest.TestCase):
    def test_tree_sizes_sum_nested_files_and_skip_linked_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "profile"
            root.mkdir()
            (root / "notes.txt").write_bytes(b"x" * 3000)
            nested = root / "folder"
            nested.mkdir()
            (nested / "inside.txt").write_bytes(b"y" * 8000)
            elsewhere = base / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / "big.bin").write_bytes(b"z" * 5000)
            linked = root / "linked"
            os.symlink(elsewhere, linked, target_is_directory=True)

            totals = tree_sizes(root, threading.Event())
            root_size = totals[os.path.normcase(os.path.normpath(str(root)))]
            self.assertEqual(root_size, 3000 + 8000)
            self.assertNotIn(os.path.normcase(os.path.normpath(str(linked))), totals)

            total, children = list_children(root, threading.Event())
            names = {child.path.name: child for child in children}
            self.assertEqual(names["notes.txt"].size, 3000)
            self.assertEqual(names["folder"].size, 8000)
            self.assertEqual(names["linked"].kind, "alias")
            self.assertEqual(total, 3000 + 8000)

    def test_scan_files_orders_by_size_and_groups_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"abc" * 20
            (root / "one.bin").write_bytes(payload)
            (root / "two.bin").write_bytes(payload)
            (root / "bigger.bin").write_bytes(b"z" * 200)
            largest, groups, seen = scan_files(
                root,
                threading.Event(),
                lambda *_args: None,
                min_same_size=len(payload),
            )
            self.assertEqual(seen, 3)
            self.assertEqual(largest[0].path.name, "bigger.bin")
            self.assertEqual(len(groups), 1)
            self.assertEqual({hit.path.name for hit in groups[0]}, {"one.bin", "two.bin"})

    def test_cancel_before_start(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ScanCancelled):
                tree_sizes(Path(directory), cancel)


if __name__ == "__main__":
    unittest.main()
