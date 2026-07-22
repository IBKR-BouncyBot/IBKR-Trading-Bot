"""Headless import guard for GUI module-level constants.

The CI/container used for project review does not have PySide6 installed, so this
subprocess supplies minimal Qt stubs. The purpose is not to test rendering; it
catches import-time GUI regressions such as assuming normalized stage strings are
still Stage enum objects.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

STUB_IMPORT_CODE = r'''
import importlib
import sys
import types

class _QtFlag:
    def __or__(self, other):
        return self
    def __and__(self, other):
        return self
    def __invert__(self):
        return self
    def __bool__(self):
        return False

class _Qt:
    TextSelectableByMouse = _QtFlag()
    AlignCenter = _QtFlag()
    AlignLeft = _QtFlag()
    AlignTop = _QtFlag()
    AlignRight = _QtFlag()
    AlignBottom = _QtFlag()
    AlignVCenter = _QtFlag()
    AlignHCenter = _QtFlag()
    TextWordWrap = _QtFlag()
    Horizontal = _QtFlag()
    Vertical = _QtFlag()
    UserRole = _QtFlag()
    DisplayRole = _QtFlag()
    EditRole = _QtFlag()
    KeepAspectRatio = _QtFlag()
    SmoothTransformation = _QtFlag()
    NoPen = _QtFlag()
    class AlignmentFlag:
        pass
    def __getattr__(self, name):
        flag = _QtFlag()
        setattr(self, name, flag)
        return flag

class _Signal:
    def connect(self, *args, **kwargs):
        return None
    def emit(self, *args, **kwargs):
        return None

class _Dummy:
    def __init__(self, *args, **kwargs):
        pass
    def __call__(self, *args, **kwargs):
        return _Dummy()
    def __getattr__(self, name):
        if name in {"clicked", "currentTextChanged", "valueChanged", "textChanged", "timeout", "stateChanged", "toggled", "accepted", "rejected", "itemSelectionChanged", "cellDoubleClicked"}:
            sig = _Signal()
            setattr(self, name, sig)
            return sig
        def method(*args, **kwargs):
            return _Dummy()
        setattr(self, name, method)
        return method
    def __iter__(self):
        return iter(())
    def __bool__(self):
        return False
    def __or__(self, other):
        return self
    def __and__(self, other):
        return self
    def __add__(self, other):
        return self
    def __sub__(self, other):
        return self
    def __mul__(self, other):
        return self
    def __truediv__(self, other):
        return self
    def __float__(self):
        return 0.0
    def __int__(self):
        return 0

class _DummyMeta(type):
    def __getattr__(cls, name):
        return _QtFlag()

class Dummy(_Dummy, metaclass=_DummyMeta):
    pass

for modname in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]:
    sys.modules[modname] = types.ModuleType(modname)

qtcore = sys.modules["PySide6.QtCore"]
for name in "QPointF QRectF QTimer QSize QEvent QObject".split():
    setattr(qtcore, name, Dummy)
qtcore.Qt = _Qt()
qtcore.Signal = lambda *args, **kwargs: _Signal()

qtgui = sys.modules["PySide6.QtGui"]
for name in "QAction QColor QFont QPainter QPen QBrush".split():
    setattr(qtgui, name, Dummy)

qtwidgets = sys.modules["PySide6.QtWidgets"]
for name in "QApplication QCheckBox QComboBox QDialog QDialogButtonBox QFileDialog QFormLayout QFrame QGridLayout QGroupBox QHeaderView QHBoxLayout QLabel QLineEdit QMainWindow QMessageBox QPushButton QProgressBar QScrollArea QSizePolicy QSpinBox QTabWidget QTableWidget QTableWidgetItem QTextEdit QToolTip QVBoxLayout QWidget QDoubleSpinBox QAbstractSpinBox".split():
    setattr(qtwidgets, name, Dummy)

module = importlib.import_module("app.gui")
assert all(isinstance(stage, str) for stage in module.STAGE_ORDER), module.STAGE_ORDER
assert module.STAGE_ORDER[0] == "1_WAIT_INITIAL_DROP"
assert module.CycleAuditDialog._outcome_badge({
    "stage": module.Stage.CYCLE_COMPLETE.value,
    "error_message": "Old transient warning from imported database",
    "sell_filled_qty": 10,
    "avg_sell_price": 110.0,
    "net_pnl": 90.0,
}, {"cycle": {}}) == "PROFIT EXIT"

assert module._is_expected_guard_or_timing_blocker("Blocked outside RTH by user setting") is True
assert module._is_expected_guard_or_timing_blocker("RECOVERY REQUIRED: broker/local mismatch") is False

assert module._timestamp_after("2026-07-08T16:00:03+00:00", "2026-07-08T16:00:06+00:00", tolerance_seconds=2) is True
assert module._timestamp_after("2026-07-08T16:00:03+00:00", "2026-07-08T16:00:04+00:00", tolerance_seconds=2) is False
assert module._is_expected_operator_stop_message("Stop selected: strategy stopped locally; no broker order was cancelled or submitted.") is True

rth_snapshot = {
    "rth_open": True,
    "rth_status": {
        "is_open": True,
        "source": "contract_liquid_hours",
        "message": "RTH open in contract liquidHours window 0930-20260708:1600 US/Eastern.",
        "checked_at": "2026-07-08T18:38:46+00:00",
        "liquid_hours": "20260708:0930-20260708:1600",
        "time_zone": "US/Eastern",
    },
}
assert module._format_rth_status(rth_snapshot, short=True).startswith("RTH open - closes in ")
long_rth = module._format_rth_status(rth_snapshot)
assert "Regular hours 09:30" in long_rth and "16:00" in long_rth and "market close" in long_rth
early_close_snapshot = {
    "rth_open": True,
    "rth_status": {
        "is_open": True,
        "source": "contract_liquid_hours",
        "message": "RTH open on an early-close day.",
        "checked_at": "2026-07-03T16:50:00+00:00",
        "liquid_hours": "20260703:0930-20260703:1600",
        "time_zone": "America/New_York",
        "session_open": "2026-07-03T09:30:00-04:00",
        "session_close": "2026-07-03T13:00:00-04:00",
    },
}
early_close_text = module._format_rth_status(early_close_snapshot)
assert "Regular hours 09:30" in early_close_text and "13:00" in early_close_text
assert "16:00" not in early_close_text
flow = object.__new__(module.StrategyFlowchartWidget)
flow._strategy = module.StrategySettings()
flow._cycle = None
flow._price_snapshot = None
flow._cards = module.build_strategy_flowchart_cards(flow._strategy)
flow._view_mode = "Full strategy"
flow._compact_mode = True
assert len(flow._filtered_cards()) == 5

guard_permissions = module._recovery_action_permissions(
    has_cycle=True,
    startup_resume_required=False,
    startup_resume_only=False,
    recovery_required=False,
    action_state="waiting",
    expected_non_recovery_wait=True,
    open_order_count=0,
    has_working_local_order=False,
    open_qty=0.0,
    terminal_safe_stage=False,
    broker_refresh_current=True,
)
assert guard_permissions["ordinary_wait_only"] is True
assert guard_permissions["no_recovery_action_needed"] is True
for key in ("can_resume", "can_stop_cycle", "can_cancel_order", "can_market_close", "can_mark_manual", "can_leave_orders"):
    assert guard_permissions[key] is False, (key, guard_permissions)

cycle_complete = {
    "id": "cycle-1",
    "stage": module.Stage.CYCLE_COMPLETE.value,
    "buy_filled_qty": 10,
    "sell_filled_qty": 10,
    "protective_sell_filled_qty": 0,
    "sell_order_ref": "IBKRBOT|AAPL|CYCLE-1|SELL_TRAIL",
    "sell_order_id": 22,
    "sell_perm_id": 33,
    "sell_status": "Filled",
    "sell_filled_at": "2026-07-10T13:05:00+00:00",
    "updated_at": "2026-07-10T13:05:01+00:00",
}
stale_probe_order = {
    "order_ref": cycle_complete["sell_order_ref"],
    "order_id": 22,
    "perm_id": 33,
    "status": "Submitted",
    "filled": 0,
    "remaining": 10,
}
visible, superseded = module._reconciled_open_app_orders({
    "active_cycle": cycle_complete,
    "broker_recovery": {
        "cycle_id": "cycle-1",
        "checked_at": "2026-07-10T13:04:00+00:00",
        "open_app_orders": [stale_probe_order],
    },
})
assert visible == []
assert superseded == [stale_probe_order]

visible, superseded = module._reconciled_open_app_orders({
    "active_cycle": cycle_complete,
    "broker_recovery": {
        "cycle_id": "cycle-1",
        "checked_at": "2026-07-10T13:06:00+00:00",
        "open_app_orders": [stale_probe_order],
    },
})
assert visible == [stale_probe_order]
assert superseded == []
'''


def test_gui_module_imports_with_qt_stubs_for_stage_constants():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path.cwd())
    result = subprocess.run(
        [sys.executable, "-c", STUB_IMPORT_CODE],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
