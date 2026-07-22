from app.flowchart_model import build_strategy_flowchart_cards
from app.models import CycleState, Stage, StrategySettings


def test_flowchart_has_five_stage_cards_and_marks_active_stage():
    settings = StrategySettings(ticker="AAPL")
    cycle = CycleState.new(settings, cycle_number=1, account="DU123", last_price=100.0, reinvested_profit=0.0)
    cards = build_strategy_flowchart_cards(settings, cycle.to_dict(), {"price": 99.0})
    assert len(cards) == 5
    assert [card.stage for card in cards] == [
        Stage.WAIT_INITIAL_DROP,
        Stage.BUY_TRAIL_ACTIVE,
        Stage.WAIT_RISE_TRIGGER,
        Stage.SELL_TRAIL_ACTIVE,
        Stage.CYCLE_COMPLETE,
    ]
    assert cards[0].active is True
    assert sum(1 for card in cards if card.active) == 1


def test_flowchart_stage_two_uses_slippage_buffer_for_sizing_text():
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        slippage_buffer_enabled=True,
        slippage_buffer_pct=1.0,
    )
    cards = build_strategy_flowchart_cards(settings, None, {"price": 100.0})
    stage_two = cards[1]
    detail_text = "\n".join(stage_two.details)
    assert "Slippage buffer ON" in detail_text
    assert "1.00%" in detail_text
    assert "Sizing:" in detail_text


def test_flowchart_stage_three_mentions_optional_protective_order():
    settings = StrategySettings(
        ticker="AAPL",
        protective_sell_enabled=True,
        protective_sell_trailing_stop_pct=4.0,
    )
    cycle = CycleState.new(settings, cycle_number=1, account="DU123", last_price=100.0, reinvested_profit=0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.avg_buy_price = 98.0
    cycle.buy_filled_qty = 10
    cards = build_strategy_flowchart_cards(settings, cycle.to_dict(), {"price": 101.0})
    stage_three = cards[2]
    assert stage_three.active is True
    assert "protective SELL TRAIL" in stage_three.order_summary
    assert "Protective trail ON" in "\n".join(stage_three.details)


def test_flowchart_shows_hard_risk_rails_when_enabled():
    settings = StrategySettings(
        ticker="AAPL",
        hard_risk_limits_enabled=True,
        max_daily_loss_ticker=50.0,
        max_spread_pct=0.25,
        min_trade_price=5.0,
    )
    cards = build_strategy_flowchart_cards(settings, None, {"price": 100.0})
    first_card_text = "\n".join(cards[0].details)
    assert "Hard risk ON" in first_card_text
    assert "spread <= 0.25%" in first_card_text
