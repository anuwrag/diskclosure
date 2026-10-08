import asyncio
import unittest

from textual.widgets import DataTable

from diskclosure.app import DiskclosureApp


class AppSmokeTests(unittest.TestCase):
    def test_common_folders_are_listed(self) -> None:
        asyncio.run(self._folders())

    async def _folders(self) -> None:
        app = DiskclosureApp(auto_scan=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#folder-table", DataTable)
            self.assertGreater(table.row_count, 5)
            labels = [str(table.get_cell_at((row, 0))) for row in range(table.row_count)]
            self.assertTrue(any("Downloads" in label for label in labels))
            self.assertTrue(any("Caches" in label for label in labels))
            downloads = next(item for item in app._stack[0].items if item.label == "Downloads")
            app._apply_sizes([(downloads.key, 1234)])
            self.assertEqual(downloads.size, 1234)
            selected = app._selected_item()
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertEqual(selected.kind, "folder")
            self.assertEqual(selected.label, "Desktop")


if __name__ == "__main__":
    unittest.main()
