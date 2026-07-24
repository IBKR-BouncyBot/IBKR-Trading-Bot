"""Same-release v3.2.0 regressions found during the USD/EUR/reconnect bugscan."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import BrokerAdapterError, IbAsyncTwsAdapter, QualifiedContract
from app.models import ConnectionSettings, StrategySettings
from app.storage import BotStorage, DatabaseCurrencyError
from app.strategy import StrategyEngine
from tests.test_comprehensive_ib_adapter import FakeIB, FakeOrder, FakeStock
from tests.test_controller_headless import _install_qt_stub
from tests.test_v320_eur_smart_reconnect import _ReconnectAdapter


def _live_adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[IbAsyncTwsAdapter, FakeIB]:
    adapter = IbAsyncTwsAdapter()
    ib = FakeIB()
    adapter.ib = ib
    adapter._upstream_connected = True
    adapter._upstream_state = "connected"
    adapter._upstream_message = "ready"
    monkeypatch.setattr(adapter, "_require_ib_async", lambda: (FakeIB, FakeOrder, FakeStock))
    return adapter, ib


def _detail(contract: Any) -> Any:
    return SimpleNamespace(
        contract=contract,
        minTick=0.01,
        validExchanges="SMART,IBIS,AEB",
        orderTypes="MKT,TRAIL,WHATIF",
        marketRuleIds="26,26,26",
        liquidHours="20260724:0900-20260724:1730",
        timeZoneId="Europe/Berlin",
        minSize=1.0,
        sizeIncrement=1.0,
    )


def _eur_cycle() -> Any:
    settings = StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001)
    return StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)


@pytest.mark.parametrize(
    ("symbol", "exchange", "primary_exchange", "message"),
    [
        ("SAPE", "SMART", "IBIS", "symbol SAPE"),
        ("SAP", "IBIS", "IBIS", "order exchange IBIS"),
        ("SAP", "SMART", "AEB", "primary exchange AEB"),
    ],
)
def test_exact_contract_qualification_rejects_identity_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    exchange: str,
    primary_exchange: str,
    message: str,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    qualified = FakeStock(symbol, exchange, "EUR", primaryExchange=primary_exchange)
    qualified.conId = 1001
    ib.qualified_contracts = [qualified]
    ib.contract_details = [_detail(qualified)]

    with pytest.raises(BrokerAdapterError, match=message):
        adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)


def test_controller_rejects_qualified_primary_exchange_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "identity.sqlite")
    )
    settings = StrategySettings(
        ticker="SAP",
        currency="EUR",
        primary_exchange="IBIS",
        contract_con_id=1001,
    )
    contract = QualifiedContract(
        ticker="SAP",
        con_id=1001,
        raw=SimpleNamespace(
            symbol="SAP",
            currency="EUR",
            exchange="SMART",
            primaryExchange="AEB",
            secType="STK",
        ),
        primary_exchange="AEB",
        currency="EUR",
        exchange="SMART",
        sec_type="STK",
    )

    with pytest.raises(ValueError, match="primary exchange AEB"):
        controller._verify_qualified_contract(contract, settings)


def test_resume_checkpoint_cannot_bypass_locked_database_currency(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "checkpoint.sqlite")
    eur_settings = StrategySettings(ticker="SAP", currency="EUR", contract_con_id=1001)
    storage.save_strategy_settings(eur_settings)
    storage.upsert_cycle(_eur_cycle())

    with pytest.raises(DatabaseCurrencyError, match="locked to EUR"):
        storage.save_resume_checkpoint(
            ConnectionSettings(),
            StrategySettings(ticker="AAPL", currency="USD", contract_con_id=2002),
            None,
            reason="application_shutdown",
            checkpoint_id="currency-conflict",
        )

    assert storage.load_strategy_settings().currency == "EUR"
    assert storage.get_json("last_resume_checkpoint", None) is None


def test_zero_commission_in_other_currency_does_not_disable_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "zero-commission.sqlite")
    cycle = _eur_cycle()
    storage.upsert_cycle(cycle)
    controller = controller_module.TradingController(storage=storage)
    controller.active_cycle = cycle

    accepted = controller._commission_in_cycle_currency(
        cycle,
        0.0,
        "USD",
        execution_id="ZERO-COMMISSION",
        source="COMMISSION_REPORT",
    )

    assert accepted == 0.0
    persisted = storage.get_cycle(cycle.id)
    assert persisted is not None and persisted.stop_after_current_cycle is False
    with storage.connect() as con:
        count = con.execute(
            "SELECT COUNT(*) AS n FROM decision_events WHERE cycle_id=?",
            (cycle.id,),
        ).fetchone()["n"]
    assert count == 0


def test_commission_currency_mismatch_is_persistently_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "commission-idempotence.sqlite")
    cycle = _eur_cycle()
    storage.upsert_cycle(cycle)

    first_controller = controller_module.TradingController(storage=storage)
    first_controller.active_cycle = cycle
    assert first_controller._commission_in_cycle_currency(
        cycle,
        0.75,
        "USD",
        execution_id="DUPLICATE-MISMATCH",
        source="COMMISSION_REPORT",
    ) is None

    reloaded = storage.get_cycle(cycle.id)
    assert reloaded is not None
    second_controller = controller_module.TradingController(storage=storage)
    second_controller.active_cycle = reloaded
    assert second_controller._commission_in_cycle_currency(
        reloaded,
        0.75,
        "USD",
        execution_id="DUPLICATE-MISMATCH",
        source="ORDER_POLL_EXECUTION",
    ) is None

    with storage.connect() as con:
        count = con.execute(
            """
            SELECT COUNT(*) AS n
            FROM decision_events
            WHERE cycle_id=? AND event_type='COMMISSION_CURRENCY_MISMATCH'
            """,
            (cycle.id,),
        ).fetchone()["n"]
    assert count == 1


def test_reconnect_interval_is_honored_when_monotonic_clock_starts_at_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "reconnect-zero.sqlite")
    )
    adapter = _ReconnectAdapter(failures=2)
    controller.adapter = adapter
    controller._auto_reconnect_enabled = True
    controller.connected = False
    controller.emit_snapshot = lambda *args, **kwargs: None

    clock = [0.0]
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock[0])

    assert controller._attempt_reconnect_if_due() is False
    assert adapter.connect_calls == 1

    clock[0] = 9.999
    assert controller._attempt_reconnect_if_due() is False
    assert adapter.connect_calls == 1

    clock[0] = 10.0
    assert controller._attempt_reconnect_if_due() is False
    assert adapter.connect_calls == 2


def test_market_data_variant_uses_qualified_eur_when_raw_currency_is_blank(
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
    raw = FakeStock("SAP", "SMART", "", primaryExchange="IBIS")
    raw.conId = 1001
    contract = QualifiedContract(
        ticker="SAP",
        con_id=1001,
        raw=raw,
        primary_exchange="IBIS",
        currency="EUR",
        exchange="SMART",
        sec_type="STK",
    )

    variant = adapter._qualified_market_data_variant(contract, "IBIS")

    assert variant is not None
    assert variant.currency == "EUR"
    assert requested[-1].currency == "EUR"
    assert ("SAP", "EUR", "IBIS", "", 1001) in adapter._variant_cache


def test_commission_currency_mismatch_dedupes_legacy_same_release_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-hotfix v3.2.0 event must suppress the replayed callback after update."""
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "commission-legacy-idempotence.sqlite")
    cycle = _eur_cycle()
    cycle.stop_after_current_cycle = True
    storage.upsert_cycle(cycle)
    storage.add_decision_event(
        event_type="COMMISSION_CURRENCY_MISMATCH",
        message="Legacy same-release mismatch event.",
        cycle=cycle,
        stage_before=cycle.stage.value,
        stage_after=cycle.stage.value,
        decision_result="commission_excluded_no_fx_conversion",
        raw={
            "execution_id": "LEGACY-MISMATCH",
            "commission": 0.75,
            "commission_currency": "USD",
            "cycle_currency": "EUR",
            "source": "COMMISSION_REPORT",
        },
    )

    controller = controller_module.TradingController(storage=storage)
    controller.active_cycle = storage.get_cycle(cycle.id)
    assert controller.active_cycle is not None
    assert controller._commission_in_cycle_currency(
        controller.active_cycle,
        0.75,
        "USD",
        execution_id="LEGACY-MISMATCH",
        source="ORDER_POLL_EXECUTION",
    ) is None

    with storage.connect() as con:
        count = con.execute(
            """
            SELECT COUNT(*) AS n
            FROM decision_events
            WHERE cycle_id=? AND event_type='COMMISSION_CURRENCY_MISMATCH'
            """,
            (cycle.id,),
        ).fetchone()["n"]
    assert count == 1


def test_qualified_contract_retains_selected_primary_exchange_when_ibkr_field_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = _live_adapter(monkeypatch)
    qualified = FakeStock("SAP", "SMART", "EUR", primaryExchange="")
    qualified.conId = 1001
    ib.qualified_contracts = [qualified]
    ib.contract_details = [_detail(qualified)]

    contract = adapter.qualify_stock("SAP", "SMART", "EUR", "IBIS", 1001)

    assert contract.primary_exchange == "IBIS"
