"""v3.9.0 reviewed local-fix and fail-closed regression coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import PolledOrderState
from app.models import CycleState, Stage
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_controller_headless import _install_qt_stub

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _sell_cycle(controller: Any, *, quantity: int = 100, order_kind: str = "SELL_TRAIL") -> CycleState:
    settings = permissive_strategy(auto_repeat=False)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.buy_filled_qty = quantity
    cycle.quantity = quantity
    cycle.avg_buy_price = 100.0
    cycle.buy_commission = 1.0
    cycle.sell_order_ref = f"IBKRBOT|AAPL|CYCLE-000001|TEST|{order_kind}"
    cycle.sell_order_id = 77
    cycle.sell_perm_id = 88
    cycle.sell_status = "Submitted"
    controller.strategy = settings
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller._start_trade_market_data_capture = lambda *args, **kwargs: None
    controller._maybe_start_next_cycle = lambda: None
    return cycle


def _sell_poll(
    cycle: CycleState,
    *,
    status: str,
    filled: int,
    remaining: int,
    price: float = 103.0,
) -> PolledOrderState:
    return PolledOrderState(
        order_ref=str(cycle.sell_order_ref),
        order_id=cycle.sell_order_id,
        perm_id=cycle.sell_perm_id,
        status=status,
        filled=filled,
        remaining=remaining,
        avg_fill_price=price,
        commission=0.6,
        executions=[],
        raw={"status": status, "filled": filled, "remaining": remaining},
    )


def _protective_cycle(controller: Any, *, quantity: int = 100) -> CycleState:
    settings = permissive_strategy(auto_repeat=False)
    settings.protective_sell_enabled = True
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = quantity
    cycle.quantity = quantity
    cycle.avg_buy_price = 100.0
    cycle.protective_sell_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|PROTECTIVE_SELL_TRAIL"
    cycle.protective_sell_order_id = 79
    cycle.protective_sell_perm_id = 89
    cycle.protective_sell_status = "Submitted"
    controller.strategy = settings
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller._start_trade_market_data_capture = lambda *args, **kwargs: None
    controller._maybe_start_next_cycle = lambda: None
    return cycle


def test_terminal_protective_full_fill_with_reported_remainder_fails_closed(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "protective-remainder.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _protective_cycle(controller)
    polled = PolledOrderState(
        order_ref=str(cycle.protective_sell_order_ref),
        order_id=cycle.protective_sell_order_id,
        perm_id=cycle.protective_sell_perm_id,
        status="Filled",
        filled=100,
        remaining=1,
        avg_fill_price=95.0,
        commission=0.5,
        executions=[],
        raw={"status": "Filled", "filled": 100, "remaining": 1},
    )

    handled = controller._handle_protective_sell_order_poll(cycle, polled)

    assert handled is True
    assert controller.active_cycle.stage == Stage.ERROR
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "PROTECTIVE_SELL_QUANTITY_MISMATCH" for row in events)


def test_final_sell_nonterminal_partial_persists_execution_and_waits(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "sell-partial.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _sell_cycle(controller)

    controller._handle_sell_order_poll(
        cycle,
        _sell_poll(cycle, status="Submitted", filled=40, remaining=60),
    )

    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert controller.active_cycle.sell_filled_qty == 40
    totals = controller.storage.get_execution_totals(cycle.id, "SELL")
    assert totals["shares"] == pytest.approx(40.0)
    assert totals["avg_price"] == pytest.approx(103.0)


@pytest.mark.parametrize("remaining", [0, 60])
def test_final_sell_terminal_partial_never_completes(
    controller_module: Any,
    tmp_path: Path,
    remaining: int,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / f"sell-terminal-partial-{remaining}.sqlite")
    )
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _sell_cycle(controller)

    controller._handle_sell_order_poll(
        cycle,
        _sell_poll(cycle, status="Cancelled", filled=40, remaining=remaining),
    )

    assert controller.active_cycle.stage == Stage.ERROR
    assert controller.active_cycle.sell_filled_qty == 40
    assert "60 shares remain" in str(controller.active_cycle.error_message)
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "SELL_PARTIAL_TERMINAL" for row in events)


def test_terminal_sell_with_full_fill_and_reported_remainder_fails_closed(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "sell-full-remainder.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _sell_cycle(controller)

    controller._handle_sell_order_poll(
        cycle,
        _sell_poll(cycle, status="Filled", filled=100, remaining=1),
    )

    assert controller.active_cycle.stage == Stage.ERROR
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "SELL_QUANTITY_MISMATCH" for row in events)


def test_exact_sell_quantity_without_a_fill_price_fails_closed(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "sell-missing-price.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _sell_cycle(controller)

    controller._handle_sell_order_poll(
        cycle,
        _sell_poll(cycle, status="Filled", filled=100, remaining=0, price=0.0),
    )

    assert controller.active_cycle.stage == Stage.ERROR
    assert "average price of 0.0000" in str(controller.active_cycle.error_message)
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "SELL_QUANTITY_MISMATCH" for row in events)


def test_terminal_forced_market_partial_with_remainder_cannot_stall(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "forced-partial.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _sell_cycle(controller, order_kind="FORCED_SELL_MARKET")
    cycle.close_position_market_requested = True
    controller.storage.upsert_cycle(cycle)

    controller._handle_sell_order_poll(
        cycle,
        _sell_poll(cycle, status="Cancelled", filled=40, remaining=60),
    )

    assert controller.active_cycle.stage == Stage.ERROR
    assert controller.active_cycle.close_position_market_requested is True
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "SELL_PARTIAL_TERMINAL" for row in events)


def test_manual_market_close_cancels_a_working_partially_filled_sell_before_replacement(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy(auto_repeat=False)
    controller = make_controller(controller_module, tmp_path / "partial-close-cancel.sqlite", broker, settings)
    cycle = _sell_cycle(controller, quantity=10)
    partial = _sell_poll(cycle, status="Submitted", filled=4, remaining=6)
    broker.orders[str(cycle.sell_order_ref)] = partial
    controller._handle_sell_order_poll(cycle, partial)

    controller._request_market_close_for_app_position(controller.active_cycle)

    assert broker.cancelled_orders == [str(cycle.sell_order_ref)]
    assert not [row for row in broker.placed_orders if row.get("order_type") == "MKT"]
    assert controller.active_cycle.close_position_market_requested is True
    assert controller.active_cycle.sell_status == "CancelRequested"


def test_forced_market_fill_completes_only_from_exact_aggregate_cycle_executions(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "forced-aggregate.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _sell_cycle(controller, order_kind="FORCED_SELL_MARKET")
    cycle.close_position_market_requested = True
    cycle.sell_filled_qty = 40
    controller.storage.upsert_cycle(cycle)
    controller.storage.upsert_execution(
        cycle=cycle,
        ticker=cycle.ticker,
        side="SELL",
        shares=40,
        price=103.0,
        avg_price=103.0,
        commission=0.4,
        currency=cycle.currency,
        order_ref="IBKRBOT|AAPL|CYCLE-000001|TEST|SELL_TRAIL",
        order_id=70,
        perm_id=80,
        execution_id="prior-sell-40",
        executed_at="2026-07-29T12:00:00+00:00",
        raw={"source": "prior partial"},
    )

    controller._handle_sell_order_poll(
        cycle,
        _sell_poll(cycle, status="Filled", filled=60, remaining=0, price=104.0),
    )

    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.active_cycle.sell_filled_qty == 100
    assert controller.active_cycle.avg_sell_price == pytest.approx(103.6)
    assert controller.active_cycle.sell_commission == pytest.approx(1.0)
    assert controller.active_cycle.net_pnl == pytest.approx(358.0)


def test_forced_market_fill_without_provable_prior_execution_fails_closed(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "forced-unproven.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _sell_cycle(controller, order_kind="FORCED_SELL_MARKET")
    cycle.close_position_market_requested = True
    cycle.sell_filled_qty = 40
    controller.storage.upsert_cycle(cycle)

    controller._handle_sell_order_poll(
        cycle,
        _sell_poll(cycle, status="Filled", filled=60, remaining=0, price=104.0),
    )

    assert controller.active_cycle.stage == Stage.ERROR
    assert "60 of 100 app-owned shares were sold" in str(controller.active_cycle.error_message)


def test_numeric_history_items_sort_by_raw_value_not_formatted_text() -> None:
    with imported_gui_with_stubs(ROOT) as gui:
        loss = gui._numeric_table_item("-$1,200.00", -1200.0)
        small = gui._numeric_table_item("$9.50", 9.5)
        medium = gui._numeric_table_item("$85.00", 85.0)
        large = gui._numeric_table_item("$1,050.00", 1050.0)
        invalid = gui._numeric_table_item("—", float("nan"))

        assert loss < small < medium < large
        assert invalid < loss
        assert not large < medium


def test_cycle_audit_read_failure_is_reported_without_opening_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT) as gui:
        warnings: list[tuple[Any, ...]] = []
        dialogs: list[Any] = []
        monkeypatch.setattr(gui.QMessageBox, "warning", lambda *args: warnings.append(args))
        monkeypatch.setattr(gui, "CycleAuditDialog", lambda *args, **kwargs: dialogs.append((args, kwargs)))

        def fail_details(_cycle_id: str) -> dict[str, Any]:
            raise OSError("database is locked")

        window = object.__new__(gui.MainWindow)
        window.controller = SimpleNamespace(get_cycle_audit_details=fail_details)
        window._visible_history_rows = [{"id": "cycle-1", "ticker": "AAPL"}]
        window.history_table = gui.QTableWidget()
        item = gui.QTableWidgetItem("AAPL")
        item.setData(gui.Qt.UserRole, 0)
        window.history_table.setItem(0, 0, item)

        gui.MainWindow._history_row_clicked(window, 0, 0)

        assert warnings
        assert "database is locked" in str(warnings[0][-1])
        assert dialogs == []


def test_history_export_failure_is_reported_without_success_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT) as gui:
        warnings: list[tuple[Any, ...]] = []
        information: list[tuple[Any, ...]] = []
        monkeypatch.setattr(gui.QMessageBox, "warning", lambda *args: warnings.append(args))
        monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: information.append(args))

        def fail_export(_ticker: str) -> Path:
            raise PermissionError("portable folder is read-only")

        window = object.__new__(gui.MainWindow)
        window.controller = SimpleNamespace(export_history=fail_export)
        window.history_ticker_filter = gui.QLineEdit()
        window.history_ticker_filter.setText("AAPL")

        gui.MainWindow._export_history(window)

        assert warnings
        assert "read-only" in str(warnings[0][-1])
        assert information == []


def test_v360_release_metadata_and_documentation_locations_are_current() -> None:
    gui = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    build = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    legacy_index = (ROOT / "docs" / "legacy" / "README.md").read_text(encoding="utf-8")
    current_note = ROOT / "docs" / "V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md"
    archived_v350_note = ROOT / "docs" / "legacy" / "V3_5_0_GUI_LIGHT_MODE_AND_LAYOUT.md"
    archived_v350_report = ROOT / "docs" / "legacy" / "V3_5_0_IMPLEMENTATION_TEST_REPORT.txt"
    archived_v360_note = ROOT / "docs" / "legacy" / "V3_6_0_SELL_RECONCILIATION_AND_HISTORY_ROBUSTNESS.md"
    archived_v360_report = ROOT / "docs" / "legacy" / "V3_6_0_IMPLEMENTATION_TEST_REPORT.txt"

    assert 'APP_VERSION = "3.9.0"' in gui
    assert "BouncyBot - IBKR Portable Trading Bot v3.9.0" in gui
    assert "This is synthetic v3.9.0 paper-trading example data." in gui
    assert 'version = "3.9.0"' in pyproject
    assert '$version = "3.9.0"' in build
    assert "**Current release: v3.9.0**" in readme
    assert "## v3.9.0" in changelog
    assert "current repository version, v3.9.0" in security
    assert "current v3.9.0 behavior" in docs_index
    assert current_note.is_file()
    assert archived_v350_note.is_file()
    assert archived_v350_report.is_file()
    assert archived_v360_note.is_file()
    assert archived_v360_report.is_file()
    assert not (ROOT / "docs" / archived_v350_note.name).exists()
    assert current_note.name in readme
    assert current_note.name in docs_index
    assert archived_v350_note.name in readme
    assert archived_v350_note.name in legacy_index
    assert archived_v350_report.name in legacy_index
    assert archived_v360_note.name in legacy_index
    assert archived_v360_report.name in legacy_index


def test_v360_release_note_documents_fail_closed_scope_and_compatibility() -> None:
    note = (
        ROOT / "docs" / "legacy" / "V3_6_0_SELL_RECONCILIATION_AND_HISTORY_ROBUSTNESS.md"
    ).read_text(encoding="utf-8")

    assert "persisted executions prove" in note
    assert "SELL_PARTIAL_TERMINAL" in note
    assert "SELL_QUANTITY_MISMATCH" in note
    assert "all app-owned SELL executions for the cycle" in note
    assert "numeric value" in note
    assert "audit-detail reads" in note
    assert "adds no SQLite table, column, index, migration, or persisted setting" in note
    assert "Existing v3.5.0 databases" in note


def test_v360_database_documentation_keeps_schema_compatibility_explicit() -> None:
    schema = (ROOT / "docs" / "DATABASE_SCHEMA.md").read_text(encoding="utf-8")
    assert "v3.6.0 changes SELL reconciliation and Trade History behavior only" in schema
    assert "Existing v3.5.0" in schema
