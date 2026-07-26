"""v3.2.1 regressions for the production-incident gaps fixed in this release."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import ib_adapter as ib_adapter_module
from app.ib_adapter import IbAsyncTwsAdapter, QualifiedContract
from app.models import Stage, StrategyAction
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.support.incident_replay import load_incident_fixture
from tests.test_controller_headless import _install_qt_stub


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _delayed_vwra_controller(
    controller_module: Any,
    db_path: Path,
) -> tuple[Any, Any, StrategyAction]:
    fixture = load_incident_fixture("vwra_delayed_data_block")
    broker = DeterministicBrokerAdapter(ticker="VWRA", con_id=375855577)
    broker.contract.primary_exchange = "LSEETF"
    broker.contract.min_tick = 0.02
    settings = permissive_strategy(ticker="VWRA")
    settings.contract_con_id = 375855577
    settings.hard_risk_limits_enabled = True
    settings.block_delayed_data_in_live = True
    controller = make_controller(controller_module, db_path, broker, settings)
    controller.emit_snapshot = lambda *args, **kwargs: None
    controller.storage.backup_database = lambda *args, **kwargs: None
    controller.connection.trading_mode = "live"
    market_data = fixture["market_data"]
    controller.price_snapshot = {
        "price": float(market_data["selected_price"]),
        "selected_market_data_type": int(market_data["selected_type"]),
        "subscription_market_data_type": int(market_data["subscription_type"]),
        "fields": {
            "bid": float(market_data["bid"]),
            "ask": float(market_data["ask"]),
            "last": float(market_data["selected_price"]),
            "marketPrice": float(market_data["selected_price"]),
        },
    }
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 186.76, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.quantity = 53
    cycle.block_delayed_data_in_live = True
    cycle.buy_order_ref = "IBKRBOT|VWRA|CYCLE-INCIDENT|LOCAL01|BUY_TRAIL"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    action = StrategyAction(
        "PLACE_BUY_TRAIL",
        {
            "ticker": "VWRA",
            "quantity": 53,
            "order_type": "TRAIL",
            "trailing_percent": 0.1,
            "initial_stop_price": 186.80,
            "reference_price": 186.80,
            "sizing_price": 186.80,
            "budget": 10_000.0,
            "order_ref": cycle.buy_order_ref,
        },
    )
    return controller, cycle, action


def _effective_lse_status(now_utc: dt.datetime):
    fixture = load_incident_fixture("vwra_lse_continuous_close_mismatch")
    contract = fixture["contract"]
    raw = IbAsyncTwsAdapter._parse_liquid_hours_window(
        str(contract["ibkr_liquid_hours"]),
        str(contract["ibkr_time_zone"]),
        now_utc,
    )
    assert raw is not None
    return IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        raw,
        str(contract["primary_exchange"]),
        now_utc,
    )


def test_lse_market_liquidation_boundary_uses_continuous_close() -> None:
    fixture = load_incident_fixture("vwra_lse_continuous_close_mismatch")
    expected_close = dt.datetime.fromisoformat(
        fixture["independent_session_oracle"]["continuous_close_utc"]
    )
    status = _effective_lse_status(dt.datetime.fromisoformat("2026-07-24T15:45:00+00:00"))

    assert status.is_open is False
    assert dt.datetime.fromisoformat(status.session_close).astimezone(
        dt.timezone.utc
    ) == expected_close
    assert dt.datetime.fromisoformat(status.ibkr_session_close).astimezone(
        dt.timezone.utc
    ) == dt.datetime.fromisoformat("2026-07-24T15:50:00+00:00")
    assert status.primary_exchange == "LSEETF"
    assert status.effective_session_policy == "LSEETF_continuous_session"
    assert status.source == "contract_liquid_hours_continuous_session"


def test_lse_continuous_policy_is_open_before_close_and_preserves_earlier_ibkr_close() -> None:
    before_close = _effective_lse_status(
        dt.datetime.fromisoformat("2026-07-24T15:28:00+00:00")
    )
    assert before_close.is_open is True

    early_raw = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260724:0900-20260724:1600",
        "MET",
        dt.datetime.fromisoformat("2026-07-24T13:55:00+00:00"),
    )
    assert early_raw is not None
    early_effective = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        early_raw,
        "LSEETF",
        dt.datetime.fromisoformat("2026-07-24T13:55:00+00:00"),
    )
    assert early_effective.is_open is True
    assert dt.datetime.fromisoformat(early_effective.session_close).astimezone(
        dt.timezone.utc
    ) == dt.datetime.fromisoformat("2026-07-24T14:00:00+00:00")


def test_lse_policy_preserves_a_later_ibkr_open() -> None:
    late_raw = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260724:1030-20260724:1750",
        "MET",
        dt.datetime.fromisoformat("2026-07-24T08:15:00+00:00"),
    )
    assert late_raw is not None
    effective = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        late_raw,
        "LSEETF",
        dt.datetime.fromisoformat("2026-07-24T08:15:00+00:00"),
    )

    assert effective.is_open is False
    assert dt.datetime.fromisoformat(effective.session_open).astimezone(
        dt.timezone.utc
    ) == dt.datetime.fromisoformat("2026-07-24T08:30:00+00:00")


def test_lse_policy_uses_london_winter_time() -> None:
    raw = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20261201:0900-20261201:1750",
        "MET",
        dt.datetime.fromisoformat("2026-12-01T16:40:00+00:00"),
    )
    assert raw is not None
    effective = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        raw,
        "LSE",
        dt.datetime.fromisoformat("2026-12-01T16:40:00+00:00"),
    )

    assert effective.is_open is False
    assert effective.continuous_session_close == "2026-12-01T16:30:00+00:00"
    assert dt.datetime.fromisoformat(effective.session_close).astimezone(
        dt.timezone.utc
    ) == dt.datetime.fromisoformat("2026-12-01T16:30:00+00:00")


def test_lse_policy_handles_winter_offset_and_closes_at_exact_boundary() -> None:
    before = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260115:0900-20260115:1750",
        "MET",
        dt.datetime.fromisoformat("2026-01-15T16:29:59+00:00"),
    )
    assert before is not None
    before = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        before,
        "LSE",
        dt.datetime.fromisoformat("2026-01-15T16:29:59+00:00"),
    )
    assert before.is_open is True
    assert before.session_close == "2026-01-15T17:30:00+01:00"

    at_close = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260115:0900-20260115:1750",
        "MET",
        dt.datetime.fromisoformat("2026-01-15T16:30:00+00:00"),
    )
    assert at_close is not None
    at_close = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        at_close,
        "LSE",
        dt.datetime.fromisoformat("2026-01-15T16:30:00+00:00"),
    )
    assert at_close.is_open is False
    assert at_close.session_close == "2026-01-15T17:30:00+01:00"


def test_controller_timing_uses_effective_lse_close(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    broker = DeterministicBrokerAdapter(ticker="VWRA", con_id=375855577)
    controller = make_controller(
        controller_module,
        tmp_path / "lse-effective-timing.sqlite",
        broker,
        permissive_strategy(ticker="VWRA"),
    )
    now = dt.datetime.fromisoformat("2026-07-24T15:25:00+00:00")
    controller._latest_rth_status = _effective_lse_status(now).to_dict()

    timing = controller._session_minutes_from_rth_status(now_utc=now)

    assert timing["available"] is True
    assert timing["minutes_to_close"] == pytest.approx(5.0)
    assert timing["session_close_display"] == "17:30 CEST"


def test_non_lse_contract_keeps_ibkr_liquid_hours_boundary() -> None:
    raw = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260724:0900-20260724:1750",
        "MET",
        dt.datetime.fromisoformat("2026-07-24T15:45:00+00:00"),
    )
    assert raw is not None
    unchanged = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        raw,
        "NASDAQ",
        dt.datetime.fromisoformat("2026-07-24T15:45:00+00:00"),
    )
    assert unchanged.is_open is True
    assert dt.datetime.fromisoformat(unchanged.session_close).astimezone(
        dt.timezone.utc
    ) == dt.datetime.fromisoformat("2026-07-24T15:50:00+00:00")


def test_regular_trading_hours_status_applies_lse_policy_to_cached_contract_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = dt.datetime.fromisoformat("2026-07-24T15:45:00+00:00")

    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(ib_adapter_module, "datetime", FixedDateTime)
    adapter = IbAsyncTwsAdapter()
    adapter.ib = SimpleNamespace(isConnected=lambda: True)
    contract = QualifiedContract(
        ticker="VWRA",
        con_id=375855577,
        raw=SimpleNamespace(primaryExchange="LSEETF", exchange="SMART"),
        primary_exchange="LSEETF",
        currency="USD",
        exchange="SMART",
        liquid_hours="20260724:0900-20260724:1750",
        time_zone="MET",
    )

    status = adapter.regular_trading_hours_status(contract)

    assert status.is_open is False
    assert status.session_close == "2026-07-24T17:30:00+02:00"
    assert status.ibkr_session_close == "2026-07-24T17:50:00+02:00"


def test_cached_lse_status_cannot_remain_open_after_continuous_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [dt.datetime.fromisoformat("2026-07-24T15:29:30+00:00")]

    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            value = current[0]
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(ib_adapter_module, "datetime", FixedDateTime)
    adapter = IbAsyncTwsAdapter()
    adapter.ib = SimpleNamespace(isConnected=lambda: True)
    contract = QualifiedContract(
        ticker="VWRA",
        con_id=375855577,
        raw=SimpleNamespace(primaryExchange="LSEETF", exchange="SMART"),
        primary_exchange="LSEETF",
        currency="USD",
        exchange="SMART",
        liquid_hours="20260724:0900-20260724:1750",
        time_zone="MET",
    )

    before_close = adapter.regular_trading_hours_status(contract)
    assert before_close.is_open is True

    current[0] = dt.datetime.fromisoformat("2026-07-24T15:30:01+00:00")
    after_close = adapter.regular_trading_hours_status(contract)

    assert after_close is before_close
    assert after_close.is_open is False


def test_invalid_lse_continuous_session_metadata_fails_closed() -> None:
    status = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        SimpleNamespace(
            is_open=True,
            source="contract_liquid_hours",
            message="open",
            checked_at="2026-07-24T15:00:00+00:00",
            liquid_hours="20260724:0900-20260724:1750",
            time_zone="MET",
            session_open="not-a-time",
            session_close="2026-07-24T17:50:00+02:00",
            session_date="20260724",
            primary_exchange="",
            effective_session_policy="",
            ibkr_session_open="",
            ibkr_session_close="",
            continuous_session_open="",
            continuous_session_close="",
        ),
        "LSEETF",
        dt.datetime.fromisoformat("2026-07-24T15:00:00+00:00"),
    )

    assert status.is_open is False
    assert status.source == "contract_continuous_session_error"
    assert status.session_open == ""
    assert status.session_close == ""


def test_unchanged_delayed_data_preflight_block_is_audit_throttled(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, cycle, action = _delayed_vwra_controller(
        controller_module,
        tmp_path / "delayed-throttle.sqlite",
    )
    monotonic_now = [100.0]
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: monotonic_now[0])

    for _ in range(20):
        controller._execute_actions([action], cycle)

    with controller.storage.connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%non-live market data mode 3%'"
        ).fetchone()[0]
        normalization_count = con.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE 'Normalized BUY trailing stop%'"
        ).fetchone()[0]
    assert count == 1
    assert normalization_count == 0

    monotonic_now[0] += controller.BUY_PREFLIGHT_AUDIT_THROTTLE_SECONDS + 1.0
    controller._execute_actions([action], cycle)
    with controller.storage.connect() as con:
        count_after_interval = con.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%non-live market data mode 3%'"
        ).fetchone()[0]
    assert count_after_interval == 2


def test_first_preflight_block_is_logged_when_monotonic_clock_is_zero(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, cycle, action = _delayed_vwra_controller(
        controller_module,
        tmp_path / "delayed-zero-clock.sqlite",
    )
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: 0.0)

    controller._execute_actions([action], cycle)

    with controller.storage.connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%non-live market data mode 3%'"
        ).fetchone()[0]
    assert count == 1


def test_delayed_data_local_block_uses_preflight_blocked_status(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller, cycle, action = _delayed_vwra_controller(
        controller_module,
        tmp_path / "delayed-status.sqlite",
    )

    controller._execute_actions([action], cycle)

    assert controller.active_cycle.buy_status == "PreflightBlocked"
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.active_cycle.buy_order_ref is None
    with controller.storage.connect() as con:
        order_count = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert order_count == 0


def test_delayed_data_guard_blocks_zero_trail_market_buy_before_intent(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller, cycle, _ = _delayed_vwra_controller(
        controller_module,
        tmp_path / "delayed-market.sqlite",
    )
    action = StrategyAction(
        "PLACE_BUY_MARKET",
        {
            "ticker": "VWRA",
            "quantity": 53,
            "order_type": "MKT",
            "reference_price": 186.80,
            "sizing_price": 186.80,
            "budget": 10_000.0,
            "order_ref": cycle.buy_order_ref,
        },
    )

    controller._execute_actions([action], cycle)

    assert controller.active_cycle.buy_status == "PreflightBlocked"
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.adapter.placed_orders == []
    with controller.storage.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
