from __future__ import annotations

import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.ib_adapter import QualifiedContract, RthStatus
from app.models import ConnectionSettings, Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


def _settings() -> StrategySettings:
    return StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        what_if_check_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )


def _completed_cycle(settings: StrategySettings, number: int, net_pnl: float = 10.0):
    cycle = StrategyEngine.start_cycle(settings, number, "SIM", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 95.0
    cycle.avg_sell_price = 96.0 + net_pnl / 10.0
    cycle.sell_filled_qty = 10
    cycle.buy_commission = 1.0
    cycle.sell_commission = 1.0
    cycle.gross_pnl = net_pnl + 2.0
    cycle.net_pnl = net_pnl
    cycle.buy_filled_at = "2026-01-01T14:30:00+00:00"
    cycle.sell_filled_at = "2026-01-01T15:00:00+00:00"
    cycle.updated_at = f"2026-01-01T15:{number % 60:02d}:00+00:00"
    return cycle


def test_backup_rotation_and_restore_validation(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    settings = _settings()
    storage.upsert_cycle(_completed_cycle(settings, 1))
    storage.add_event("INFO", "seed event", ticker="AAPL")

    created = []
    for idx in range(5):
        path = storage.backup_database(f"rotation_{idx}", keep=3)
        assert path is not None
        created.append(path)
        # Ensure distinct timestamps on fast filesystems.
        time.sleep(0.002)

    backups = storage.list_database_backups()
    assert len(backups) <= 3
    assert created[-1] in backups
    latest_validation = storage.validate_latest_backup()
    assert latest_validation["ok"] is True
    assert latest_validation["integrity_check"].lower() == "ok"
    restore_validation = storage.validate_restore_candidate(backups[0])
    assert restore_validation["ok"] is True
    assert restore_validation["restore_copy_validated"] is True

    validation_file = tmp_path / "backups" / "latest_restore_validation.json"
    assert validation_file.exists()
    assert json.loads(validation_file.read_text())["ok"] is True


def test_audit_export_bundle_contains_restore_validated_database_and_reconciliation_facts(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    settings = _settings()
    cycle = _completed_cycle(settings, 1)
    storage.upsert_cycle(cycle)
    storage.add_order(
        cycle=cycle,
        action="BUY",
        order_type="TRAIL",
        order_id=1,
        perm_id=2,
        order_ref="IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL",
        quantity=10,
        trailing_percent=1.0,
        initial_stop_price=99.0,
        status="Submitted",
        raw={"test": True},
    )
    storage.add_event("INFO", "bundle event", ticker="AAPL", cycle_id=cycle.id)

    bundle = storage.create_audit_export_bundle(snapshot={"status": "test", "active_cycle": cycle.snapshot()})

    assert bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "snapshot.json" in names
        assert "database/bot_state_backup.sqlite" in names
        assert "sqlite_exports/cycles.json" in names
        assert "sqlite_exports/orders.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["backup_validation"]["ok"] is True
        snapshot = json.loads(zf.read("snapshot.json"))
        assert snapshot["status"] == "test"


def test_v300_metadata_reconciliation_ui_and_quality_config_are_present():
    gui = Path("app/gui.py").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    script = Path("scripts/run_quality_checks.py").read_text(encoding="utf-8")

    assert "IBKR Portable Trading Bot v3.0.18" in gui
    assert "# IBKR Portable Trading Bot v3.0.18" in readme
    assert 'version = "3.0.18"' in pyproject
    assert 'self.tabs.addTab(self.recovery_tab, "Reconciliation")' in gui
    assert "Reconciliation screen: SQLite vs IBKR/TWS" in gui
    assert "Export audit bundle" in gui
    assert "[tool.ruff]" in pyproject
    assert "[tool.pyright]" in pyproject
    assert "typeCheckingMode = \"basic\"" in pyproject
    assert "ruff" in script and "pyright" in script


class _StaleAdapter:
    def is_connected(self):
        return True

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        return QualifiedContract(ticker=ticker, con_id=con_id or 123, raw=object(), primary_exchange=primary_exchange)

    def regular_trading_hours_status(self, contract):
        return RthStatus(True, "test", "open", utc_now_iso())

    def open_app_orders(self):
        return []

    def recent_executions(self):
        return []

    def position_size(self, contract, account=""):
        return 0.0


def test_startup_detects_stale_active_cycle_and_blocks_first_resume_until_reconciliation(tmp_path: Path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    settings = _settings()
    old_cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    old_cycle.updated_at = (datetime.now(timezone.utc) - timedelta(hours=18)).replace(microsecond=0).isoformat()
    storage.upsert_cycle(old_cycle)

    controller = controller_module.TradingController(storage=storage)
    controller.adapter = _StaleAdapter()
    controller.connected = True
    controller.connection = ConnectionSettings(account="SIM", trading_mode="live")
    controller.contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ")

    assert controller._stale_active_cycle_detected is True
    assert controller._recovery_required is True
    assert controller._recovery_confidence() == "manual_review_required"

    controller._start_strategy(settings)

    assert controller._stale_active_cycle_detected is True
    assert controller._recovery_required is True
    assert "Stale active cycle" in controller.status
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP

    controller._resume_recovery_monitoring()
    assert controller._stale_active_cycle_detected is False


def test_clean_worker_shutdown_is_written_to_events(tmp_path: Path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage=storage)
    controller.start_thread()

    assert controller.shutdown_and_wait(timeout=3.0) is True

    events = storage.get_recent_events(20)
    messages = [row["message"] for row in events]
    assert any("Application worker stopped cleanly" in message for message in messages)
