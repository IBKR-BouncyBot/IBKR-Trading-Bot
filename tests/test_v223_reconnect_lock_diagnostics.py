from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from app.ib_adapter import IbAsyncTwsAdapter, QualifiedContract
from app.models import ConnectionSettings, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_atr_controls_participate_in_top_bar_input_lock():
    gui = _source("app/gui.py")
    assert "def _atr_config_widgets" in gui
    assert "self.atr_adaptive_check" in gui
    assert "self.atr_period_spin" in gui
    assert "self.atr_max_pct_spin" in gui
    assert "self._set_widgets_enabled(self._atr_config_widgets(), True)" in gui
    section = gui[gui.index("def _set_atr_percentage_field_state"):gui.index("def _update_recovery_panel")]
    assert "self._set_widgets_enabled([self.atr_min_profit_mult_spin]" in section
    assert "widget.setEnabled(enabled)" not in section
    assert "self.atr_min_profit_mult_spin.setEnabled" not in section


class _FakeIB:
    def __init__(self):
        self.connected = False
        self.req_market_data_type_calls: list[int] = []
        self.req_mkt_data_calls: list[object] = []
        self.cancel_mkt_data_calls: list[object] = []

    def isConnected(self):
        return self.connected

    def connect(self, host, port, clientId, timeout=12):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def reqMarketDataType(self, mode):
        self.req_market_data_type_calls.append(int(mode))

    def sleep(self, seconds):
        return None

    def reqMktData(self, contract, generic_tick_list, snapshot, regulatory_snapshot):
        ticker = SimpleNamespace(contract=contract, marker=f"ticker-{len(self.req_mkt_data_calls) + 1}")
        self.req_mkt_data_calls.append(ticker)
        return ticker

    def cancelMktData(self, contract):
        self.cancel_mkt_data_calls.append(contract)


def _contract(con_id: int = 1001) -> QualifiedContract:
    raw = SimpleNamespace(conId=con_id, exchange="SMART", primaryExchange="NASDAQ", symbol="NBIS")
    return QualifiedContract(ticker="NBIS", con_id=con_id, raw=raw, primary_exchange="NASDAQ")


def test_adapter_reconnect_forgets_stale_market_data_tickers(monkeypatch):
    adapter = IbAsyncTwsAdapter()
    fake_ib = _FakeIB()
    adapter._require_ib_async = lambda: (lambda: fake_ib, object, object)  # type: ignore[assignment]
    adapter.refresh_open_trades_cache = lambda: None  # type: ignore[assignment]

    old_contract = _contract(1001)
    old_ticker = SimpleNamespace(contract=old_contract.raw, marker="stale")
    adapter._tickers[adapter._subscription_key(old_contract, "")] = old_ticker
    adapter._active_market_data_type = 1
    adapter._auto_selected_market_data_type = 1
    adapter._last_auto_rescan_monotonic = 123.0

    adapter.connect("127.0.0.1", 4001, 11, market_data_type=1)

    assert adapter._tickers == {}
    assert adapter._active_market_data_type == 1
    assert adapter._auto_selected_market_data_type is None
    assert adapter._last_auto_rescan_monotonic == 0.0
    fresh = adapter._request_ticker(old_contract, "")
    assert fresh is not old_ticker
    assert fresh.marker == "ticker-1"
    assert len(fake_ib.req_mkt_data_calls) == 1


def test_adapter_disconnect_clears_subscription_state_and_cancels_when_connected(monkeypatch):
    adapter = IbAsyncTwsAdapter()
    fake_ib = _FakeIB()
    fake_ib.connected = True
    adapter.ib = fake_ib
    contract = _contract(2002)
    adapter._tickers[adapter._subscription_key(contract, "")] = SimpleNamespace(contract=contract.raw)
    adapter._active_market_data_type = 1
    adapter._auto_selected_market_data_type = 3
    adapter._last_auto_rescan_monotonic = 99.0

    adapter.disconnect()

    assert fake_ib.connected is False
    assert fake_ib.cancel_mkt_data_calls == [contract.raw]
    assert adapter._tickers == {}
    assert adapter._active_market_data_type is None
    assert adapter._auto_selected_market_data_type is None
    assert adapter._last_auto_rescan_monotonic == 0.0


def test_human_readable_event_log_and_latest_state_report(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    conn = ConnectionSettings(host="127.0.0.1", port=4001, client_id=11, account="")
    strat = StrategySettings(ticker="NBIS", investment_amount=1000.0)
    cycle = StrategyEngine.start_cycle(strat, 1, "", 217.16, 0.0)
    storage.save_connection_settings(conn)
    storage.save_strategy_settings(strat)
    storage.upsert_cycle(cycle)
    storage.add_event("WARN", "Reconnect failed once", ticker="NBIS", cycle_id=cycle.id, raw={"errno": 1225})

    event_log = tmp_path / "debug_reports" / "audit_events_readable.log"
    assert event_log.exists()
    event_text = event_log.read_text(encoding="utf-8")
    assert "[WARN]" in event_text
    assert "Reconnect failed once" in event_text
    assert "ticker=NBIS" in event_text

    report_path = storage.write_human_debug_report({
        "connected": True,
        "status": "Connected to IB Gateway",
        "connection": asdict(conn),
        "strategy": asdict(strat),
        "active_cycle": cycle.snapshot(),
        "price_snapshot": {"price": 217.16, "source": "marketPrice", "api_data_seen_count": 3},
        "broker_recovery": {"connected": True, "open_app_orders": [], "recent_executions": []},
        "history_summary": {"completed_cycles": 0},
        "events": storage.get_recent_events(5),
    })
    report_text = report_path.read_text(encoding="utf-8")
    assert "IBKR Trading Bot human-readable debug report" in report_text
    assert "Active cycle" in report_text
    assert "Price snapshot" in report_text
    assert "Broker recovery probe" in report_text
    assert "Reconnect failed once" in report_text
