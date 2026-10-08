import tempfile
import threading
import unittest
from pathlib import Path

from diskclosure.scanner import (
    format_size,
    list_children,
    parse_du_line,
    scan_files,
    share_text,
    trash_block_reason,
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

    def test_parse_du_line(self) -> None:
        parsed = parse_du_line("12\t/Users/me/Desktop\n")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        size, path = parsed
        self.assertEqual(size, 12 * 1024)
        self.assertEqual(path, Path("/Users/me/Desktop"))
        self.assertIsNone(parse_du_line("not a line"))


class TrashPolicyTests(unittest.TestCase):
    def test_blocks_home_and_system_paths(self) -> None:
        home = Path.home()
        self.assertIsNotNone(trash_block_reason(home))
        self.assertIsNotNone(trash_block_reason(Path("/")))
        self.assertIsNotNone(trash_block_reason(Path("/System")))
        self.assertIsNotNone(trash_block_reason(home / "Desktop"))
        self.assertIsNotNone(trash_block_reason(home / "Library"))
        self.assertIsNotNone(trash_block_reason(Path("/Applications")))

    def test_allows_a_file_inside_a_home_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            downloads = home / "Downloads"
            downloads.mkdir()
            target = downloads / "diskclosure-test.txt"
            target.write_text("safe to consider")
            self.assertIsNone(trash_block_reason(target, home=home))
            self.assertIsNotNone(trash_block_reason(downloads, home=home))

    def test_blocks_a_missing_path_and_a_symlink(self) -> None:
        missing = Path.home() / "Downloads" / "diskclosure-does-not-exist.txt"
        self.assertIn("already gone", trash_block_reason(missing) or "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            folder = home / "Downloads"
            folder.mkdir()
            link = folder / "link"
            link.symlink_to(folder / "missing-target")
            self.assertIn("shortcut", trash_block_reason(link, home=home) or "")


class WalkTests(unittest.TestCase):
    def test_list_children_includes_files_and_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_bytes(b"x" * 3000)
            nested = root / "folder"
            nested.mkdir()
            (nested / "inside.txt").write_bytes(b"y" * 8000)
            total, children = list_children(root, threading.Event())
            names = {child.path.name: child for child in children}
            self.assertIn("notes.txt", names)
            self.assertIn("folder", names)
            self.assertGreater(names["folder"].size, 0)
            self.assertIsNotNone(total)
            assert total is not None
            self.assertGreater(total, 0)

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
        from diskclosure.scanner import ScanCancelled, stream_du

        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ScanCancelled):
            list(stream_du(Path.home(), 0, cancel))


if __name__ == "__main__":
    unittest.main()
