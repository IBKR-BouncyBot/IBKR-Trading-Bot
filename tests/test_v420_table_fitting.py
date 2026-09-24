"""Exercise fitted-table geometry using measured rows and distinct Qt policies.

These checks run with unittest or pytest and require no Qt rendering backend.
They verify content reachability; real screen/DPI layout needs visual testing.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.qt_stubs import imported_gui_with_stubs


class MeasuredTable:
    def __init__(self, rows: list[int], *, horizontal_policy: int = 2, scrollbar_height: int = 14):
        self.rows = rows
        self.horizontal_policy = horizontal_policy
        self.scrollbar_height = scrollbar_height
        self.minimum_height = 0
        self.maximum_height = 0
        self.vertical_policy = None
        self.resize_count = 0

    def resizeRowsToContents(self):
        self.resize_count += 1

    def rowCount(self):
        return len(self.rows)

    def rowHeight(self, row):
        return self.rows[row]

    def horizontalHeader(self):
        return SimpleNamespace(height=lambda: 30)

    def frameWidth(self):
        return 1

    def horizontalScrollBarPolicy(self):
        return self.horizontal_policy

    def horizontalScrollBar(self):
        return SimpleNamespace(
            isVisible=lambda: False,
            sizeHint=lambda: SimpleNamespace(height=lambda: self.scrollbar_height),
        )

    def setMinimumHeight(self, value):
        self.minimum_height = value

    def setMaximumHeight(self, value):
        self.maximum_height = value

    def setVerticalScrollBarPolicy(self, value):
        self.vertical_policy = value

    def setHorizontalScrollBarPolicy(self, value):
        self.horizontal_policy = value

    def setSizePolicy(self, *values):
        self.size_policy = values


class TableFittingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        policies = SimpleNamespace(ScrollBarAlwaysOff=1, ScrollBarAsNeeded=2, ScrollBarAlwaysOn=3)
        self.qt_patch = patch.object(self.gui, "Qt", policies)
        self.qt_patch.start()
        self.addCleanup(self.qt_patch.stop)

    def test_hidden_horizontal_scrollbar_space_preserves_last_row(self):
        for scrollbar_height in (14, 28):
            with self.subTest(scrollbar_height=scrollbar_height):
                table = MeasuredTable([26] * 8, scrollbar_height=scrollbar_height)
                self.gui._fit_table_height_to_rows(table, max_visible_rows=8)
                self.assertGreaterEqual(table.minimum_height - 30 - 2 - scrollbar_height, sum(table.rows))
                self.assertEqual(table.resize_count, 1)

    def test_disabled_horizontal_scrollbar_does_not_reserve_space(self):
        table = MeasuredTable([30] * 8, horizontal_policy=1)
        self.gui._fit_table_height_to_all_rows(table, max_height=None)
        height_without_bar = table.minimum_height
        table.horizontal_policy = 2
        self.gui._fit_table_height_to_all_rows(table, max_height=None)
        self.assertEqual(table.minimum_height - height_without_bar, table.scrollbar_height)

    def test_uncapped_all_rows_remain_visible_with_tall_wrapped_content(self):
        table = MeasuredTable([84, 96, 120, 108, 96, 84, 108, 96], horizontal_policy=1)
        self.gui._fit_table_height_to_all_rows(table, max_height=None)
        self.assertGreater(table.minimum_height, 560)
        self.assertGreaterEqual(table.minimum_height - 30 - 2, sum(table.rows))
        self.assertEqual(table.vertical_policy, 1)

    def test_capped_all_rows_keep_overflow_reachable(self):
        table = MeasuredTable([100] * 8)
        self.gui._fit_table_height_to_all_rows(table, max_height=230)
        self.assertEqual(table.minimum_height, 230)
        self.assertEqual(table.maximum_height, 234)
        self.assertEqual(table.vertical_policy, 2)

    def test_scrollbar_reservation_counts_toward_capped_content(self):
        table = MeasuredTable([30] * 4, scrollbar_height=28)
        self.gui._fit_table_height_to_all_rows(table, max_height=165)
        self.assertEqual(table.vertical_policy, 2)

    def test_refit_after_rows_shrink_removes_unneeded_vertical_scrolling(self):
        table = MeasuredTable([100] * 8)
        self.gui._fit_table_height_to_all_rows(table)
        self.assertEqual(table.vertical_policy, 2)
        table.rows = [26] * 8
        self.gui._fit_table_height_to_all_rows(table)
        self.assertEqual(table.vertical_policy, 1)
        self.assertLess(table.maximum_height, 560)


if __name__ == "__main__":
    unittest.main()
