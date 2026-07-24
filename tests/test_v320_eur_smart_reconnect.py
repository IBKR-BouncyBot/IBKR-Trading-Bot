"""v3.2.0 USD/EUR SMART-contract and fixed reconnect regressions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.flowchart_model import build_strategy_flowchart_cards
from app.ib_adapter import (
    BrokerAdapter,
    BrokerAdapterError,
    BrokerConnectivityStatus,
    ContractSearchResult,
    IbAsyncTwsAdapter,
    QualifiedContract,
)
from app.models import ConnectionSettings, Stage, StrategySettings
from app.storage import BotStorage, DatabaseCurrencyError
from app.strategy import StrategyEngine
from tests.test_comprehensive_ib_adapter import FakeIB, FakeOrder, FakeStock
from tests.test_controller_headless import _install_qt_stub


def _detail(
    contract: Any,
    *,
    valid_exchanges: str = "SMART,IBIS",
    order_types: str = "MKT,TRAIL,WHATIF",
    currency_hours: bool = True,
    min_size: float = 1.0,
    size_increment: float = 1.0,
) -> Any:
    return SimpleNamespace(
        contract=contract,
        minTick=0.01,
        validExchanges=valid_exchanges,
        orderTypes=order_types,
        marketRuleIds="26,26",
        liquidHours=("20260724:0900-20260724:1730" if currency_hours else ""),
        timeZoneId=("Europe/Berlin" if currency_hours else ""),
        minSize=min_size,
        sizeIncrement=size_increment,
    )


def _live_adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[IbAsyncTwsAdapter, FakeIB]:
    adapter = IbAsyncTwsAdapter()
    ib = FakeIB()
    adapter.ib = ib
    adapter._upstream_connected = True
    adapter._upstream_state = "connected"
    adapter._upstream_message = "ready"
    adapter._market_data_event_tracking_available = True
    monkeypatch.setattr(adapter, "_require_ib_async", lambda: (FakeIB, FakeOrder, FakeStock))
    return adapter, ib


def test_base_adapter_optional_defaults_are_noops() -> None:
    adapter = BrokerAdapter()
    assert adapter.process_events(0.0) is None
    assert adapter.managed_accounts() == []


def test_strategy_settings_accept_only_usd_eur_stk_smart() -> None:
    assert StrategySettings(ticker="AAPL", currency="usd").validate() == []
    assert StrategySettings(ticker="SAP", currency="eur").validate() == []
    assert StrategySettings(ticker="EXAMPLE", currency="EUR", primary_exchange="ENEXT.BE").validate() == []
    assert "USD or EUR" in " ".join(StrategySettings(ticker="VOD", currency="GBP").validate())
    assert "SMART" in " ".join(StrategySettings(ticker="SAP", exchange="IBIS", currency="EUR").validate())
    assert "ordinary STK" in " ".join(StrategySettings(ticker="SAP", currency="EUR", sec_type="CFD").validate())


def test_contract_search_support_requires_exact_usd_or_eur_stock() -> None:
    assert ContractSearchResult("SAP", "STK", "EUR", "IBIS", "IBIS", 123).supported is True
    assert ContractSearchResult("AAPL", "STK", "USD", "NASDAQ", "NASDAQ", 456).supported is True
    assert ContractSearchResult("SAP", "STK", "EUR", "IBIS", "IBIS", None).supported is False
    assert ContractSearchResult("VOD", "STK", "GBP", "LSE", "LSE", 789).supported is False
    assert ContractSearchResult("DAX", "CFD", "EUR", "SMART", "", 999).supported is False


def test_live_qualification_requires_exact_eur_stk_and_smart_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    qualified = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    qualified.conId = 1001
    qualified.localSymbol = "SAP"
    qualified.tradingClass = "SAP"
    ib.qualified_contracts = [qualified]
    ib.contract_details = [_detail(qualified, min_size=1.0, size_increment=0.001)]

    contract = adapter.qualify_stock("sap", "smart", "eur", "ibis", 1001)

    assert contract.ticker == "SAP"
    assert contract.con_id == 1001
    assert contract.currency == "EUR"
    assert contract.exchange == "SMART"
    assert contract.sec_type == "STK"
    assert contract.valid_exchanges == ("SMART", "IBIS")
    assert {"MKT", "TRAIL"}.issubset(set(contract.order_types))
    assert contract.liquid_hours
    assert contract.time_zone == "Europe/Berlin"
    assert contract.size_increment == pytest.approx(0.001)

    with pytest.raises(BrokerAdapterError, match="positive conId"):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", None)
    with pytest.raises(BrokerAdapterError, match="USD and EUR"):
        adapter.qualify_stock("SAP", "SMART", "GBP", "IBIS", 1001)
    with pytest.raises(BrokerAdapterError, match="SMART-routed"):
        adapter.qualify_stock("SAP", "IBIS", "EUR", "IBIS", 1001)


def test_live_qualification_fails_closed_for_wrong_identity_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)

    wrong_id = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    wrong_id.conId = 2002
    ib.qualified_contracts = [wrong_id]
    ib.contract_details = [_detail(wrong_id)]
    with pytest.raises(BrokerAdapterError, match="instead of the selected conId"):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)

    adapter._contracts.clear()
    adapter._contract_details.clear()
    wrong_currency = FakeStock("SAP", "SMART", "USD", primaryExchange="IBIS")
    wrong_currency.conId = 1001
    ib.qualified_contracts = [wrong_currency]
    ib.contract_details = [_detail(wrong_currency)]
    with pytest.raises(BrokerAdapterError, match="not the requested EUR"):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)

    adapter._contracts.clear()
    adapter._contract_details.clear()
    unsupported = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    unsupported.conId = 1001
    ib.qualified_contracts = [unsupported]
    ib.contract_details = [_detail(unsupported, valid_exchanges="IBIS", order_types="MKT")]
    with pytest.raises(BrokerAdapterError, match="not available for SMART"):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)


def test_whole_share_quantity_normalization_honors_fractional_and_large_steps() -> None:
    adapter = IbAsyncTwsAdapter()
    contract = QualifiedContract("SAP", 1, object(), min_size=1.0, size_increment=0.001)
    assert adapter.normalize_order_quantity(contract, 37) == 37

    contract.size_increment = 0.5
    assert adapter.normalize_order_quantity(contract, 37) == 37

    contract.size_increment = 1.5
    assert adapter.normalize_order_quantity(contract, 37) == 36

    contract.min_size = 100.0
    contract.size_increment = 100.0
    assert adapter.normalize_order_quantity(contract, 99) == 0
    assert adapter.normalize_order_quantity(contract, 255) == 200


def test_position_lookup_uses_exact_con_id_without_same_symbol_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    raw = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    raw.conId = 1001
    contract = QualifiedContract(
        "SAP",
        1001,
        raw,
        primary_exchange="IBIS",
        currency="EUR",
        exchange="SMART",
    )
    ib.position_values = [
        SimpleNamespace(account="DU1", contract=SimpleNamespace(conId=1001, symbol="SAP"), position=7),
        SimpleNamespace(account="DU1", contract=SimpleNamespace(conId=2002, symbol="SAP"), position=11),
    ]

    assert adapter.position_size(contract, account="DU1") == pytest.approx(7.0)


def test_non_us_contract_without_liquid_hours_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    ib.contract_details = []
    eur_raw = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    eur_raw.conId = 1001
    eur = QualifiedContract(
        "SAP",
        1001,
        eur_raw,
        primary_exchange="IBIS",
        currency="EUR",
        exchange="SMART",
    )
    status = adapter.regular_trading_hours_status(eur)
    assert status.is_open is False
    assert status.source == "contract_rth_unavailable"
    assert "cannot use US fallback" in status.message

    us_raw = FakeStock("AAPL", "SMART", "USD", primaryExchange="NASDAQ")
    us_raw.conId = 1002
    us = QualifiedContract(
        "AAPL",
        1002,
        us_raw,
        primary_exchange="NASDAQ",
        currency="USD",
        exchange="SMART",
    )
    adapter._rth_cache.clear()
    us_status = adapter.regular_trading_hours_status(us)
    assert us_status.source == "fallback_no_contract_liquid_hours"


def test_european_liquid_hours_use_contract_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ib = _live_adapter(monkeypatch)
    midday_utc = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    status = adapter._parse_liquid_hours_window(
        "20260724:0900-20260724:1730",
        "Europe/Berlin",
        midday_utc,
    )
    assert status is not None and status.is_open is True
    assert status.session_open == "2026-07-24T09:00:00+02:00"
    assert status.session_close == "2026-07-24T17:30:00+02:00"

    before_open = adapter._parse_liquid_hours_window(
        "20260724:0900-20260724:1730",
        "Europe/Berlin",
        datetime(2026, 7, 24, 6, 59, tzinfo=timezone.utc),
    )
    assert before_open is not None and before_open.is_open is False


def _cycle(settings: StrategySettings, cycle_number: int = 1) -> Any:
    return StrategyEngine.start_cycle(settings, cycle_number, "", 100.0, 0.0)


def test_database_currency_is_draft_until_first_cycle_then_locked(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "currency.sqlite")
    assert storage.database_contract_currency_info() == {
        "currency": "",
        "cycle_count": 0,
        "locked": False,
    }

    storage.save_strategy_settings(
        StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001)
    )
    assert storage.database_contract_currency_info() == {
        "currency": "EUR",
        "cycle_count": 0,
        "locked": False,
    }

    storage.save_strategy_settings(
        StrategySettings(ticker="AAPL", currency="USD", contract_con_id=1002)
    )
    assert storage.database_contract_currency() == "USD"

    eur_storage = BotStorage(tmp_path / "eur.sqlite")
    eur_settings = StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001)
    eur_cycle = _cycle(eur_settings)
    assert eur_cycle.con_id == 1001
    eur_storage.upsert_cycle(eur_cycle)
    assert eur_storage.database_contract_currency_info() == {
        "currency": "EUR",
        "cycle_count": 1,
        "locked": True,
    }
    with pytest.raises(DatabaseCurrencyError, match="locked to EUR"):
        eur_storage.claim_database_contract_currency("USD")
    with pytest.raises(DatabaseCurrencyError, match="locked to EUR"):
        eur_storage.save_strategy_settings(
            StrategySettings(ticker="AAPL", currency="USD", contract_con_id=1002)
        )


def test_v312_database_currency_is_inferred_and_mixed_currency_fails_closed(
    tmp_path: Path,
) -> None:
    storage = BotStorage(tmp_path / "legacy.sqlite")
    storage.upsert_cycle(
        _cycle(StrategySettings(ticker="AAPL", currency="USD", contract_con_id=1001))
    )
    with storage.connect() as con:
        con.execute("DELETE FROM app_settings WHERE key='database_contract_currency'")
    assert storage.database_contract_currency() == "USD"
    with storage.connect() as con:
        row = con.execute(
            "SELECT value_json FROM app_settings WHERE key='database_contract_currency'"
        ).fetchone()
    assert row is not None and row["value_json"] == '"USD"'

    second = _cycle(
        StrategySettings(ticker="MSFT", currency="USD", contract_con_id=1002),
        cycle_number=2,
    )
    storage.upsert_cycle(second)
    with storage.connect() as con:
        con.execute("UPDATE cycles SET currency='EUR' WHERE id=?", (second.id,))
    with pytest.raises(DatabaseCurrencyError, match="multiple contract currencies"):
        storage.database_contract_currency_info()



def test_exact_contract_ledgers_exclude_other_positive_conids_but_include_legacy(
    tmp_path: Path,
) -> None:
    storage = BotStorage(tmp_path / "contract-ledgers.sqlite")

    def persist_unsold(cycle_number: int, con_id: int | None, quantity: int) -> None:
        settings = StrategySettings(
            ticker="DUAL",
            currency="USD",
            contract_con_id=con_id,
        )
        cycle = _cycle(settings, cycle_number=cycle_number)
        cycle.con_id = con_id
        cycle.stage = Stage.WAIT_RISE_TRIGGER
        cycle.buy_filled_qty = quantity
        cycle.avg_buy_price = 100.0
        storage.upsert_cycle(cycle)

    def persist_complete(cycle_number: int, con_id: int | None, pnl: float) -> None:
        settings = StrategySettings(
            ticker="DUAL",
            currency="USD",
            contract_con_id=con_id,
        )
        cycle = _cycle(settings, cycle_number=cycle_number)
        cycle.con_id = con_id
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.buy_filled_qty = 1
        cycle.sell_filled_qty = 1
        cycle.avg_buy_price = 100.0
        cycle.avg_sell_price = 100.0 + pnl
        cycle.net_pnl = pnl
        storage.upsert_cycle(cycle)

    persist_unsold(1, 1001, 10)
    persist_unsold(2, 2002, 20)
    persist_unsold(3, None, 3)
    persist_complete(4, 1001, 10.0)
    persist_complete(5, 2002, 20.0)
    persist_complete(6, None, 3.0)

    exact_1001 = storage.get_app_owned_unsold_position("DUAL", con_id=1001)
    exact_2002 = storage.get_app_owned_unsold_position("DUAL", con_id=2002)
    ticker_wide = storage.get_app_owned_unsold_position("DUAL")

    assert exact_1001["quantity"] == 13
    assert {item["con_id"] for item in exact_1001["cycles"]} == {1001, None}
    assert exact_2002["quantity"] == 23
    assert {item["con_id"] for item in exact_2002["cycles"]} == {2002, None}
    assert ticker_wide["quantity"] == 33

    assert storage.get_realized_net_profit_for_ticker("DUAL", con_id=1001) == 13.0
    assert storage.get_realized_net_profit_for_ticker("DUAL", con_id=2002) == 23.0
    assert storage.get_realized_net_profit_for_ticker("DUAL") == 33.0
    assert storage.get_daily_net_pnl_for_ticker("DUAL", con_id=1001) == 13.0
    assert storage.get_completed_cycle_count("DUAL", con_id=1001) == 2
    assert storage.get_completed_cycle_count("DUAL", con_id=2002) == 2
    assert storage.get_completed_cycle_count("DUAL") == 3

def test_missing_required_order_type_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    contract = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    contract.conId = 1001
    ib.qualified_contracts = [contract]
    ib.contract_details = [
        _detail(contract, valid_exchanges="SMART,IBIS", order_types="MKT,WHATIF")
    ]

    with pytest.raises(BrokerAdapterError, match="TRAIL"):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)


def test_invalid_non_us_contract_timezone_fails_closed() -> None:
    adapter = IbAsyncTwsAdapter()
    status = adapter._parse_liquid_hours_window(
        "20260724:0900-20260724:1730",
        "Not/A-Timezone",
    )
    assert status is not None
    assert status.is_open is False
    assert status.source == "contract_time_zone_invalid"


def test_eur_flowchart_formats_contract_currency() -> None:
    settings = StrategySettings(
        ticker="SAP",
        currency="EUR",
        contract_con_id=1001,
        investment_amount=10_000.0,
    )
    cards = build_strategy_flowchart_cards(settings)
    joined = " ".join(card.trigger_summary + " " + " ".join(card.details) for card in cards)
    assert "€" in joined
    assert "$" not in joined


def test_draft_without_exact_contract_does_not_claim_database_currency(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "unselected.sqlite")
    storage.save_strategy_settings(StrategySettings(ticker="SAP", currency="EUR"))
    assert storage.database_contract_currency_info() == {
        "currency": "",
        "cycle_count": 0,
        "locked": False,
    }


def test_controller_requires_exact_contract_for_live_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "exact.sqlite")
    )
    controller.adapter = SimpleNamespace(requires_exact_contract_selection=True)

    with pytest.raises(ValueError, match="positive conId"):
        controller._validate_exact_contract_selection(
            StrategySettings(ticker="SAP", currency="EUR")
        )

    settings = StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001)
    assert controller._validate_exact_contract_selection(
        settings, claim_database_currency=True
    ) == "EUR"
    assert controller.storage.database_contract_currency() == "EUR"


def test_commission_currency_mismatch_is_excluded_and_stops_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "commission.sqlite")
    cycle = _cycle(
        StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001)
    )
    storage.upsert_cycle(cycle)
    controller = controller_module.TradingController(storage=storage)
    controller.active_cycle = cycle

    accepted = controller._commission_in_cycle_currency(
        cycle,
        1.25,
        "USD",
        execution_id="EUR-MISMATCH-1",
        source="TEST",
    )

    assert accepted is None
    stored = storage.get_cycle(cycle.id)
    assert stored is not None
    assert stored.stop_after_current_cycle is True
    with storage.connect() as con:
        rows = con.execute(
            "SELECT event_type FROM decision_events WHERE cycle_id=?",
            (cycle.id,),
        ).fetchall()
    assert any(row["event_type"] == "COMMISSION_CURRENCY_MISMATCH" for row in rows)


def test_commission_mismatch_disables_saved_repeat_and_marks_new_active_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "commission-next-cycle.sqlite")
    settings = StrategySettings(
        ticker="SAP",
        currency="EUR",
        contract_con_id=1001,
        auto_repeat=True,
    )
    completed = _cycle(settings)
    completed.stage = Stage.CYCLE_COMPLETE
    completed.buy_filled_qty = 10
    completed.sell_filled_qty = 10
    completed.avg_buy_price = 100.0
    completed.avg_sell_price = 101.0
    storage.upsert_cycle(completed)
    active = _cycle(settings, cycle_number=2)
    storage.upsert_cycle(active)
    storage.save_strategy_settings(settings)

    controller = controller_module.TradingController(storage=storage)
    controller.strategy = settings
    controller.active_cycle = active

    accepted = controller._commission_in_cycle_currency(
        completed,
        0.75,
        "USD",
        execution_id="LATE-MISMATCH",
        source="COMMISSION_REPORT",
    )

    assert accepted is None
    assert controller.strategy.auto_repeat is False
    saved = storage.load_strategy_settings()
    assert saved.auto_repeat is False
    stored_completed = storage.get_cycle(completed.id)
    stored_active = storage.get_cycle(active.id)
    assert stored_completed is not None and stored_completed.stop_after_current_cycle is True
    assert stored_active is not None and stored_active.stop_after_current_cycle is True


def test_recovered_fill_reports_all_nonzero_commission_currencies() -> None:
    adapter = IbAsyncTwsAdapter()
    fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                shares=5,
                price=100.0,
                avgPrice=100.0,
                side="BOT",
                execId="A",
            ),
            commissionReport=SimpleNamespace(commission=0.5, currency="EUR"),
            time=None,
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                shares=5,
                price=101.0,
                avgPrice=100.5,
                side="BOT",
                execId="B",
            ),
            commissionReport=SimpleNamespace(commission=0.25, currency="USD"),
            time=None,
        ),
    ]
    state = adapter._polled_state_from_fills(
        fills,
        order_ref="IBKRBOT|SAP|CYCLE-000001|X|BUY_TRAIL",
        order_id=1,
        perm_id=2,
        action="BUY",
    )
    assert state is not None
    assert state.raw["commission_currencies"] == ["EUR", "USD"]


def test_legacy_blank_cycle_currency_is_treated_as_usd(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "legacy-blank.sqlite")
    cycle = _cycle(StrategySettings(ticker="AAPL", currency="USD", contract_con_id=1001))
    storage.upsert_cycle(cycle)
    with storage.connect() as con:
        con.execute("DELETE FROM app_settings WHERE key='database_contract_currency'")
        con.execute("UPDATE cycles SET currency=NULL WHERE id=?", (cycle.id,))

    assert storage.database_contract_currency() == "USD"
    loaded = storage.get_cycle(cycle.id)
    assert loaded is not None
    assert loaded.currency == "USD"
    with pytest.raises(DatabaseCurrencyError, match="locked to USD"):
        storage.claim_database_contract_currency("EUR")


def test_non_us_contract_with_hours_but_missing_timezone_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    eur_raw = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    eur_raw.conId = 1001
    ib.contract_details = [
        SimpleNamespace(
            liquidHours="20260724:0900-20260724:1730",
            timeZoneId="",
        )
    ]
    eur = QualifiedContract(
        "SAP",
        1001,
        eur_raw,
        primary_exchange="IBIS",
        currency="EUR",
        exchange="SMART",
    )

    status = adapter.regular_trading_hours_status(eur)

    assert status.is_open is False
    assert status.source == "contract_rth_unavailable"
    assert "without a timeZoneId" in status.message


def test_live_qualification_requires_capability_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    contract = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    contract.conId = 1001
    ib.qualified_contracts = [contract]

    ib.contract_details = [_detail(contract, valid_exchanges="", order_types="MKT,TRAIL")]
    with pytest.raises(BrokerAdapterError, match="valid-exchange metadata"):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)

    adapter._contracts.clear()
    adapter._contract_details.clear()
    ib.contract_details = [_detail(contract, valid_exchanges="SMART,IBIS", order_types="")]
    with pytest.raises(BrokerAdapterError, match="order-type metadata"):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)


class _ReconnectAdapter:
    requires_exact_contract_selection = True

    def __init__(self, failures: int) -> None:
        self.failures_remaining = failures
        self.connect_calls = 0
        self.connected = False

    def connect(self, host: str, port: int, client_id: int, market_data_type: int) -> None:
        del host, port, client_id, market_data_type
        self.connect_calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise BrokerAdapterError("socket unavailable")
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def process_events(self, timeout: float = 0.0) -> None:
        del timeout

    def drain_broker_events(self) -> list[dict[str, Any]]:
        return []

    def connectivity_status(self) -> BrokerConnectivityStatus:
        return BrokerConnectivityStatus(
            local_connected=self.connected,
            upstream_connected=self.connected,
            state=("connected" if self.connected else "local_disconnected"),
            message=("ready" if self.connected else "disconnected"),
            market_data_event_tracking=True,
        )


def test_auto_reconnect_retries_every_ten_seconds_without_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "reconnect.sqlite")
    )
    adapter = _ReconnectAdapter(failures=12)
    controller.adapter = adapter
    controller._auto_reconnect_enabled = True
    controller.connected = False
    controller._recover_after_connect = lambda: None
    controller._refresh_confirmed_market_data_if_due = lambda **kwargs: None
    controller.emit_snapshot = lambda *args, **kwargs: None

    clock = [100.0]
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock[0])

    assert controller._attempt_reconnect_if_due() is False
    assert adapter.connect_calls == 1
    assert controller._reconnect_failures == 1

    clock[0] = 109.999
    assert controller._attempt_reconnect_if_due() is False
    assert adapter.connect_calls == 1

    for expected_call in range(2, 13):
        clock[0] = 100.0 + (expected_call - 1) * 10.0
        assert controller._attempt_reconnect_if_due() is False
        assert adapter.connect_calls == expected_call

    assert controller._reconnect_failures == 12
    clock[0] += 10.0
    assert controller._attempt_reconnect_if_due() is True
    assert adapter.connect_calls == 13
    assert controller.connected is True
    assert controller._reconnect_failures == 0
    assert controller._last_reconnect_attempt_monotonic == 0.0


def test_initial_connect_failure_enables_fixed_indefinite_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "initial-connect.sqlite")
    )
    adapter = _ReconnectAdapter(failures=1)
    controller.adapter = adapter
    controller._recover_after_connect = lambda: None
    controller._refresh_confirmed_market_data_if_due = lambda **kwargs: None
    controller.emit_snapshot = lambda *args, **kwargs: None

    clock = [500.0]
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock[0])

    with pytest.raises(BrokerAdapterError, match="retry every 10 seconds"):
        controller._connect(ConnectionSettings())

    assert controller._auto_reconnect_enabled is True
    assert controller.connected is False
    assert controller._reconnect_failures == 1
    assert controller._last_reconnect_attempt_monotonic == 500.0
    assert adapter.connect_calls == 1

    clock[0] = 509.999
    assert controller._attempt_reconnect_if_due() is False
    assert adapter.connect_calls == 1

    clock[0] = 510.0
    assert controller._attempt_reconnect_if_due() is True
    assert adapter.connect_calls == 2
    assert controller.connected is True
    assert controller._reconnect_failures == 0


def test_market_data_variants_preserve_exact_contract_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    requested: list[Any] = []

    def qualify(contract: Any) -> list[Any]:
        requested.append(contract)
        resolved = FakeStock(
            contract.symbol,
            contract.exchange,
            contract.currency,
            primaryExchange=getattr(contract, "primaryExchange", ""),
        )
        resolved.conId = int(getattr(contract, "conId", 0) or 0)
        return [resolved]

    ib.qualifyContracts = qualify
    base_raw = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    base_raw.conId = 1001
    base = QualifiedContract(
        "SAP",
        1001,
        base_raw,
        primary_exchange="IBIS",
        currency="EUR",
        exchange="SMART",
        sec_type="STK",
    )

    direct = adapter._qualified_market_data_variant(base, "IBIS")

    assert direct is not None
    assert direct.con_id == 1001
    assert direct.currency == "EUR"
    assert requested[-1].conId == 1001

    second_raw = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    second_raw.conId = 2002
    second = QualifiedContract(
        "SAP",
        2002,
        second_raw,
        primary_exchange="IBIS",
        currency="EUR",
        exchange="SMART",
        sec_type="STK",
    )
    assert adapter._qualified_market_data_variant(second, "IBIS") is not None
    assert len(adapter._variant_cache) == 2

    def wrong_identity(contract: Any) -> list[Any]:
        resolved = FakeStock(contract.symbol, contract.exchange, "USD")
        resolved.conId = 9999
        return [resolved]

    ib.qualifyContracts = wrong_identity
    third_raw = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    third_raw.conId = 3003
    third = QualifiedContract(
        "SAP",
        3003,
        third_raw,
        primary_exchange="IBIS",
        currency="EUR",
        exchange="SMART",
        sec_type="STK",
    )
    assert adapter._qualified_market_data_variant(third, "IBIS") is None


def test_manual_disconnect_disables_indefinite_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "disconnect.sqlite")
    )
    adapter = _ReconnectAdapter(failures=0)
    adapter.connected = True
    controller.adapter = adapter
    controller.connected = True
    controller._auto_reconnect_enabled = True
    controller.emit_snapshot = lambda *args, **kwargs: None

    controller._disconnect()

    assert controller._auto_reconnect_enabled is False
    assert controller.connected is False
    assert controller._attempt_reconnect_if_due() is False
    assert adapter.connect_calls == 0


def test_controller_search_marks_other_currency_unsupported_after_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "search.sqlite")
    storage.upsert_cycle(
        _cycle(StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001))
    )
    controller = controller_module.TradingController(storage=storage)

    class _SearchAdapter:
        def search_stock_contracts(self, query: str) -> list[ContractSearchResult]:
            assert query == "A"
            return [
                ContractSearchResult("SAP", "STK", "EUR", "IBIS", "IBIS", 1001),
                ContractSearchResult("AAPL", "STK", "USD", "NASDAQ", "NASDAQ", 1002),
            ]

    controller.adapter = _SearchAdapter()
    controller.connected = True
    controller._require_broker_operation_connectivity = lambda *args, **kwargs: True
    controller.emit_snapshot = lambda *args, **kwargs: None

    controller._search_contracts(controller.connection, "a")

    payload = controller.signals.contract_search_updated.emissions[-1][0][0]
    assert payload[0]["supported"] is True
    assert payload[0]["database_currency_compatible"] is True
    assert payload[1]["supported"] is False
    assert payload[1]["database_currency_compatible"] is False
    assert "locked to EUR" in payload[1]["label"]


def test_controller_requires_exact_contract_and_claims_database_currency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "exact.sqlite")
    controller = controller_module.TradingController(storage=storage)
    controller.adapter = SimpleNamespace(requires_exact_contract_selection=True)

    missing = StrategySettings(ticker="SAP", currency="EUR", contract_con_id=None)
    with pytest.raises(ValueError, match="positive conId"):
        controller._validate_exact_contract_selection(missing, claim_database_currency=True)
    assert storage.database_contract_currency() == ""

    selected = StrategySettings(
        ticker="SAP",
        exchange="SMART",
        primary_exchange="IBIS",
        currency="eur",
        sec_type="STK",
        contract_con_id=1001,
    )
    assert controller._validate_exact_contract_selection(
        selected,
        claim_database_currency=True,
    ) == "EUR"
    assert selected.currency == "EUR"
    assert storage.database_contract_currency_info() == {
        "currency": "EUR",
        "cycle_count": 0,
        "locked": False,
    }

    contract = QualifiedContract(
        ticker="SAP",
        con_id=1001,
        raw=SimpleNamespace(
            currency="EUR",
            exchange="SMART",
            secType="STK",
        ),
        currency="EUR",
        exchange="SMART",
        sec_type="STK",
    )
    controller._verify_qualified_contract(contract, selected)

    wrong_currency = QualifiedContract(
        ticker="SAP",
        con_id=1001,
        raw=SimpleNamespace(
            currency="USD",
            exchange="SMART",
            secType="STK",
        ),
        currency="USD",
        exchange="SMART",
        sec_type="STK",
    )
    with pytest.raises(ValueError, match="not EUR"):
        controller._verify_qualified_contract(wrong_currency, selected)


def test_commission_in_other_currency_is_excluded_without_fx_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "commission.sqlite")
    settings = StrategySettings(
        ticker="SAP",
        currency="EUR",
        contract_con_id=1001,
        auto_repeat=True,
    )
    cycle = _cycle(settings)
    cycle.buy_order_ref = "IBKRBOT|SAP|CYCLE-000001|TEST|BUY_TRAIL"
    storage.upsert_cycle(cycle)
    controller = controller_module.TradingController(storage=storage)
    controller.active_cycle = cycle

    first = controller._commission_in_cycle_currency(
        cycle,
        0.75,
        "USD",
        execution_id="EXEC-EUR-1",
        source="COMMISSION_REPORT",
    )
    second = controller._commission_in_cycle_currency(
        cycle,
        0.75,
        "USD",
        execution_id="EXEC-EUR-1",
        source="ORDER_POLL_EXECUTION",
    )

    assert first is None
    assert second is None
    persisted = storage.get_cycle(cycle.id)
    assert persisted is not None and persisted.stop_after_current_cycle is True
    with storage.connect() as con:
        rows = con.execute(
            "SELECT event_type, decision_result FROM decision_events WHERE cycle_id=?",
            (cycle.id,),
        ).fetchall()
    assert [(row["event_type"], row["decision_result"]) for row in rows] == [
        ("COMMISSION_CURRENCY_MISMATCH", "commission_excluded_no_fx_conversion")
    ]


def test_eur_gui_uses_exact_contract_currency_and_matching_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.support.qt_stubs import imported_gui_with_stubs

    with imported_gui_with_stubs(Path.cwd()) as gui_module:
        gui_module._set_active_contract_currency("EUR")
        row = gui_module.MainWindow._example_history_row()
        details = gui_module.MainWindow._example_audit_details(row)

        assert gui_module._format_currency(123.45, 2) == "€123.45"
        assert row["ticker"] == "SAP"
        assert row["currency"] == "EUR"
        assert row["primary_exchange"] == "IBIS"
        assert all(item["raw_json"]["exchange"] == "IBIS" for item in details["executions"])
        assert "€210.5700" in details["decision_events"][3]["message"]


def test_ib_unset_size_sentinels_fall_back_to_single_whole_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    qualified = FakeStock("SAP", "SMART", "EUR", primaryExchange="IBIS")
    qualified.conId = 1001
    ib.qualified_contracts = [qualified]
    ib.contract_details = [
        _detail(
            qualified,
            min_size=1.7976931348623157e308,
            size_increment=1.7976931348623157e308,
        )
    ]

    contract = adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)

    assert contract.min_size == 1.0
    assert contract.size_increment == 1.0
    assert adapter.normalize_order_quantity(contract, 37) == 37


def test_active_cycle_currency_conflict_blocks_recovery_without_rewriting_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "active-conflict.sqlite")
    settings = StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001)
    cycle = _cycle(settings)
    storage.upsert_cycle(cycle)
    with storage.connect() as con:
        con.execute(
            "UPDATE app_settings SET value_json='\"USD\"' "
            "WHERE key='database_contract_currency'"
        )

    controller = controller_module.TradingController(storage=storage)
    controller.adapter = SimpleNamespace(requires_exact_contract_selection=True)
    controller.emit_snapshot = lambda *args, **kwargs: None

    controller._start_strategy(settings)

    assert controller._recovery_required is True
    assert "conflicts with the portable database currency lock" in controller.status
    with storage.connect() as con:
        row = con.execute(
            "SELECT currency, stage FROM cycles WHERE id=?",
            (cycle.id,),
        ).fetchone()
    assert row is not None
    assert row["currency"] == "EUR"
    assert row["stage"] == cycle.stage.value
