from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
MODELS = Path("app/models.py").read_text(encoding="utf-8")
CONTROLLER = Path("app/controller.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")


def test_v224_default_atr_and_warmup_guards_are_visible_and_on():
    assert "atr_adaptive_enabled: bool = True" in MODELS
    assert "atr_block_new_buy_until_ready: bool = True" in MODELS
    assert "session_timing_guard_enabled: bool = True" in MODELS
    assert "self.atr_adaptive_check.setChecked(True)" in GUI
    assert "self.atr_block_until_ready_check.setChecked(True)" in GUI
    assert "self.session_timing_guard_check.setChecked(True)" in GUI
    assert "Block new BUY until ATR has enough RTH data" in GUI
    assert "ATR warmup guard blocked BUY" in CONTROLLER


def test_v224_atr_protective_sell_option_is_present_but_manual_by_default():
    assert "atr_adapt_protective_sell_enabled: bool = False" in MODELS
    assert "atr_protective_sell_multiplier: float = 3.00" in MODELS
    assert "Adapt Protective SELL trailing-stop % with ATR" in GUI
    assert "self.atr_protective_sell_adaptive_check.setChecked(False)" in GUI
    assert "Protective SELL multiplier" in GUI
    assert "protective_sell_trailing_stop_pct" in MODELS


def test_v224_price_monitor_time_and_paper_warning_cleanup():
    assert '"Current time"' in GUI
    assert '"Current UTC"' in GUI
    assert '"System time"' in GUI
    assert "Use paper mode first" not in GUI
    assert "Use paper mode first" not in README


def test_v224_stopped_trading_top_status_is_yellow_waiting():
    assert 'trading_text, trading_state = "Stopped", "waiting"' in GUI
    assert 'trading_state = ("Stopped", "inactive")' not in GUI
