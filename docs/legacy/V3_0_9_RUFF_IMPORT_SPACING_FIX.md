# v3.0.9 Ruff import-block spacing correction

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

The v3.0.8 Windows quality run passed 369 pytest cases, all 18 CSV simulation scenarios, and Pyright, but Ruff reported one `I001` issue in `tests/test_v308_gui_guards_atr_position_scope.py`.

The import names and modules were already ordered correctly. The file had two blank lines between the final import and the first module-level constant. Ruff's import organizer expects one blank line at that boundary. v3.0.9 removes the extra blank line and adds a focused regression test for the boundary.

This patch changes test-source formatting and release metadata only. It does not change trading logic, GUI behavior, broker integration, persistence, or account routing.
