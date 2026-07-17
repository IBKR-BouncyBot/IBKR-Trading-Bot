# v3.0.5 final Ruff GUI import-order correction

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

The v3.0.4 Windows quality run passed all 350 pytest tests, all 18 CSV simulations, and Pyright, but Ruff still reported one `I001` import-block finding in `app/gui.py`.

The remaining ordering corrections are:

- `QTableWidget` and `QTableWidgetItem` precede `QTabWidget` in the `PySide6.QtWidgets` import.
- `choose_timestamp_for_display` precedes `clamp_fraction` in the local timeline-scaling import.

This patch changes import ordering and version metadata only. It does not change trading decisions, strategy mathematics, broker-order handling, persistence behavior, or GUI behavior.
