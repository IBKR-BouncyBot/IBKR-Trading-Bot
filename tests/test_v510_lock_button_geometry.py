"""Native Qt geometry regression, isolated from the suite's Qt doubles."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GEOMETRY_PROBE = r'''
import json

from PySide6.QtWidgets import QApplication

from app.gui import LiveStatusBar, MainWindow, _apply_fusion_application_palette

app = QApplication([])
results = []
for dark in (False, True):
    _apply_fusion_application_palette(app, dark)
    bar = LiveStatusBar()
    MainWindow._apply_styles(bar)
    bar.pills["Account"].set_value("A longer account status that wraps when narrowed")
    bar.pills["Trading"].set_detail("12345.67 / 12400.00")
    bar.show()
    for width in (1600, 900):
        for locked in (False, True):
            bar.input_lock_btn.setChecked(locked)
            bar.set_input_lock_state(locked)
            bar.resize(width, 110)
            app.processEvents()
            bar.layout().activate()
            app.processEvents()
            lock = bar.input_lock_btn.geometry()
            pills = [pill.geometry() for pill in bar.pills.values()]
            results.append({
                "dark": dark, "width": width, "locked": locked,
                "lock_height": lock.height(), "lock_top": lock.top(),
                "pill_heights": [rect.height() for rect in pills],
                "pill_tops": [rect.top() for rect in pills],
                "checked": bar.input_lock_btn.isChecked(),
            })
    bar.close()
    bar.deleteLater()
    app.processEvents()
print(json.dumps(results))
'''


class LockButtonGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = subprocess.run(
            [sys.executable, "-c", (
                "try:\n"
                " import PySide6.QtWidgets\n"
                "except ModuleNotFoundError as exc:\n"
                " if str(exc.name).startswith('PySide6'):\n"
                "  raise SystemExit(77)\n"
                " raise\n"
            )],
            capture_output=True, text=True, cwd=ROOT, timeout=30, check=False,
        )
        if probe.returncode == 77:
            raise unittest.SkipTest("Native PySide6 is unavailable; geometry requires the real Qt layout engine.")
        if probe.returncode:
            raise AssertionError(probe.stdout + probe.stderr)

    def test_lock_matches_status_row_after_resize_theme_and_dpi_changes(self):
        for scale in ("1", "1.5", "2"):
            with self.subTest(scale=scale):
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale)
                result = subprocess.run(
                    [sys.executable, "-c", GEOMETRY_PROBE],
                    capture_output=True, text=True, cwd=ROOT, env=env, timeout=45, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows = json.loads(result.stdout)
                self.assertEqual(len(rows), 8)
                for row in rows:
                    self.assertGreater(row["lock_height"], 0, row)
                    self.assertEqual(row["pill_heights"], [row["lock_height"]] * 10, row)
                    self.assertEqual(row["pill_tops"], [row["lock_top"]] * 10, row)
                    self.assertEqual(row["checked"], row["locked"], row)


if __name__ == "__main__":
    unittest.main()
