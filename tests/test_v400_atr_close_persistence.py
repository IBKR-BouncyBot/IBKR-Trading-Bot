"""Same-version v4.0.0 correction: save ATR near RTH close, not all day."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta, timezone

import pytest

from app.atr_memory import ATR_SEED_SAVE_WINDOW_SECONDS, AtrSessionMemory, atr_seed_identity
from app.ib_adapter import MarketPriceSnapshot
from app.storage import BotStorage
from tests.test_v400_atr_memory_and_order_edits import (
    FRIDAY,
    MONDAY,
    Store,
    controller_with_settings,
    inputs,
    pending_result,
    ready_result,
    rth,
    seeded_memory,
)

MIDDAY = FRIDAY.replace(hour=16, minute=0, second=0)
CLOSE = FRIDAY.replace(hour=20, minute=0, second=0)


def observe(memory, identity, now, atr=2.0, status=None):
    memory.prepare(identity, rth(now) if status is None else status, now.isoformat())
    memory.note_observation(live=True)
    return memory.apply(ready_result(atr))


def midday_memory():
    contract, settings = inputs()
    identity = atr_seed_identity(contract, settings, "live|0")
    store = Store()
    memory = AtrSessionMemory(store)
    observe(memory, identity, MIDDAY)
    return memory, identity, store


def test_ready_atr_updates_in_memory_without_intraday_database_writes():
    memory, identity, store = midday_memory()
    for second in range(1, 601):
        result = observe(memory, identity, MIDDAY + timedelta(seconds=second), 2 + second / 1000)
        assert result["ready"] and not result["seeded"]
    assert store.writes == 0 and memory.dirty
    assert memory.seed["snapshot"]["atr"] == 2.6
    assert memory.seed["observed_at"] == (MIDDAY + timedelta(minutes=10)).isoformat()


def test_five_minute_boundary_is_inclusive_and_no_earlier_write_occurs():
    memory, identity, store = midday_memory()
    assert ATR_SEED_SAVE_WINDOW_SECONDS == 300
    before = FRIDAY - timedelta(microseconds=1)
    observe(memory, identity, before, 3.0)
    assert store.writes == 0
    observe(memory, identity, FRIDAY, 4.0)
    assert store.writes == 1 and not memory.dirty
    assert store.data[memory.key]["snapshot"]["atr"] == 4.0


def test_final_five_minutes_save_once_per_minute_then_flush_latest_at_close():
    memory, identity, store = midday_memory()
    for second in range(300):
        observe(memory, identity, FRIDAY + timedelta(seconds=second), 2 + second / 1000)
    assert store.writes == 5
    assert memory.dirty
    latest = deepcopy(memory.seed)
    memory.prepare(identity, {"is_open": False}, CLOSE.isoformat())
    assert store.writes == 6
    assert store.data[memory.key] == latest
    assert latest["observed_at"] == (CLOSE - timedelta(seconds=1)).isoformat()
    for minute in range(1, 121):
        memory.prepare(identity, {"is_open": False}, (CLOSE + timedelta(minutes=minute)).isoformat())
    assert store.writes == 6
    assert not memory.dirty


@pytest.mark.parametrize("change", [
    {"is_open": False},
    {"is_open": None},
    {"checked_at": MIDDAY.isoformat()},
    {"checked_at": (MIDDAY + timedelta(hours=1)).isoformat()},
    {"session_close": "bad"},
])
def test_midday_status_loss_is_not_misclassified_as_session_close(change):
    memory, identity, store = midday_memory()
    now = MIDDAY + timedelta(minutes=2)
    status = rth(now)
    status.update(change)
    memory.prepare(identity, status, now.isoformat())
    memory.apply(ready_result(4.0))
    assert store.writes == 0
    assert memory.dirty
    assert memory.seed["snapshot"]["atr"] == 2.0
    observe(memory, identity, now + timedelta(seconds=1), 5.0)
    assert store.writes == 0
    assert memory.seed["snapshot"]["atr"] == 5.0


def test_unverified_status_in_closing_window_does_not_force_a_write():
    memory, identity, store = midday_memory()
    memory.prepare(identity, {"is_open": False}, FRIDAY.isoformat())
    assert store.writes == 0 and memory.dirty
    # A verified closing session resumes normal eligibility without a fake tick.
    memory.prepare(identity, rth(FRIDAY), FRIDAY.isoformat())
    memory.apply(ready_result())
    assert store.writes == 1
    assert store.data[memory.key]["observed_at"] == MIDDAY.isoformat()


@pytest.mark.parametrize("change", [
    {"period": 9}, {"bar_seconds": 300}, {"con_id": 456}, {"profile": "paper|0"}, None,
])
def test_identity_edits_cannot_flush_intraday_estimates(change):
    memory, identity, store = midday_memory()
    updated = dict(identity, **change) if change is not None else None
    now = MIDDAY + timedelta(minutes=1)
    memory.prepare(updated, rth(now), now.isoformat())
    assert store.writes == 0


def test_genuine_close_flushes_even_when_status_was_lost_before_the_boundary():
    memory, identity, store = seeded_memory()
    observe(memory, identity, CLOSE - timedelta(seconds=20), 3.0)
    observe(memory, identity, CLOSE - timedelta(seconds=1), 4.0)
    assert store.writes == 2
    memory.prepare(identity, {"is_open": False}, (CLOSE - timedelta(microseconds=1)).isoformat())
    assert memory.session is None and store.writes == 2
    memory.prepare(identity, {"is_open": False}, CLOSE.isoformat())
    assert store.writes == 3
    assert store.data[memory.key]["snapshot"]["atr"] == 4.0


def test_direct_next_session_transition_saves_previous_final_observation():
    memory, identity, store = seeded_memory()
    observe(memory, identity, CLOSE - timedelta(seconds=2), 3.0)
    observe(memory, identity, CLOSE - timedelta(seconds=1), 4.0)
    assert memory.dirty
    assert memory.prepare(identity, rth(MONDAY), MONDAY.isoformat())
    result = memory.apply(pending_result(1))
    assert result["ready"] and result["seeded"]
    assert result["atr"] == 4.0
    assert store.data[memory.key]["observed_at"] == (CLOSE - timedelta(seconds=1)).isoformat()


@pytest.mark.parametrize("offset_hours", [0, -4, 5.5])
def test_window_follows_broker_early_close_and_timezone_not_a_fixed_clock(offset_hours):
    zone = timezone(timedelta(hours=offset_hours))
    now = MIDDAY.replace(minute=54, second=59).astimezone(zone)
    close = MIDDAY.replace(hour=17).astimezone(zone)
    status = rth(now.astimezone(timezone.utc))
    status["session_close"] = close.isoformat()
    memory, identity, store = midday_memory()
    observe(memory, identity, now, 3.0, status)
    assert store.writes == 0
    now += timedelta(seconds=1)
    status["checked_at"] = now.isoformat()
    observe(memory, identity, now, 4.0, status)
    assert store.writes == 1
    assert store.data[memory.key]["snapshot"]["atr"] == 4.0


def test_explicit_app_close_saves_latest_midday_estimate_and_restart_restores_it(tmp_path):
    storage = BotStorage(tmp_path / "bot.sqlite")
    contract, settings = inputs()
    identity = atr_seed_identity(contract, settings, "live|0")
    memory = AtrSessionMemory(storage)
    observe(memory, identity, MIDDAY, 2.0)
    latest_time = MIDDAY + timedelta(minutes=1)
    observe(memory, identity, latest_time, 4.0)
    assert storage.get_json(memory.key, None) is None
    memory.flush()  # Existing orderly-worker-shutdown call; no RTH-window limit.
    assert storage.get_json(memory.key)["snapshot"]["atr"] == 4.0
    for restart in (latest_time + timedelta(seconds=1), MONDAY):
        restored = AtrSessionMemory(BotStorage(storage.db_path))
        restored.prepare(identity, rth(restart), restart.isoformat())
        result = restored.apply(pending_result())
        assert result["ready"] and result["seeded"] and result["atr"] == 4.0
        assert result["seed_observed_at"] == latest_time.isoformat()


def test_next_day_intraday_estimates_do_not_overwrite_previous_persisted_session():
    memory, identity, store = seeded_memory()
    prior = deepcopy(store.data[memory.key])
    for minute in range(61):
        observe(memory, identity, MONDAY + timedelta(minutes=minute), 3.0 + minute / 100)
    assert memory.seed["snapshot"]["atr"] == 3.6
    assert store.data[memory.key] == prior and store.writes == 1
    # A crash before today's closing window reuses the previous saved estimate.
    restarted = AtrSessionMemory(store)
    now = MONDAY + timedelta(hours=2)
    restarted.prepare(identity, rth(now), now.isoformat())
    assert restarted.apply(pending_result())["atr"] == 2.0


def test_shutdown_during_warmup_does_not_relabel_or_overwrite_saved_seed():
    _, identity, store = seeded_memory()
    prior = deepcopy(store.data)
    memory = AtrSessionMemory(store)
    memory.prepare(identity, rth(MONDAY), MONDAY.isoformat())
    assert memory.apply(pending_result())["seeded"]
    memory.flush()
    assert store.data == prior and store.writes == 1


def test_failed_final_write_is_retried_at_most_once_per_minute():
    memory, identity, store = seeded_memory()
    observe(memory, identity, CLOSE - timedelta(seconds=10), 3.0)
    observe(memory, identity, CLOSE - timedelta(seconds=1), 4.0)
    store.write_error = True
    memory.prepare(identity, {"is_open": False}, CLOSE.isoformat())
    assert store.writes == 3 and memory.dirty
    for tenth in range(1, 600):
        memory.prepare(identity, {"is_open": False}, (CLOSE + timedelta(seconds=tenth / 10)).isoformat())
    assert store.writes == 3
    memory.prepare(identity, {"is_open": False}, (CLOSE + timedelta(seconds=60)).isoformat())
    assert store.writes == 4 and memory.dirty
    store.write_error = False
    memory.prepare(identity, {"is_open": False}, (CLOSE + timedelta(seconds=120)).isoformat())
    assert store.writes == 5 and not memory.dirty and memory.error == ""
    assert store.data[memory.key]["snapshot"]["atr"] == 4.0


def test_transient_status_blips_cannot_bypass_closing_window_retry_interval():
    memory, identity, store = seeded_memory()
    observe(memory, identity, FRIDAY + timedelta(seconds=1), 3.0)
    store.write_error = True
    for second in range(2, 60):
        now = FRIDAY + timedelta(seconds=second)
        status = rth(now) if second % 2 else {"is_open": False}
        memory.prepare(identity, status, now.isoformat())
        memory.apply(ready_result(4.0))
    assert store.writes == 1 and memory.dirty
    observe(memory, identity, FRIDAY + timedelta(seconds=60), 5.0)
    assert store.writes == 2 and memory.dirty


def test_invalid_pending_estimate_cannot_overwrite_good_checkpoint_even_at_shutdown():
    memory, _, store = seeded_memory()
    prior = deepcopy(store.data)
    memory.seed["snapshot"]["atr"] = float("nan")
    memory.dirty = True
    memory.flush()
    assert store.data == prior and store.writes == 1


def test_controller_saves_final_rth_value_and_restores_without_fake_quotes(tmp_path, monkeypatch):
    module, controller, contract, settings = controller_with_settings(tmp_path, monkeypatch)
    clock = {"wall": MIDDAY, "mono": 10000.0}
    monkeypatch.setattr(module, "utc_now_iso", lambda: clock["wall"].isoformat())
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["mono"])

    def tick(price, now, *, is_open=True):
        clock["mono"] += (now - clock["wall"]).total_seconds()
        clock["wall"] = now
        controller._latest_rth_status = {**rth(now), "is_open": is_open}
        controller._record_price_snapshot(MarketPriceSnapshot(
            price=price, source="marketPrice", requested_market_data_type=1,
            subscription_market_data_type=1,
            fields={"marketPrice": price, "bid": price - .01, "ask": price + .01},
            timestamp=now.isoformat(), status="OK",
        ), contract)

    try:
        for minute, price in enumerate((100.0, 101.0, 100.0, 102.0)):
            tick(price, MIDDAY + timedelta(minutes=minute))
        memory = controller._atr_session_memory
        assert controller.price_snapshot["atr_ready"]
        assert controller.storage.get_json(memory.key, None) is None
        for minute, price in enumerate((100.0, 101.0, 100.0, 102.0, 104.0)):
            tick(price, FRIDAY + timedelta(minutes=minute))
        tick(105.0, CLOSE - timedelta(seconds=1))
        final = deepcopy(memory.seed)
        assert final["snapshot"]["atr"] == controller.price_snapshot["atr_value"]
        tick(106.0, CLOSE, is_open=False)
        assert controller.storage.get_json(memory.key) == final
        assert not controller.price_snapshot["atr_ready"]
        restored = AtrSessionMemory(BotStorage(controller.storage.db_path))
        identity = atr_seed_identity(contract, settings, f"{controller.connection.trading_mode}|{controller.connection.market_data_type}")
        restored.prepare(identity, rth(MONDAY), MONDAY.isoformat())
        result = restored.apply(pending_result())
        assert result["seeded"] and result["atr"] == final["snapshot"]["atr"]
        assert result["live_bars_available"] == 0
    finally:
        controller._market_capture.shutdown()
