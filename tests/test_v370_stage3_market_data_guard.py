"""v3.7.0 field-freshness and Stage-3 final-SELL regressions."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import IbAsyncTwsAdapter, MarketPriceSnapshot, QualifiedContract
from app.models import Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine, make_order_ref
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


class _MarketPriceTicker:
    """Small ib_async-compatible ticker with the documented marketPrice shape."""

    def __init__(self) -> None:
        self.contract = SimpleNamespace(conId=777, exchange="SMART", primaryExchange="NASDAQ")
        self.marketDataType = 1
        self.time = utc_now_iso()
        self.last = 129.44
        self.delayedLast = None
        self.bid = 127.82
        self.ask = 128.10
        self.delayedBid = None
        self.delayedAsk = None
        self.close = 128.00
        self.delayedClose = None
        self.markPrice = None
        self.delayedMarkPrice = None
        self.ticks: list[Any] = []

    def marketPrice(self) -> float:
        bid = self.bid
        ask = self.ask
        last = self.last
        if bid is not None and ask is not None and bid > 0 and ask >= bid:
            if last is not None and bid <= last <= ask:
                return float(last)
            return (float(bid) + float(ask)) / 2.0
        if last is not None:
            return float(last)
        return float(self.close)


def _live_tracking_adapter(ticker: _MarketPriceTicker) -> tuple[IbAsyncTwsAdapter, QualifiedContract]:
    adapter = IbAsyncTwsAdapter()
    adapter._market_data_event_tracking_available = True
    adapter._upstream_connected = True
    adapter._upstream_state = "connected"
    adapter._upstream_message = "Connected"
    key = (777, "default", "SMART", "NASDAQ")
    ticker_id = id(ticker)
    adapter._ticker_keys_by_id[ticker_id] = key
    adapter._ticker_update_meta[ticker_id] = adapter._empty_ticker_update_meta(
        key,
        "777|default|SMART|NASDAQ|g1",
    )
    contract = QualifiedContract(
        ticker="CHIP",
        con_id=777,
        raw=ticker.contract,
        exchange="SMART",
        primary_exchange="NASDAQ",
        min_tick=0.01,
    )
    return adapter, contract


def _tracked_quote_snapshot(
    sequence: int,
    *,
    bid: float | None,
    ask: float | None,
    last: float | None = None,
    selected_price: float | None = None,
    selected_basis: str = "bid_ask",
    basis_changed: bool = True,
    quote_sequence: int | None = None,
    spread_source: str = "marketPrice",
) -> MarketPriceSnapshot:
    now = utc_now_iso()
    fields: dict[str, float | None] = {
        "bid": bid,
        "ask": ask,
        "last": last,
        "marketPrice": selected_price,
        "bidAskMidpoint": ((bid + ask) / 2.0 if bid is not None and ask is not None and ask >= bid else None),
        "close": 100.0,
    }
    basis_fields = ["bid", "ask"] if selected_basis == "bid_ask" else ["last"]
    quote_field_sequence = quote_sequence if quote_sequence is not None else sequence
    field_sequences = {"bid": quote_field_sequence, "ask": quote_field_sequence}
    field_times = {"bid": now, "ask": now}
    field_ages: dict[str, float | None] = {"bid": 0.0, "ask": 0.0, "last": None}
    quote_updated_in_event = quote_field_sequence == sequence
    updated = ["bid", "ask"] if quote_updated_in_event else []
    changed = list(updated)
    if last is not None:
        field_sequences["last"] = sequence if basis_changed else max(1, sequence - 1)
        field_times["last"] = now
        field_ages["last"] = 0.0
        if basis_changed:
            updated.append("last")
            changed.append("last")
    return MarketPriceSnapshot(
        price=selected_price,
        source=spread_source,
        requested_market_data_type=1,
        subscription_market_data_type=1,
        fields=fields,
        timestamp=now,
        status="OK",
        api_data_received=True,
        api_data_field_count=sum(value is not None for value in fields.values()),
        market_data_update_sequence=sequence,
        market_data_subscription_id="CHIP|777|g1",
        market_data_update_received_at=now,
        market_data_update_age_seconds=0.0,
        market_data_event_tracking=True,
        market_data_event_tracking_available=True,
        market_data_field_tracking=True,
        market_data_field_tracking_source="test_values",
        field_update_sequences=dict(field_sequences),
        field_update_received_at=dict(field_times),
        field_update_age_seconds=dict(field_ages),
        fields_updated_in_event=list(updated),
        field_change_sequences=field_sequences,
        field_change_received_at=field_times,
        field_change_age_seconds=field_ages,
        fields_changed_in_update=changed,
        quote_update_sequence=quote_sequence if quote_sequence is not None else sequence,
        quote_update_received_at=now,
        quote_update_age_seconds=0.0,
        selected_price_basis=selected_basis,
        selected_price_basis_fields=basis_fields,
        selected_price_basis_update_sequence=(sequence if basis_changed else max(1, sequence - 1)),
        selected_price_basis_received_at=now,
        selected_price_basis_age_seconds=0.0,
        selected_price_basis_updated_in_event=basis_changed,
        selected_price_basis_changed_in_update=basis_changed,
        upstream_connected=True,
        upstream_state="connected",
        upstream_message="Connected",
    )


def _stage3_controller(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = DeterministicBrokerAdapter(ticker="CHIP", con_id=777)
    controller.adapter = adapter
    controller.connected = True
    controller.contract = adapter.contract
    controller._broker_connectivity = {
        "local_connected": True,
        "upstream_connected": True,
        "state": "connected",
        "message": "Connected",
        "trading_ready": True,
    }
    controller._broker_connectivity_initialized = True
    settings = StrategySettings(
        ticker="CHIP",
        investment_amount=10_000.0,
        rise_trigger_pct=1.0,
        sell_trailing_stop_pct=0.2,
        atr_adaptive_enabled=False,
        max_spread_pct=0.5,
        hard_risk_limits_enabled=False,
        stale_data_guard_enabled=True,
        max_selected_price_age_seconds=3.0,
        max_bid_ask_age_seconds=3.0,
        rth_only=True,
        exchange="SMART",
        primary_exchange="NASDAQ",
        contract_con_id=777,
    )
    controller.strategy = settings
    cycle = StrategyEngine.start_cycle(settings, 3, "DU_TEST", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.avg_buy_price = 100.0
    cycle.buy_filled_qty = 10
    cycle.quantity = 10
    cycle.buy_status = "Filled"
    cycle.rise_trigger_price = StrategyEngine.recalculate_rise_trigger_price(cycle)
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return controller, adapter, cycle


def _advance_with_snapshot(controller: Any, cycle: Any, snapshot: MarketPriceSnapshot):
    controller._record_price_snapshot(snapshot, controller.contract)
    return controller._advance_waiting_cycle_from_price(
        cycle,
        float(snapshot.price or 0.0),
        is_rth=True,
        rth_message="RTH open",
    )


def test_chip_wide_ask_and_missing_bid_do_not_refresh_unchanged_last_or_atr(tmp_path, monkeypatch):
    ticker = _MarketPriceTicker()
    adapter, contract = _live_tracking_adapter(ticker)
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = StrategySettings(ticker="CHIP", atr_adaptive_enabled=False)
    controller._latest_rth_status = {
        "is_open": True,
        "message": "RTH open",
        "checked_at": utc_now_iso(),
    }

    adapter._on_pending_tickers([ticker])
    normal = adapter._snapshot_from_ticker(ticker, contract)
    assert normal.selected_price_basis == "bid_ask"
    controller._record_price_snapshot(normal, contract)
    assert controller.price_snapshot["strategy_price_usable"] is True
    starting_atr_observations = len(controller._price_history)

    # Exact first CHIP incident shape: ask widens around an unchanged Last.
    ticker.ask = 130.42
    ticker.time = utc_now_iso()
    adapter._on_pending_tickers([ticker])
    wide = adapter._snapshot_from_ticker(ticker, contract)
    assert wide.price == 129.44
    assert wide.selected_price_basis == "last"
    assert wide.selected_price_basis_changed_in_update is False
    assert "ask" in (wide.fields_changed_in_update or [])
    assert "ask" in (wide.fields_updated_in_event or [])
    assert "last" not in (wide.fields_changed_in_update or [])
    assert "last" not in (wide.fields_updated_in_event or [])
    controller._record_price_snapshot(wide, contract)
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert controller.price_snapshot["api_data_state"] == "selected_field_unchanged"
    assert len(controller._price_history) == starting_atr_observations

    # Restore the quote, then reproduce the exact second incident shape: bid
    # disappears while ask and the cached Last remain unchanged.
    ticker.ask = 128.04
    adapter._on_pending_tickers([ticker])
    controller._record_price_snapshot(adapter._snapshot_from_ticker(ticker, contract), contract)
    restored_count = len(controller._price_history)

    ticker.bid = None
    adapter._on_pending_tickers([ticker])
    missing_bid = adapter._snapshot_from_ticker(ticker, contract)
    assert missing_bid.price == 129.44
    assert missing_bid.selected_price_basis == "last"
    assert missing_bid.selected_price_basis_changed_in_update is False
    assert "bid" in (missing_bid.fields_changed_in_update or [])
    assert "bid" in (missing_bid.fields_updated_in_event or [])
    assert "last" not in (missing_bid.fields_changed_in_update or [])
    assert "last" not in (missing_bid.fields_updated_in_event or [])
    controller._record_price_snapshot(missing_bid, contract)
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert len(controller._price_history) == restored_count


def test_field_freshness_invalidation_preserves_comparison_baseline():
    ticker = _MarketPriceTicker()
    adapter, contract = _live_tracking_adapter(ticker)
    adapter._on_pending_tickers([ticker])
    initial = adapter._snapshot_from_ticker(ticker, contract)
    assert {"bid", "ask", "last"}.issubset(set(initial.fields_changed_in_update or []))

    adapter._invalidate_market_data_event_state()
    ticker.ask = 128.20
    adapter._on_pending_tickers([ticker])
    recovered = adapter._snapshot_from_ticker(ticker, contract)

    # The farm-reset invalidation clears freshness but retains values as a
    # comparison baseline. Only the changed ask (and its derived marketPrice)
    # may become fresh; the cached bid and Last remain un-stamped.
    assert "ask" in (recovered.fields_changed_in_update or [])
    assert "ask" in (recovered.fields_updated_in_event or [])
    assert "bid" not in (recovered.fields_changed_in_update or [])
    assert "last" not in (recovered.fields_changed_in_update or [])
    assert recovered.field_update_age_seconds["bid"] is None
    assert recovered.field_update_age_seconds["last"] is None


def test_price_tick_types_refresh_same_value_field_but_size_ticks_do_not():
    ticker = _MarketPriceTicker()
    adapter, contract = _live_tracking_adapter(ticker)
    adapter._on_pending_tickers([ticker])

    ticker.ticks = [SimpleNamespace(tickType=1, price=ticker.bid)]
    adapter._on_pending_tickers([ticker])
    same_bid = adapter._snapshot_from_ticker(ticker, contract)
    assert "bid" in (same_bid.fields_updated_in_event or [])
    assert "bid" not in (same_bid.fields_changed_in_update or [])
    assert "last" not in (same_bid.fields_changed_in_update or [])
    bid_sequence = same_bid.field_update_sequences["bid"]

    ticker.ticks = [SimpleNamespace(tickType=5, size=100)]
    adapter._on_pending_tickers([ticker])
    last_size_only = adapter._snapshot_from_ticker(ticker, contract)
    assert last_size_only.fields_changed_in_update == []
    assert last_size_only.fields_updated_in_event == []
    assert last_size_only.field_update_sequences["bid"] == bid_sequence
    assert last_size_only.selected_price_basis_updated_in_event is False

    # Last-timestamp messages are metadata, not trade-price updates. The old
    # CHIP Last must remain stale when only tick 45/88 arrives.
    previous_last_sequence = last_size_only.field_update_sequences["last"]
    for timestamp_tick in (45, 88):
        ticker.ticks = [SimpleNamespace(tickType=timestamp_tick)]
        adapter._on_pending_tickers([ticker])
        timestamp_only = adapter._snapshot_from_ticker(ticker, contract)
        assert "last" not in (timestamp_only.fields_updated_in_event or [])
        assert timestamp_only.field_update_sequences["last"] == previous_last_sequence



def test_worker_cycle_can_confirm_on_fresh_bid_when_selected_marketprice_is_cached_last(
    tmp_path,
    monkeypatch,
):
    controller, adapter, cycle = _stage3_controller(tmp_path, monkeypatch)

    # Reproduce the important distinction from CHIP without the invalid spread:
    # the convenience marketPrice still resolves to an unchanged Last, while a
    # fresh, narrow quote supplies an executable bid above the rise trigger.
    first = _tracked_quote_snapshot(
        1,
        bid=102.00,
        ask=102.10,
        last=102.05,
        selected_price=102.05,
        selected_basis="last",
        basis_changed=False,
    )
    adapter._snapshot = first
    controller._run_strategy_cycle()
    assert adapter.placed_orders == []
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert controller._stage3_sell_confirmation["sequence"] == 1

    second = _tracked_quote_snapshot(
        2,
        bid=102.02,
        ask=102.12,
        last=102.05,
        selected_price=102.05,
        selected_basis="last",
        basis_changed=False,
    )
    adapter._snapshot = second
    controller._run_strategy_cycle()

    assert len(adapter.placed_orders) == 1
    assert adapter.placed_orders[0]["action"] == "SELL"
    assert adapter.placed_orders[0]["initial_stop_price"] < 102.02
    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE

def test_stage3_requires_two_distinct_valid_quote_confirmations_and_uses_bid(tmp_path, monkeypatch):
    controller, adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    trigger = float(cycle.rise_trigger_price or 0.0)
    assert trigger > 101.0

    first_snapshot = _tracked_quote_snapshot(
        1,
        bid=102.00,
        ask=102.10,
        last=102.05,
        selected_price=102.05,
        selected_basis="bid_ask",
    )
    first_cycle, first_actions = _advance_with_snapshot(controller, cycle, first_snapshot)
    assert first_actions == []
    assert first_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller._stage3_sell_confirmation["sequence"] == 1

    second_snapshot = _tracked_quote_snapshot(
        2,
        bid=102.02,
        ask=102.12,
        last=102.07,
        selected_price=102.07,
        selected_basis="bid_ask",
    )
    second_cycle, second_actions = _advance_with_snapshot(controller, first_cycle, second_snapshot)
    assert [action.action_type for action in second_actions] == ["PLACE_SELL_TRAIL"]
    assert second_actions[0].payload["reference_price"] == 102.02
    marker = second_actions[0].payload["stage3_market_data_guard"]
    assert marker["sequence"] == 2
    assert marker["bid"] == 102.02
    assert second_cycle.stage == Stage.SELL_TRAIL_ACTIVE

    assert controller._commit_waiting_transition(second_cycle, second_actions) is True
    assert len(adapter.placed_orders) == 1
    assert adapter.placed_orders[0]["action"] == "SELL"


def test_stage3_second_confirmation_must_include_a_new_quote_update(tmp_path, monkeypatch):
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    first_cycle, first_actions = _advance_with_snapshot(
        controller,
        cycle,
        _tracked_quote_snapshot(
            1,
            bid=102.00,
            ask=102.10,
            last=102.05,
            selected_price=102.05,
            quote_sequence=1,
        ),
    )
    assert first_actions == []

    # A distinct Last update with the same still-fresh quote does not count as
    # the second executable-price observation.
    second_cycle, second_actions = _advance_with_snapshot(
        controller,
        first_cycle,
        _tracked_quote_snapshot(
            2,
            bid=102.00,
            ask=102.10,
            last=102.06,
            selected_price=102.06,
            selected_basis="last",
            basis_changed=True,
            quote_sequence=1,
        ),
    )
    assert second_actions == []
    assert controller._stage3_sell_confirmation == {}

    # The intervening non-quote event breaks consecutiveness. The next valid
    # quote starts a new pair and a fourth distinct quote completes it.
    third_cycle, third_actions = _advance_with_snapshot(
        controller,
        second_cycle,
        _tracked_quote_snapshot(
            3,
            bid=102.02,
            ask=102.12,
            last=102.06,
            selected_price=102.07,
            quote_sequence=3,
        ),
    )
    assert third_actions == []
    assert controller._stage3_sell_confirmation["sequence"] == 3

    fourth_cycle, fourth_actions = _advance_with_snapshot(
        controller,
        third_cycle,
        _tracked_quote_snapshot(
            4,
            bid=102.03,
            ask=102.13,
            last=102.06,
            selected_price=102.08,
            quote_sequence=4,
        ),
    )
    assert fourth_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert [action.action_type for action in fourth_actions] == ["PLACE_SELL_TRAIL"]


def test_stage3_blocks_missing_quote_wide_spread_and_bid_below_trigger(tmp_path, monkeypatch):
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)

    missing_bid = _tracked_quote_snapshot(
        1,
        bid=None,
        ask=102.10,
        last=102.05,
        selected_price=102.05,
        selected_basis="last",
        basis_changed=True,
    )
    waiting, actions = _advance_with_snapshot(controller, cycle, missing_bid)
    assert actions == []
    assert waiting.stage == Stage.WAIT_RISE_TRIGGER
    assert controller._stage3_sell_confirmation == {}

    wide = _tracked_quote_snapshot(
        2,
        bid=102.00,
        ask=104.00,
        last=103.00,
        selected_price=103.00,
        selected_basis="last",
        basis_changed=True,
    )
    waiting, actions = _advance_with_snapshot(controller, waiting, wide)
    assert actions == []
    assert controller._stage3_sell_confirmation == {}

    # A fresh Last above the trigger cannot substitute for an executable bid.
    below = _tracked_quote_snapshot(
        3,
        bid=100.00,
        ask=100.10,
        last=103.00,
        selected_price=103.00,
        selected_basis="last",
        basis_changed=True,
    )
    waiting, actions = _advance_with_snapshot(controller, waiting, below)
    assert actions == []
    assert controller._stage3_sell_confirmation == {}


def test_stage3_requires_both_quote_sides_to_be_individually_fresh(tmp_path, monkeypatch):
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    snapshot = _tracked_quote_snapshot(
        1,
        bid=102.00,
        ask=102.10,
        last=102.05,
        selected_price=102.05,
    )
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    assert snapshot.field_update_received_at is not None
    assert snapshot.field_update_age_seconds is not None
    assert snapshot.field_change_received_at is not None
    assert snapshot.field_change_age_seconds is not None
    snapshot.field_update_received_at["ask"] = old_time
    snapshot.field_update_age_seconds["ask"] = 0.0
    snapshot.field_change_received_at["ask"] = old_time
    snapshot.field_change_age_seconds["ask"] = 0.0

    controller._record_price_snapshot(snapshot, controller.contract)
    evidence, message = controller._stage3_sell_quote_evidence(cycle, require_latest_event=True)
    assert evidence is None
    assert "ask" in message
    assert "configured" in message


def test_unusable_fresh_event_resets_pending_stage3_confirmation(tmp_path, monkeypatch):
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    first_cycle, first_actions = _advance_with_snapshot(
        controller,
        cycle,
        _tracked_quote_snapshot(
            1,
            bid=102.00,
            ask=102.10,
            last=102.05,
            selected_price=102.05,
        ),
    )
    assert first_actions == []
    controller.active_cycle = first_cycle
    assert controller._stage3_sell_confirmation

    controller.price_snapshot = {
        "market_data_field_tracking": True,
        "api_data_received_in_latest_read": True,
        "strategy_price_usable": False,
        "strategy_price_block_reason": "unchanged cached Last",
    }
    controller._poll_price_if_due = lambda *args, **kwargs: (False, None)
    controller._run_strategy_cycle()
    assert controller._stage3_sell_confirmation == {}


@pytest.mark.parametrize(
    ("mutated_field", "mutated_value"),
    [("bid", 102.10), ("ask", 102.24)],
)
def test_final_sell_revalidates_before_intent_and_again_before_broker_submit(
    tmp_path,
    monkeypatch,
    mutated_field,
    mutated_value,
):
    controller, adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    first_cycle, _ = _advance_with_snapshot(
        controller,
        cycle,
        _tracked_quote_snapshot(1, bid=102.00, ask=102.10, last=102.05, selected_price=102.05),
    )
    armed_cycle, actions = _advance_with_snapshot(
        controller,
        first_cycle,
        _tracked_quote_snapshot(2, bid=102.02, ask=102.12, last=102.07, selected_price=102.07),
    )
    assert actions

    # Change quote identity before _record_order_intent: no durable order intent
    # and no broker order may be created.
    controller.price_snapshot["market_data_update_sequence"] = 3
    assert controller._commit_waiting_transition(armed_cycle, actions) is True
    assert adapter.placed_orders == []
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER

    # Re-arm, then change the bid immediately after the intent is persisted.
    cycle = controller.active_cycle
    first_cycle, _ = _advance_with_snapshot(
        controller,
        cycle,
        _tracked_quote_snapshot(4, bid=102.10, ask=102.20, last=102.15, selected_price=102.15),
    )
    armed_cycle, actions = _advance_with_snapshot(
        controller,
        first_cycle,
        _tracked_quote_snapshot(5, bid=102.12, ask=102.22, last=102.17, selected_price=102.17),
    )
    original_record = controller._record_order_intent

    def record_then_change_quote(*args: Any, **kwargs: Any) -> None:
        original_record(*args, **kwargs)
        controller.price_snapshot["fields"][mutated_field] = mutated_value

    controller._record_order_intent = record_then_change_quote
    assert controller._commit_waiting_transition(armed_cycle, actions) is True
    assert adapter.placed_orders == []
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    events = controller.storage.cycle_audit_details(controller.active_cycle.id)["decision_events"]
    assert any(row["event_type"] == "SELL_MARKET_DATA_REVALIDATION_BLOCKED" for row in events)


def test_final_sell_revalidation_rejects_selected_price_source_change(tmp_path, monkeypatch):
    controller, adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    first_cycle, _ = _advance_with_snapshot(
        controller,
        cycle,
        _tracked_quote_snapshot(1, bid=102.00, ask=102.10, last=102.05, selected_price=102.05),
    )
    armed_cycle, actions = _advance_with_snapshot(
        controller,
        first_cycle,
        _tracked_quote_snapshot(2, bid=102.02, ask=102.12, last=102.07, selected_price=102.07),
    )
    assert actions

    # The Stage-3 order is based on the executable quote, but the complete
    # selected-price source/basis identity is still part of the confirmed
    # snapshot and must remain stable through both submission boundaries.
    controller.price_snapshot["source"] = "last"
    controller.price_snapshot["selected_price_basis"] = "last"
    assert controller._commit_waiting_transition(armed_cycle, actions) is True
    assert adapter.placed_orders == []
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER


def test_stage3_quote_age_advances_during_slow_pre_submit_work(tmp_path, monkeypatch):
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    snapshot = _tracked_quote_snapshot(
        1,
        bid=102.00,
        ask=102.10,
        last=102.05,
        selected_price=102.05,
    )
    snapshot.quote_update_received_at = old_time
    snapshot.quote_update_age_seconds = 0.0
    controller._record_price_snapshot(snapshot, controller.contract)
    evidence, message = controller._stage3_sell_quote_evidence(cycle, require_latest_event=True)
    assert evidence is None
    assert "above the configured" in message


def _enable_stage3_close_before_rth(controller: Any, cycle: Any) -> None:
    cycle.cancel_sell_and_liquidate_before_close_enabled = True
    cycle.liquidate_before_close_minutes = 5
    controller.storage.upsert_cycle(cycle)
    controller._latest_rth_status = {
        "is_open": True,
        "message": "RTH open",
        "checked_at": utc_now_iso(),
    }
    controller._session_minutes_from_rth_status = lambda: {
        "available": True,
        "minutes_since_open": 60.0,
        "minutes_to_close": 5.0,
        "session_open_display": "13:30 UTC",
        "session_close_display": "20:00 UTC",
        "source": "v370_test",
        "message": "RTH open",
    }
    controller._update_rth_status = lambda contract: dict(controller._latest_rth_status)


def test_stage3_close_before_rth_revalidates_after_protective_cancel(tmp_path, monkeypatch):
    controller, adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    _enable_stage3_close_before_rth(controller, cycle)
    valid = _tracked_quote_snapshot(
        1,
        bid=102.00,
        ask=102.10,
        last=102.05,
        selected_price=102.05,
    )
    controller._record_price_snapshot(valid, controller.contract)

    protective_ref = make_order_ref(
        cycle.ticker,
        cycle.cycle_number,
        cycle.id,
        "PROTECTIVE_SELL_TRAIL",
    )
    handle = adapter.place_trailing_stop(
        contract=adapter.contract,
        action="SELL",
        quantity=cycle.buy_filled_qty,
        trailing_percent=3.0,
        initial_stop_price=97.0,
        order_ref=protective_ref,
        tif="GTC",
        account="DU_TEST",
        outside_rth=False,
    )
    cycle.protective_sell_order_ref = protective_ref
    cycle.protective_sell_order_id = handle.order_id
    cycle.protective_sell_perm_id = handle.perm_id
    cycle.protective_sell_status = handle.status
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    assert controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 102.00) is True
    assert adapter.cancelled_orders == [protective_ref]

    # The protective order is now terminal, but the new current quote has no
    # bid. The close path must not reuse the old selected Last or submit an
    # unverified market SELL after removing the native protection.
    missing_bid = _tracked_quote_snapshot(
        2,
        bid=None,
        ask=102.10,
        last=129.44,
        selected_price=129.44,
        selected_basis="last",
        basis_changed=False,
    )
    controller._record_price_snapshot(missing_bid, controller.contract)
    terminal = adapter.poll_order(protective_ref)
    assert terminal is not None and terminal.status == "Cancelled"
    controller._handle_protective_sell_order_poll(controller.active_cycle, terminal)

    market_orders = [item for item in adapter.placed_orders if item.get("order_type") == "MKT"]
    assert market_orders == []
    assert controller.active_cycle.stage == Stage.ERROR
    assert "fresh complete executable quote" in str(controller.active_cycle.error_message)


def test_stage3_close_before_rth_revalidates_immediately_before_broker_call(tmp_path, monkeypatch):
    controller, adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    _enable_stage3_close_before_rth(controller, cycle)
    valid = _tracked_quote_snapshot(
        1,
        bid=102.00,
        ask=102.10,
        last=102.05,
        selected_price=102.05,
    )
    controller._record_price_snapshot(valid, controller.contract)
    original_record = controller._record_order_intent

    def record_then_remove_bid(*args: Any, **kwargs: Any) -> None:
        original_record(*args, **kwargs)
        controller.price_snapshot["fields"]["bid"] = None

    controller._record_order_intent = record_then_remove_bid
    assert controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 102.00) is True

    market_orders = [item for item in adapter.placed_orders if item.get("order_type") == "MKT"]
    assert market_orders == []
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.sell_status == "SubmitFailed"
    assert controller.active_cycle.close_before_rth_liquidation_requested is False
