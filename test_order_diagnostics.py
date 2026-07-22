from app.models import Stage
from app.order_diagnostics import native_trailing_order_diagnostics


def test_buy_diag_distinguishes_market_price_from_last_trigger():
    diag = native_trailing_order_diagnostics(
        stage=Stage.BUY_TRAIL_ACTIVE,
        fields={"marketPrice": 101.0, "last": 99.5},
        selected_price=101.0,
        buy_initial_stop=100.0,
        trigger_method=2,
    )
    assert diag["active"] is True
    assert diag["side"] == "BUY"
    assert diag["selected_crossed_displayed_initial_stop"] is True
    assert diag["raw_last_crossed_displayed_initial_stop"] is False
    assert "Selected app price crossed" in diag["message"]


def test_buy_diag_reports_raw_last_crossed():
    diag = native_trailing_order_diagnostics(
        stage=Stage.BUY_TRAIL_ACTIVE,
        fields={"last": 100.5},
        selected_price=100.5,
        buy_initial_stop=100.0,
        trigger_method=2,
    )
    assert diag["raw_last_crossed_displayed_initial_stop"] is True
    assert "Raw last has crossed" in diag["message"]


def test_sell_diag_uses_downward_cross():
    diag = native_trailing_order_diagnostics(
        stage=Stage.SELL_TRAIL_ACTIVE,
        fields={"delayedLast": 98.5},
        selected_price=98.5,
        sell_initial_stop=99.0,
        trigger_method=2,
    )
    assert diag["side"] == "SELL"
    assert diag["raw_last_crossed_displayed_initial_stop"] is True
    assert diag["raw_last_source"] == "delayedLast"
