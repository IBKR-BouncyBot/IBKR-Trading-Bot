from pathlib import Path

from app.models import Stage, StrategySettings
from tests.simulated_strategy_runner import load_prices_csv, run_one_cycle

DATA_DIR = Path(__file__).resolve().parent / "simulated_data"


def default_settings(**overrides) -> StrategySettings:
    values = dict(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        rise_trigger_pct=10.0,
        sell_trailing_stop_pct=1.0,
        protective_sell_enabled=False,
        slippage_buffer_enabled=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        reinvest_profits=False,
    )
    values.update(overrides)
    return StrategySettings(**values)


def test_simulated_profitable_cycle_completes_from_csv_data():
    result = run_one_cycle(default_settings(), load_prices_csv(DATA_DIR / "full_profitable_cycle.csv"))

    assert result.cycle.stage == Stage.CYCLE_COMPLETE
    assert result.cycle.buy_filled_qty > 0
    assert result.cycle.sell_filled_qty == result.cycle.buy_filled_qty
    assert result.cycle.net_pnl > 0
    assert result.event_kinds() == ["buy_trail_submitted", "buy_filled", "sell_trail_submitted", "sell_filled"]


def test_buy_trail_follows_lower_prices_until_configured_rebound():
    result = run_one_cycle(
        default_settings(rise_trigger_pct=3.0),
        load_prices_csv(DATA_DIR / "buy_trail_keeps_falling.csv"),
    )

    assert "buy_trail_submitted" in result.event_kinds()
    assert "buy_filled" in result.event_kinds()
    buy_fill = next(event for event in result.events if event.kind == "buy_filled")
    # The low after submission is 93.00; a 2% rebound triggers around 94.86.
    assert round(buy_fill.payload["stop_price"], 2) == 94.86
    assert round(buy_fill.price, 2) == 94.86


def test_no_initial_drop_creates_no_order():
    result = run_one_cycle(default_settings(), load_prices_csv(DATA_DIR / "no_initial_drop.csv"))

    assert result.cycle.stage == Stage.WAIT_INITIAL_DROP
    assert result.event_kinds() == []
    assert result.cycle.anchor_price == 104.0


def test_rth_closed_blocks_order_submission_even_when_drop_occurs():
    result = run_one_cycle(default_settings(), [100.0, 95.0, 94.0, 93.0], rth_open=False)

    assert result.cycle.stage == Stage.WAIT_INITIAL_DROP
    assert result.event_kinds() == []
    assert result.cycle.error_message is not None
    assert result.cycle.error_message.startswith("RTH guard:")


def test_partial_buy_fill_simulation_uses_only_filled_quantity_for_sell():
    result = run_one_cycle(
        default_settings(investment_amount=2000, rise_trigger_pct=5.0),
        [100.0, 95.0, 93.0, 94.86, 102.0, 104.0, 103.0, 102.90],
        partial_buy_ratio=0.4,
    )

    assert "cancel_remainder" in result.event_kinds()
    assert result.cycle.stage == Stage.CYCLE_COMPLETE
    assert result.cycle.buy_filled_qty < result.cycle.quantity
    assert result.cycle.sell_filled_qty == result.cycle.buy_filled_qty
