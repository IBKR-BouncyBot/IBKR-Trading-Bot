from __future__ import annotations

from types import SimpleNamespace

from app.ib_adapter import QualifiedContract
from app.models import Stage, StopAction, StrategySettings, recovery_cycle_signature
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub

APP_REF_BUY = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
APP_REF_SELL = "IBKRBOT|AAPL|CYCLE-000001|TEST|SELL_TRAIL"


def _order(ref: str = APP_REF_BUY, *, order_id: int = 101, status: str = "Submitted"):
    return SimpleNamespace(
        order_ref=ref,
        order_id=order_id,
        perm_id=9000 + order_id,
        status=status,
        filled=0,
        remaining=10,
        avg_fill_price=0.0,
        commission=0.0,
        raw={"orderRef": ref, "orderId": order_id},
    )


class RecoveryAdapter:
    def __init__(self, *, open_sequences=None, position=0, executions=None):
        self.open_sequences = [list(seq) for seq in (open_sequences or [[]])]
        self.position = position
        self.executions = list(executions or [])
        self.cancel_calls: list[tuple[str, int | None]] = []
        self.open_calls = 0
        self.qualified: list[tuple] = []
        self.polled_refs: list[str] = []

    def open_app_orders(self):
        self.open_calls += 1
        if self.open_sequences:
            return list(self.open_sequences.pop(0))
        return []

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        self.qualified.append((ticker, exchange, currency, primary_exchange, con_id))
        return QualifiedContract(ticker=ticker.upper(), con_id=con_id or 123, raw=object(), primary_exchange=primary_exchange)

    def position_size(self, contract, account=""):
        return self.position

    def recent_executions(self):
        return list(self.executions)

    def poll_order(self, order_ref: str):
        self.polled_refs.append(order_ref)
        return None

    def cancel_order(self, order_ref: str, order_id=None):
        self.cancel_calls.append((order_ref, order_id))



def _controller(tmp_path, monkeypatch, *, adapter=None):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage=storage)
    controller.adapter = adapter or RecoveryAdapter()
    controller.connected = True
    controller.connection.account = "DU1234567"
    return controller



def _active_cycle(controller, *, stage: Stage = Stage.BUY_TRAIL_ACTIVE):
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=1.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=1.0,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "DU1234567", 100.0, 0.0)
    cycle.stage = stage
    cycle.buy_order_ref = APP_REF_BUY
    cycle.buy_order_id = 101
    cycle.buy_status = "Submitted"
    cycle.sell_order_ref = APP_REF_SELL
    cycle.sell_order_id = 202
    cycle.sell_status = "Submitted"
    if stage in {Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE}:
        cycle.buy_filled_qty = 10
        cycle.avg_buy_price = 96.0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return cycle



def test_recovery_broker_refresh_command_populates_probe(tmp_path, monkeypatch):
    executions = [
        {
            "execution_id": "EXEC1",
            "order_ref": APP_REF_BUY,
            "side": "BUY",
            "shares": 10,
            "price": 96.0,
            "time": "2026-01-01T14:30:00+00:00",
        }
    ]
    adapter = RecoveryAdapter(open_sequences=[[_order(APP_REF_BUY)]], position=10, executions=executions)
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    cycle = _active_cycle(controller, stage=Stage.BUY_TRAIL_ACTIVE)

    controller._handle_command("REFRESH_BROKER_STATE", {})

    probe = controller._last_recovery_probe
    assert probe["connected"] is True
    assert probe["cycle_id"] == cycle.id
    assert probe["open_order_refs"] == [APP_REF_BUY]
    assert probe["position_size"] == 10
    assert probe["recent_executions"][0]["execution_id"] == "EXEC1"
    assert probe["local_cycle_signature"] == recovery_cycle_signature(cycle)
    assert probe["last_successful_checked_at"] == probe["checked_at"]
    assert controller.status == "Broker state refreshed for Recovery screen."
    assert adapter.open_calls == 1



def test_failed_recovery_refresh_preserves_last_successful_timestamp(tmp_path, monkeypatch):
    adapter = RecoveryAdapter(open_sequences=[[]], position=0, executions=[])
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    cycle = _active_cycle(controller, stage=Stage.BUY_TRAIL_ACTIVE)

    controller._handle_command("REFRESH_BROKER_STATE", {})
    successful_at = controller._last_recovery_probe["checked_at"]
    assert controller._last_successful_recovery_refresh_at == successful_at

    controller._handle_broker_connection_problem("temporary socket loss")
    assert controller._last_recovery_probe["invalidated_at"]
    assert "connection failed" in controller._last_recovery_probe["invalidation_reason"]

    controller.connected = False
    controller._handle_command("REFRESH_BROKER_STATE", {})

    failed_probe = controller._last_recovery_probe
    assert failed_probe["connected"] is False
    assert failed_probe["last_successful_checked_at"] == successful_at
    assert failed_probe["local_cycle_signature"] == recovery_cycle_signature(cycle)


def test_recovery_broker_refresh_disconnected_does_not_query_adapter(tmp_path, monkeypatch):
    adapter = RecoveryAdapter(open_sequences=[[_order(APP_REF_BUY)]])
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    controller.connected = False
    _active_cycle(controller, stage=Stage.BUY_TRAIL_ACTIVE)

    controller._handle_command("REFRESH_BROKER_STATE", {})

    probe = controller._last_recovery_probe
    assert probe["connected"] is False
    assert probe["open_app_orders"] == []
    assert "requires an active IBKR API connection" in probe["error"]
    assert adapter.open_calls == 0



def test_recovery_resume_command_recovers_matching_open_buy_order(tmp_path, monkeypatch):
    adapter = RecoveryAdapter(open_sequences=[[_order(APP_REF_BUY)]], position=0)
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    cycle = _active_cycle(controller, stage=Stage.BUY_TRAIL_ACTIVE)

    controller._handle_command("RESUME_RECOVERY_MONITORING", {})

    assert controller.active_cycle is not None
    assert controller.active_cycle.id == cycle.id
    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert controller._recovery_required is False
    assert controller.status == f"Recovery resumed: monitoring AAPL at {Stage.BUY_TRAIL_ACTIVE.value}."
    assert adapter.open_calls == 1



def test_recovery_cancel_app_order_cancels_orphan_app_owned_order(tmp_path, monkeypatch):
    adapter = RecoveryAdapter(open_sequences=[[_order(APP_REF_SELL, order_id=202)], []])
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    controller.active_cycle = None

    controller._handle_command("CANCEL_RECOVERY_APP_ORDER", {})

    assert adapter.cancel_calls == [(APP_REF_SELL, 202)]
    assert controller._last_recovery_probe["open_app_orders"] == []
    assert controller._recovery_required is False
    assert "Cancel requested for 1 app-owned order" in controller.status



def test_recovery_mark_manually_handled_stops_cycle_and_writes_audit(tmp_path, monkeypatch):
    adapter = RecoveryAdapter(open_sequences=[[_order(APP_REF_BUY)]])
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    cycle = _active_cycle(controller, stage=Stage.MANUAL_REVIEW)
    cycle.error_message = "RECOVERY REQUIRED: test"
    controller.storage.upsert_cycle(cycle)

    controller._handle_command("MARK_RECOVERY_MANUALLY_HANDLED", {"note": "closed in TWS"})

    stored = controller.storage.get_cycle(cycle.id)
    details = controller.storage.get_cycle_audit_bundle(cycle.id)
    assert controller.active_cycle is None
    assert stored is not None
    assert stored.stage == Stage.STOPPED
    assert stored.stop_after_current_cycle is True
    assert "closed in TWS" in (stored.error_message or "")
    assert any(row["event_type"] == "MANUALLY_HANDLED" for row in details["decision_events"])
    assert adapter.cancel_calls == []



def test_recovery_stop_after_current_cycle_command_sets_safe_stop_flag(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    cycle = _active_cycle(controller, stage=Stage.WAIT_RISE_TRIGGER)

    controller._handle_command("STOP_ACTION", {"action": StopAction.STOP_AFTER_CURRENT_CYCLE})

    stored = controller.storage.get_cycle(cycle.id)
    assert controller.active_cycle is not None
    assert controller.active_cycle.stop_after_current_cycle is True
    assert stored is not None
    assert stored.stop_after_current_cycle is True
    assert stored.stage == Stage.WAIT_RISE_TRIGGER


class StartupResumeAdapter(RecoveryAdapter):
    def __init__(self, *, open_sequences=None):
        super().__init__(open_sequences=open_sequences or [[]])
        self.connect_calls: list[tuple[str, int, int, int]] = []
        self.connected = False

    def connect(self, host, port, client_id, market_data_type):
        self.connect_calls.append((host, port, client_id, market_data_type))
        self.connected = True

    def is_connected(self):
        return self.connected


def test_startup_active_cycle_does_not_auto_recover_on_connect_until_start_clicked(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=1.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=1.0,
    )
    storage.save_strategy_settings(settings)
    cycle = StrategyEngine.start_cycle(settings, 1, "DU1234567", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = APP_REF_BUY
    cycle.buy_order_id = 101
    cycle.buy_status = "Submitted"
    storage.upsert_cycle(cycle)

    controller = controller_module.TradingController(storage=storage)
    adapter = StartupResumeAdapter(open_sequences=[[_order(APP_REF_BUY)]])
    controller.adapter = adapter
    controller.connection.account = "DU1234567"

    assert controller._startup_resume_required is True
    controller._connect(controller.connection)

    assert controller.connected is True
    assert controller._startup_resume_required is True
    assert adapter.open_calls == 0
    assert "click 4. Start strategy to resume monitoring/recovery" in controller.status


def test_start_click_clears_startup_resume_gate_and_runs_recovery(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=1.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=1.0,
    )
    storage.save_strategy_settings(settings)
    cycle = StrategyEngine.start_cycle(settings, 1, "DU1234567", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = APP_REF_BUY
    cycle.buy_order_id = 101
    cycle.buy_status = "Submitted"
    storage.upsert_cycle(cycle)

    controller = controller_module.TradingController(storage=storage)
    adapter = StartupResumeAdapter(open_sequences=[[_order(APP_REF_BUY)]])
    adapter.connected = True
    controller.adapter = adapter
    controller.connected = True
    controller.connection.account = "DU1234567"

    controller._start_strategy(settings)

    assert controller._startup_resume_required is False
    assert adapter.open_calls == 1
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE


def test_stop_now_no_broker_action_stops_stage1_without_adapter_calls(tmp_path, monkeypatch):
    adapter = RecoveryAdapter(open_sequences=[[_order(APP_REF_BUY)]], position=0)
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    cycle = _active_cycle(controller, stage=Stage.WAIT_INITIAL_DROP)
    cycle.buy_order_ref = ""
    cycle.buy_order_id = None
    cycle.buy_status = None
    cycle.sell_order_ref = ""
    cycle.sell_order_id = None
    cycle.sell_status = None
    controller.storage.upsert_cycle(cycle)

    controller._handle_command("STOP_ACTION", {"action": StopAction.STOP_NOW_NO_BROKER_ACTION})

    stored = controller.storage.get_cycle(cycle.id)
    assert stored is not None
    assert stored.stage == Stage.STOPPED
    assert stored.stop_after_current_cycle is True
    assert "no broker order was cancelled or submitted" in (stored.error_message or "")
    assert adapter.cancel_calls == []
    assert adapter.open_calls == 0


def test_stop_now_no_broker_action_stops_stage1_without_adapter_order_calls(tmp_path, monkeypatch):
    adapter = RecoveryAdapter(open_sequences=[[]])
    controller = _controller(tmp_path, monkeypatch, adapter=adapter)
    cycle = _active_cycle(controller, stage=Stage.WAIT_INITIAL_DROP)
    cycle.buy_order_ref = None
    cycle.buy_order_id = None
    cycle.buy_status = ""
    cycle.sell_order_ref = None
    cycle.sell_order_id = None
    cycle.sell_status = ""
    controller.storage.upsert_cycle(cycle)

    controller._handle_command("STOP_ACTION", {"action": StopAction.STOP_NOW_NO_BROKER_ACTION})

    stored = controller.storage.get_cycle(cycle.id)
    assert stored is not None
    assert stored.stage == Stage.STOPPED
    assert stored.stop_after_current_cycle is True
    assert "no broker order was cancelled or submitted" in (stored.error_message or "")
    assert adapter.cancel_calls == []
