"""Canonical filesystem locations for source and packaged portable mode.

A packaged build writes beside the executable; a source run writes in the
repository root. Directory helpers create their target on first use.
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def database_path() -> Path:
    return app_dir() / "bot_state.sqlite"


def logs_dir() -> Path:
    path = app_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    path = app_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backups_dir() -> Path:
    path = app_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path

def lock_file_path() -> Path:
    return app_dir() / "ibkr_trading_bot.lock"


def debug_captures_dir() -> Path:
    """Folder for per-trade market-data capture packages.

    Files are written only after the complete post-trade window has been kept
    in RAM. Incomplete captures are intentionally not flushed on shutdown.
    """
    path = app_dir() / "debug_captures"
    path.mkdir(parents=True, exist_ok=True)
    return path
