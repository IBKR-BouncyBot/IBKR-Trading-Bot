from pathlib import Path

from app.models import CycleState, Stage, StrategySettings, suggested_hard_risk_defaults


def test_v131_hard_risk_caps_default_disabled():
    settings = StrategySettings(ticker="AAPL")
    assert settings.max_daily_loss_ticker == 0.0
    assert settings.max_daily_loss_total == 0.0
    assert settings.max_cycles_per_ticker_day == 0
    assert settings.max_consecutive_losses == 0

    cycle = CycleState(id="c1", cycle_number=1, ticker="AAPL", stage=Stage.WAIT_INITIAL_DROP)
    assert cycle.max_daily_loss_ticker == 0.0
    assert cycle.max_daily_loss_total == 0.0
    assert cycle.max_cycles_per_ticker_day == 0
    assert cycle.max_consecutive_losses == 0


def test_v131_suggested_hard_risk_caps_stay_disabled_with_market_data():
    calm = suggested_hard_risk_defaults(
        10_000,
        market_price=100.0,
        bid=99.99,
        ask=100.01,
        previous_close=99.75,
    )
    stressed = suggested_hard_risk_defaults(
        10_000,
        market_price=4.50,
        bid=4.40,
        ask=4.60,
        previous_close=5.50,
        recent_move_pct=8.0,
    )
    for defaults in (calm, stressed):
        assert defaults["max_daily_loss_ticker"] == 0.0
        assert defaults["max_daily_loss_total"] == 0.0
        assert defaults["max_cycles_per_ticker_day"] == 0
        assert defaults["max_consecutive_losses"] == 0
    assert stressed["max_spread_pct"] == calm["max_spread_pct"] == 1.0
    assert stressed["max_gap_from_prev_close_pct"] == calm["max_gap_from_prev_close_pct"] == 0.0


def test_v131_flowchart_ui_source_is_compact_and_has_no_duplicate_selector_label():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    assert source.count('selector_row.addWidget(QLabel("Flowchart data"))') == 1
    assert "CANVAS_HEIGHT = 1580" in source
    assert "CARD_HEIGHT = 272.0" in source
    assert "def _canvas_height" in source
    assert "self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in source
    assert "box_gap = 8" in source
