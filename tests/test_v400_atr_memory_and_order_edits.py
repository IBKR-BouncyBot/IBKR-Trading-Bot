"""v4.0.0 RTH checkpoint and next-order scheduling safety regressions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.atr_memory import (
    ATR_SEED_MAX_AGE_SECONDS,
    AtrSessionMemory,
    atr_seed_identity,
    valid_atr_seed,
)
from app.ib_adapter import MarketPriceSnapshot, QualifiedContract
from app.models import CycleState, Stage, StrategySettings
from app.order_edit_policy import (
    NEXT_ORDER_RISK_FIELDS,
    apply_repeat_order_guards,
    apply_waiting_order_guards,
    pending_risk_settings,
    risk_edit_request,
    settings_match_cycle,
)
from app.storage import BotStorage
from tests.test_controller_headless import _install_qt_stub

# A Thursday-close seed can supply the next weekday open within 24 hours.
THURSDAY = datetime(2026, 8, 6, 19, 55, tzinfo=timezone.utc)
FRIDAY = datetime(2026, 8, 7, 13, 30, 1, tzinfo=timezone.utc)


def rth(now):
    return {
        "is_open": True, "checked_at": now.isoformat(),
        "session_open": now.replace(hour=13, minute=30, second=0).isoformat(),
        "session_close": now.replace(hour=20, minute=0, second=0).isoformat(),
    }


def inputs():
    contract = QualifiedContract(ticker="NBIS", con_id=123, currency="USD", primary_exchange="NASDAQ", raw=None)
    settings = StrategySettings(ticker="NBIS", contract_con_id=123, primary_exchange="NASDAQ", atr_period=3, atr_bar_seconds=60)
    return contract, settings


def ready_result(atr=2.0, close=100.0):
    return {
        "ready": True, "period": 3, "bar_seconds": 60,
        "bars_available": 4, "bars_required": 4, "atr": atr,
        "atr_pct": 100.0 * atr / close, "latest_close": close,
        "latest_bar_high": close + 1, "latest_bar_low": close - 1, "true_ranges_used": 3,
    }


def pending_result(bars=0):
    return {"ready": False, "period": 3, "bar_seconds": 60, "bars_available": bars, "bars_required": 4, "atr": None, "atr_pct": None}


class Store:
    def __init__(self):
        self.data = {}
        self.writes = 0
        self.read_error = False
        self.write_error = False

    def get_json(self, key, default=None):
        if self.read_error:
            raise OSError("read unavailable")
        return deepcopy(self.data.get(key, default))

    def set_json(self, key, value):
        self.writes += 1
        if self.write_error:
            raise OSError("write unavailable")
        self.data[key] = deepcopy(value)


def seeded_memory(store=None, *, observed=THURSDAY, status=None):
    store = store if store is not None else Store()
    contract, settings = inputs()
    identity = atr_seed_identity(contract, settings, "live|0")
    memory = AtrSessionMemory(store)
    memory.prepare(identity, rth(observed) if status is None else status, observed.isoformat())
    memory.note_observation(live=True)
    memory.apply(ready_result())
    return memory, identity, store


def test_first_day_warms_normally_and_next_weekday_seed_survives_database_restart(tmp_path):
    storage = BotStorage(tmp_path / "bot.sqlite")
    contract, settings = inputs()
    identity = atr_seed_identity(contract, settings, "live|0")
    first = AtrSessionMemory(storage)
    first.prepare(identity, rth(THURSDAY), THURSDAY.isoformat())
    assert not first.apply(pending_result())["ready"]
    first.note_observation(live=True)
    first.apply(ready_result())
    second = AtrSessionMemory(BotStorage(storage.db_path))
    assert second.prepare(identity, rth(FRIDAY), FRIDAY.isoformat())
    result = second.apply(pending_result(1))
    assert result["ready"] and result["seeded"]
    assert result["atr_pct"] == 2.0
    assert result["live_bars_available"] == 1
    assert result["seed_observed_at"] == THURSDAY.isoformat()
    # A seeded observation must not refresh/roll forward the checkpoint age.
    assert second.seed["observed_at"] == THURSDAY.isoformat()
    assert second.dirty is False
    fresh = second.apply(ready_result(atr=4.0))
    assert not fresh["seeded"] and fresh["atr_pct"] == 4.0


@pytest.mark.parametrize("change", [
    {"version": 2}, {"observed_at": "bad"}, {"observed_at": "2026-08-07T19:55:00"},
    {"observed_at": "2026-08-12T19:55:00+00:00"}, {"session_close": "2026-08-06T13:00:00+00:00"},
    {"identity": {}}, {"snapshot": {}}, {"snapshot": None},
])
def test_bad_seed_metadata_is_rejected(change):
    memory, identity, _ = seeded_memory()
    assert valid_atr_seed(memory.seed, identity, FRIDAY)
    candidate = deepcopy(memory.seed)
    candidate.update(change)
    assert not valid_atr_seed(candidate, identity, FRIDAY)


@pytest.mark.parametrize("key,value", [
    ("ready", False), ("seeded", True), ("period", 14), ("bar_seconds", 300),
    ("bars_available", 2), ("atr", 0), ("atr", float("nan")), ("atr_pct", float("inf")),
    ("atr_pct", 99.0), ("latest_close", -1), ("atr", "bad"),
])
def test_bad_seed_numeric_facts_are_rejected(key, value):
    memory, identity, _ = seeded_memory()
    assert valid_atr_seed(memory.seed, identity, FRIDAY)
    record = deepcopy(memory.seed)
    record["snapshot"][key] = value
    assert not valid_atr_seed(record, identity, FRIDAY)


@pytest.mark.parametrize("key,value", [
    ("ticker", "CHIP"), ("con_id", 456), ("currency", "EUR"), ("primary_exchange", "IBIS2"),
    ("profile", "paper|0"), ("period", 9), ("bar_seconds", 300),
])
def test_seeds_are_scoped_to_contract_profile_and_calculation(key, value):
    memory, identity, store = seeded_memory()
    other = dict(identity)
    other[key] = value
    next_memory = AtrSessionMemory(store)
    next_memory.prepare(other, rth(FRIDAY), FRIDAY.isoformat())
    assert not next_memory.apply(pending_result())["ready"]
    assert next_memory.seed is None
    assert memory.seed is not None


def test_identity_rejects_unqualified_contract_and_uses_normalized_names():
    contract, settings = inputs()
    assert atr_seed_identity(None, settings, "live") is None
    assert atr_seed_identity(replace(contract, con_id=0), settings, "live") is None
    assert atr_seed_identity(replace(contract, currency=""), settings, "live") is None
    assert atr_seed_identity(contract, SimpleNamespace(atr_period="bad"), "live") is None
    assert atr_seed_identity(replace(contract, ticker=" nbis "), settings, "live")["ticker"] == "NBIS"


def test_expired_and_future_seeds_do_not_become_ready():
    memory, identity, store = seeded_memory()
    for now in (THURSDAY - timedelta(minutes=1), THURSDAY + timedelta(days=8)):
        restored = AtrSessionMemory(store)
        restored.prepare(identity, rth(now), now.isoformat())
        assert restored.seed is None
        assert not restored.apply(pending_result())["ready"]
    assert memory.seed is not None


@pytest.mark.parametrize("age_seconds,expected", [
    (-0.000001, False), (0, True), (86399.999999, True),
    (86400, True), (86400.000001, False),
])
def test_seed_age_uses_the_inclusive_24_hour_elapsed_boundary(age_seconds, expected):
    memory, identity, _ = seeded_memory()
    assert ATR_SEED_MAX_AGE_SECONDS == 86400
    now = THURSDAY + timedelta(seconds=age_seconds)
    assert valid_atr_seed(memory.seed, identity, now) is expected


@pytest.mark.parametrize("observed,next_open", [
    (datetime(2026, 8, 7, 19, 55, tzinfo=timezone.utc), datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc)),
    (datetime(2026, 8, 4, 19, 55, tzinfo=timezone.utc), datetime(2026, 8, 6, 13, 30, tzinfo=timezone.utc)),
    (datetime.fromisoformat("2026-03-06T15:55:00-05:00"), datetime.fromisoformat("2026-03-09T09:30:00-04:00")),
    (datetime.fromisoformat("2026-10-30T15:55:00-04:00"), datetime.fromisoformat("2026-11-02T09:30:00-05:00")),
])
def test_weekend_or_missing_weekday_expires_seed_for_restart_and_continuous_run(observed, next_open):
    # Use UTC broker-session fixtures; offset-changing weekends cover DST too.
    observed = observed.astimezone(timezone.utc)
    next_open = next_open.astimezone(timezone.utc)
    prior_status = rth(observed)
    prior_status.update({
        "session_open": (observed - timedelta(hours=6, minutes=25)).isoformat(),
        "session_close": (observed + timedelta(minutes=5)).isoformat(),
    })
    next_status = rth(next_open)
    next_status.update({
        "session_open": next_open.isoformat(),
        "session_close": (next_open + timedelta(hours=6, minutes=30)).isoformat(),
    })
    memory, identity, store = seeded_memory(observed=observed, status=prior_status)
    memory.flush()
    stored = deepcopy(store.data)
    assert valid_atr_seed(memory.seed, identity, observed)
    assert stored[memory.key] == memory.seed
    assert observed.weekday() in {1, 4} and next_open.weekday() in {0, 3}
    for restored in (memory, AtrSessionMemory(store)):
        restored.prepare(identity, next_status, next_open.isoformat())
        assert restored.session is not None
        result = restored.apply(pending_result(1))
        assert not result["ready"] and not result["seeded"]
        assert result["atr"] is None and result["live_bars_available"] == 1
    assert store.data == stored


@pytest.mark.parametrize("offset_hours", [-5, 0, 5.5])
def test_seed_expiry_is_unchanged_by_timestamp_timezone_offsets(offset_hours):
    memory, identity, _ = seeded_memory()
    record = deepcopy(memory.seed)
    zone = timezone(timedelta(hours=offset_hours))
    for key in ("observed_at", "session_open", "session_close"):
        record[key] = datetime.fromisoformat(record[key]).astimezone(zone).isoformat()
    boundary = (THURSDAY + timedelta(hours=24)).astimezone(zone)
    assert valid_atr_seed(record, identity, boundary)
    assert not valid_atr_seed(record, identity, boundary + timedelta(microseconds=1))


def test_seed_already_loaded_expires_during_current_session_without_relabeling():
    _, identity, store = seeded_memory()
    stored = deepcopy(store.data)
    memory = AtrSessionMemory(store)
    memory.prepare(identity, rth(FRIDAY), FRIDAY.isoformat())
    assert memory.apply(pending_result(1))["seeded"]
    expired = THURSDAY + timedelta(hours=24, seconds=1)
    memory.prepare(identity, rth(expired), expired.isoformat())
    result = memory.apply(pending_result(2))
    assert not result["ready"] and not result["seeded"]
    assert result["atr"] is None
    assert memory.seed["observed_at"] == THURSDAY.isoformat()
    memory.flush()
    assert store.data == stored


@pytest.mark.parametrize("status_change", [
    {"is_open": False}, {"session_close": "bad"}, {"checked_at": THURSDAY.isoformat()},
    {"session_open": "2026-08-07T13:30:00"}, {"checked_at": "2026-08-07T14:00:00+00:00"},
])
def test_seed_never_bypasses_unverified_closed_or_stale_rth(status_change):
    _, identity, store = seeded_memory()
    memory = AtrSessionMemory(store)
    status = rth(FRIDAY)
    status.update(status_change)
    memory.prepare(identity, status, FRIDAY.isoformat())
    assert not memory.apply(pending_result())["ready"]


def test_same_session_restart_config_changes_and_zero_atr_fail_closed():
    memory, identity, store = seeded_memory()
    restarted = AtrSessionMemory(store)
    restarted.prepare(identity, rth(THURSDAY), THURSDAY.isoformat())
    assert restarted.apply(pending_result())["seeded"]
    # A completed but zero-volatility current sample is not hidden by old data.
    result = restarted.apply({**pending_result(4), "atr": 0.0, "atr_pct": 0.0})
    assert not result["ready"]
    changed = dict(identity, period=9)
    assert not memory.prepare(changed, rth(THURSDAY), THURSDAY.isoformat())
    assert memory.seed is None
    assert memory.prepare(None, rth(THURSDAY), THURSDAY.isoformat())


def test_checkpoint_writes_are_coalesced_and_latest_is_flushed_at_close():
    memory, identity, store = seeded_memory()
    assert store.writes == 1
    for second in range(1, 20):
        now = THURSDAY + timedelta(seconds=second)
        memory.prepare(identity, rth(now), now.isoformat())
        memory.note_observation(live=True)
        memory.apply(ready_result(2.0 + second / 100))
    assert store.writes == 1 and memory.dirty
    closed = THURSDAY.replace(hour=20, minute=0, second=0)
    memory.prepare(identity, {**rth(closed), "is_open": False}, closed.isoformat())
    assert store.writes == 2
    assert store.data[memory.key]["snapshot"]["atr"] == 2.19


def test_cache_failures_are_optional_visible_and_retries_remain_bounded():
    memory, identity, store = seeded_memory()
    errors = []
    memory.on_error = lambda message, **kwargs: errors.append(message)
    store.write_error = True
    now = THURSDAY + timedelta(seconds=61)
    memory.prepare(identity, rth(now), now.isoformat())
    memory.note_observation(live=True)
    result = memory.apply(ready_result(3))
    assert result["ready"] and "write unavailable" in result["seed_storage_error"]
    assert memory.dirty and len(errors) == 1
    # Close transition allows one last flush; later closed ticks cannot retry
    # a failing optional database write at the broker callback rate.
    closed = now.replace(hour=20, minute=0, second=0)
    memory.prepare(identity, {"is_open": False}, closed.isoformat())
    writes = store.writes
    for second in range(1, 20):
        memory.prepare(identity, {"is_open": False}, (closed + timedelta(seconds=second)).isoformat())
    assert store.writes == writes
    store.write_error = False
    memory.flush()
    assert not memory.dirty and memory.error == ""
    store.read_error = True
    restored = AtrSessionMemory(store, lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("logger failure")))
    restored.prepare(identity, rth(FRIDAY), FRIDAY.isoformat())
    assert not restored.apply(pending_result())["ready"]
    assert "read unavailable" in restored.error
    restored.flush()  # no pending write


def test_delayed_or_mixed_observations_cannot_replace_live_seed():
    memory, identity, store = seeded_memory()
    now = THURSDAY + timedelta(minutes=1)
    memory.prepare(identity, rth(now), now.isoformat())
    memory.note_observation(live=False)
    memory.apply(ready_result(9))
    memory.note_observation(live=True)
    memory.apply(ready_result(10))
    assert memory.seed["snapshot"]["atr"] == 2
    assert store.writes == 1


def controller_with_settings(tmp_path, monkeypatch):
    module = _install_qt_stub(monkeypatch)
    controller = module.TradingController(storage=BotStorage(tmp_path / "bot.sqlite"))
    contract, settings = inputs()
    controller.strategy = settings
    controller.contract = contract
    controller.storage.save_strategy_settings(settings)
    return module, controller, contract, settings


@pytest.mark.parametrize("observed,next_open,reuse_seed", [
    (THURSDAY, FRIDAY, True),
    (datetime(2026, 8, 7, 19, 55, tzinfo=timezone.utc), datetime(2026, 8, 10, 13, 30, 1, tzinfo=timezone.utc), False),
])
def test_controller_next_rth_uses_only_unexpired_seed_until_fresh_bars_are_ready(tmp_path, monkeypatch, observed, next_open, reuse_seed):
    module, controller, contract, settings = controller_with_settings(tmp_path, monkeypatch)
    clock = {"wall": observed, "mono": 10000.0}
    monkeypatch.setattr(module, "utc_now_iso", lambda: clock["wall"].isoformat())
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["mono"])

    def tick(price):
        controller._latest_rth_status = rth(clock["wall"])
        controller._record_price_snapshot(MarketPriceSnapshot(
            price=price, source="marketPrice", requested_market_data_type=1,
            subscription_market_data_type=1, fields={"marketPrice": price, "bid": price - .01, "ask": price + .01},
            timestamp=clock["wall"].isoformat(), status="OK",
        ), contract)

    tick(100.0)
    assert not controller.price_snapshot["atr_ready"]
    for price in (101.0, 100.0, 102.0):
        clock["wall"] += timedelta(seconds=60)
        clock["mono"] += 60
        tick(price)
    assert controller.price_snapshot["atr_ready"]
    saved = controller.price_snapshot["atr_pct"]
    controller._atr_session_memory.flush()
    # Running continuously into the next RTH session resets raw RTH observations.
    clock.update(wall=next_open, mono=10000.0 + (next_open - observed).total_seconds())
    tick(110.0)
    assert len(controller._price_history) == 1
    assert controller.price_snapshot["atr_ready"] is reuse_seed
    assert controller.price_snapshot["atr"]["seeded"] is reuse_seed
    assert controller.price_snapshot["atr_pct"] == (saved if reuse_seed else None)
    assert controller.price_snapshot["atr"]["live_bars_available"] == 1
    # Warmup may use the seed, but no other entry guard/market tick is fabricated.
    cycle = CycleState.new(settings, 1, "DU1", 110.0, 0.0)
    blocker = controller._atr_warmup_guard_blocker_for_buy(cycle)
    assert (blocker is None) is reuse_seed
    for price in (113.0, 111.0, 114.0):
        clock["wall"] += timedelta(seconds=60)
        clock["mono"] += 60
        tick(price)
    assert not controller.price_snapshot["atr"]["seeded"]
    assert controller.price_snapshot["atr_ready"]
    assert controller.price_snapshot["atr_pct"] != saved
    assert controller._atr_warmup_guard_blocker_for_buy(cycle) is None
    controller._market_capture.shutdown()


def make_cycle(stage=Stage.WAIT_RISE_TRIGGER):
    _, settings = inputs()
    cycle = CycleState.new(settings, 1, "DU1", 100.0, 0.0)
    cycle.stage = stage
    cycle.con_id = 123
    cycle.buy_filled_qty = 100
    cycle.avg_buy_price = 100.0
    return cycle, settings


def test_ordinary_draft_does_not_override_cycle_without_explicit_edit(tmp_path, monkeypatch):
    _, controller, _, settings = controller_with_settings(tmp_path, monkeypatch)
    cycle, _ = make_cycle(Stage.CYCLE_COMPLETE)
    controller.strategy = replace(settings, max_spread_pct=12, no_new_buy_last_minutes=99)
    result = controller._settings_for_repeat_cycle(cycle)
    assert result.max_spread_pct == cycle.max_spread_pct
    assert result.no_new_buy_last_minutes == cycle.no_new_buy_last_minutes


@pytest.mark.parametrize("stage", [Stage.BUY_TRAIL_ACTIVE, Stage.SELL_TRAIL_ACTIVE])
def test_working_orders_keep_their_original_safety_and_cancel_policy(stage):
    cycle, settings = make_cycle(stage)
    before = cycle.to_dict()
    draft = replace(settings, max_spread_pct=0.8, max_bid_ask_age_seconds=12, no_new_buy_last_minutes=30, cancel_buy_before_close_minutes=25)
    result, changes = apply_waiting_order_guards(cycle, draft)
    assert not changes and result.to_dict() == before


def test_next_sell_receives_only_quote_guards_next_buy_receives_all_reviewed_guards():
    cycle, settings = make_cycle()
    draft = replace(settings, max_spread_pct=0.8, no_new_buy_last_minutes=30, cancel_buy_before_close_minutes=25)
    result, changes = apply_waiting_order_guards(cycle, draft)
    assert result.max_spread_pct == 0.8 and "max_spread_pct" in changes
    assert result.no_new_buy_last_minutes == cycle.no_new_buy_last_minutes
    assert result.cancel_buy_before_close_minutes == cycle.cancel_buy_before_close_minutes
    cycle.stage = Stage.WAIT_INITIAL_DROP
    result, changes = apply_waiting_order_guards(cycle, draft)
    assert result.cancel_buy_before_close_minutes == 25
    assert result.no_new_buy_last_minutes == 30
    assert cycle.max_spread_pct != 0.8


@pytest.mark.parametrize("change", [
    {"ticker": "CHIP"}, {"contract_con_id": 456}, {"currency": "EUR"}, {"exchange": "LSE"}, {"primary_exchange": "IBIS2"},
])
def test_next_order_edits_cannot_leak_between_contracts(change):
    cycle, settings = make_cycle()
    draft = replace(settings, **change)
    assert not settings_match_cycle(cycle, draft)
    assert apply_waiting_order_guards(cycle, draft) == (cycle, [])
    assert apply_repeat_order_guards(settings, cycle, draft) is settings


def test_explicit_edits_are_persisted_restored_and_applied_only_at_safe_boundary(tmp_path, monkeypatch):
    module, controller, _, settings = controller_with_settings(tmp_path, monkeypatch)
    cycle, _ = make_cycle(Stage.BUY_TRAIL_ACTIVE)
    cycle.buy_order_ref = "native-buy"
    cycle.buy_order_id = 77
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    draft = replace(settings, max_spread_pct=0.8, max_bid_ask_age_seconds=12, no_new_buy_last_minutes=30)
    controller._apply_active_strategy_edits(draft, reevaluate_market_state=False)
    assert controller.active_cycle.max_spread_pct == cycle.max_spread_pct
    assert controller.active_cycle.buy_order_id == 77
    request = controller.storage.get_json("next_order_risk_edits_v1")
    restored = module.TradingController(storage=controller.storage)
    pending = pending_risk_settings(restored._pending_order_risk_edits, cycle)
    assert pending.max_spread_pct == 0.8
    restored.active_cycle.stage = Stage.WAIT_RISE_TRIGGER
    restored.active_cycle.buy_filled_qty = 100
    restored.active_cycle.avg_buy_price = 100
    updated, actions = restored._advance_waiting_cycle_from_price(restored.active_cycle, 100, is_rth=True, rth_message="open")
    assert updated.max_spread_pct == 0.8 and updated.max_bid_ask_age_seconds == 12
    assert updated.no_new_buy_last_minutes == cycle.no_new_buy_last_minutes
    assert not actions
    repeated = restored._settings_for_repeat_cycle(updated)
    assert repeated.no_new_buy_last_minutes == 30
    assert repeated.max_spread_pct == 0.8
    assert repeated.investment_amount == updated.investment_amount
    assert request["cycle_id"] == cycle.id
    assert pending_risk_settings(request, replace(updated, id="another-cycle")) is None
    controller._market_capture.shutdown()
    restored._market_capture.shutdown()


def test_edit_revert_discards_queue_and_quote_edits_clear_confirmation(tmp_path, monkeypatch):
    _, controller, _, settings = controller_with_settings(tmp_path, monkeypatch)
    cycle, _ = make_cycle(Stage.BUY_TRAIL_ACTIVE)
    controller.active_cycle = cycle
    controller._apply_active_strategy_edits(replace(settings, max_spread_pct=.9), reevaluate_market_state=False)
    assert controller._pending_order_risk_edits
    controller._apply_active_strategy_edits(settings, reevaluate_market_state=False)
    assert controller._pending_order_risk_edits == {}
    controller.active_cycle.stage = Stage.WAIT_RISE_TRIGGER
    controller._stage3_sell_confirmation = {"cycle_id": cycle.id, "quote_sequence": 1}
    controller._apply_active_strategy_edits(replace(settings, max_spread_pct=.9), reevaluate_market_state=False)
    assert controller.active_cycle.max_spread_pct == .9
    assert not controller._stage3_sell_confirmation
    controller._market_capture.shutdown()


@pytest.mark.parametrize("malform", [
    None, [], {}, {"version": 1, "cycle_id": "other"},
])
def test_invalid_pending_record_is_ignored(malform):
    cycle, _ = make_cycle()
    assert pending_risk_settings(malform, cycle) is None


def test_pending_record_validation_and_complete_field_inventory():
    cycle, settings = make_cycle()
    request = risk_edit_request(cycle, settings)
    assert pending_risk_settings(request, cycle) is not None
    assert len(set(NEXT_ORDER_RISK_FIELDS)) == len(NEXT_ORDER_RISK_FIELDS)
    assert all(key in asdict(settings) for key in NEXT_ORDER_RISK_FIELDS)
    for key, value in (("max_spread_pct", float("nan")), ("what_if_check_enabled", "False"), ("no_new_buy_first_minutes", True)):
        candidate = deepcopy(request)
        candidate["values"][key] = value
        assert pending_risk_settings(candidate, cycle) is None
    request["values"].pop("what_if_check_enabled")
    assert pending_risk_settings(request, cycle) is None


def test_new_rth_window_resets_atr_without_erasing_recent_volatility_history(tmp_path, monkeypatch):
    module, controller, contract, _ = controller_with_settings(tmp_path, monkeypatch)
    clock = {"wall": FRIDAY, "mono": 10000.0}
    monkeypatch.setattr(module, "utc_now_iso", lambda: clock["wall"].isoformat())
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["mono"])

    def tick(price, opened):
        controller._latest_rth_status = {**rth(clock["wall"]), "session_open": opened.isoformat()}
        controller._record_price_snapshot(MarketPriceSnapshot(
            price=price, source="marketPrice", requested_market_data_type=1,
            subscription_market_data_type=1,
            fields={"marketPrice": price, "bid": price - .01, "ask": price + .01},
            timestamp=clock["wall"].isoformat(), status="OK",
        ), contract)

    first_open = FRIDAY.replace(second=0)
    for price in (100., 101., 100., 102.):
        tick(price, first_open)
        clock["wall"] += timedelta(seconds=60)
        clock["mono"] += 60
    previous = list(controller._price_history)
    second_open = clock["wall"].replace(second=0)
    tick(110., second_open)
    assert list(controller._price_history)[:-1] == previous
    assert len(controller._atr_bars) == 1
    assert controller.price_snapshot["atr"]["seeded"]
    # A later interval edit must still rebuild from only this RTH window.
    controller._rebuild_incremental_atr_bars(30)
    assert len(controller._atr_bars) == 1
    assert controller._atr_bars[0]["close"] == 110.
    controller._market_capture.shutdown()


def test_restored_seed_does_not_make_cached_quote_a_new_event(tmp_path, monkeypatch):
    from tests.test_v370_stage3_market_data_guard import _tracked_quote_snapshot

    module, controller, contract, settings = controller_with_settings(tmp_path, monkeypatch)
    profile = f"{controller.connection.trading_mode}|{controller.connection.market_data_type}"
    identity = atr_seed_identity(contract, settings, profile)
    cache = AtrSessionMemory(controller.storage)
    cache.prepare(identity, rth(THURSDAY), THURSDAY.isoformat())
    cache.note_observation(live=True)
    cache.apply(ready_result())
    monkeypatch.setattr(module, "utc_now_iso", lambda: FRIDAY.isoformat())
    monkeypatch.setattr(module.time, "monotonic", lambda: 10000.0)
    controller._latest_rth_status = rth(FRIDAY)
    quote = _tracked_quote_snapshot(1, bid=100., ask=100.1, last=120., selected_price=100.05)
    controller._record_price_snapshot(quote, contract)
    assert controller.price_snapshot["atr_ready"]
    assert controller.price_snapshot["atr"]["seeded"]
    received = controller._api_data_seen_count
    observations = list(controller._price_history)
    controller._record_price_snapshot(quote, contract)
    assert controller._api_data_seen_count == received
    assert list(controller._price_history) == observations
    assert not controller.price_snapshot["strategy_price_usable"]
    # A new event with a cached Last fallback is still rejected, despite a
    # persisted ready volatility estimate and a price apparently above target.
    fallback = _tracked_quote_snapshot(
        2, bid=None, ask=100.1, last=120., selected_price=120.,
        selected_basis="last", basis_changed=False,
    )
    controller._record_price_snapshot(fallback, contract)
    assert controller.price_snapshot["atr"]["seeded"]
    assert not controller.price_snapshot["strategy_price_usable"]
    assert list(controller._price_history) == observations
    controller._market_capture.shutdown()


def test_risk_draft_cannot_change_cancellation_of_an_already_working_buy(tmp_path, monkeypatch):
    from tests.test_v380_buy_partial_fill_grace import _buy_cycle, _controller, _partial

    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    original = controller.strategy
    # All these restrictions would reject the current quote if applied now.
    draft = replace(
        original, hard_risk_limits_enabled=True, min_trade_price=1000.,
        max_spread_pct=.0001, stale_data_guard_enabled=True,
        max_selected_price_age_seconds=.1, volatility_filter_enabled=True,
    )
    controller.strategy = draft
    controller._apply_active_strategy_edits(draft, reevaluate_market_state=False)
    controller._handle_buy_order_poll(controller.active_cycle, state)
    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert controller.active_cycle.buy_filled_qty == 4
    assert not controller.active_cycle.buy_remainder_cancel_requested
    assert not controller.active_cycle.hard_risk_limits_enabled
    assert not broker.cancelled_orders
    queued = pending_risk_settings(controller._pending_order_risk_edits, cycle)
    assert queued.min_trade_price == 1000.
    assert controller._settings_for_repeat_cycle(controller.active_cycle).min_trade_price == 1000.
    controller._market_capture.shutdown()


def test_profile_change_cannot_rebuild_atr_from_prior_profile_prices(tmp_path, monkeypatch):
    module, controller, contract, _ = controller_with_settings(tmp_path, monkeypatch)
    clock = {"wall": FRIDAY, "mono": 10000.0}
    monkeypatch.setattr(module, "utc_now_iso", lambda: clock["wall"].isoformat())
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["mono"])

    def tick(price):
        controller._latest_rth_status = rth(clock["wall"])
        controller._record_price_snapshot(MarketPriceSnapshot(
            price=price, source="marketPrice", requested_market_data_type=1,
            subscription_market_data_type=1,
            fields={"marketPrice": price, "bid": price - .01, "ask": price + .01},
            timestamp=clock["wall"].isoformat(), status="OK",
        ), contract)
        clock["wall"] += timedelta(seconds=60)
        clock["mono"] += 60

    controller.connection.trading_mode = "live"
    for price in (100., 101., 100., 102.):
        tick(price)
    assert controller.price_snapshot["atr_ready"]
    live_key = controller._atr_session_memory.key
    controller.connection.trading_mode = "paper"
    tick(110.)
    assert controller._atr_session_memory.key != live_key
    assert len(controller._price_history) == 1
    assert len(controller._atr_bars) == 1
    assert not controller.price_snapshot["atr_ready"]
    assert not controller.price_snapshot["atr"]["seeded"]
    controller._market_capture.shutdown()
