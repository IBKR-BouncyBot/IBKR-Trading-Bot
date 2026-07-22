from pathlib import Path

from app.models import StrategySettings, suggested_broker_timing_defaults, suggested_hard_risk_defaults


def test_broker_and_timing_safety_defaults_match_requested_startup_state():
    settings = StrategySettings(ticker="AAPL")
    assert settings.what_if_check_enabled is True
    assert settings.stale_data_guard_enabled is True
    assert settings.atr_adaptive_enabled is True
    assert settings.atr_block_new_buy_until_ready is True
    assert settings.volatility_filter_enabled is False
    assert settings.session_timing_guard_enabled is True


def test_hard_risk_defaults_are_static_and_operator_owned():
    defaults = suggested_hard_risk_defaults(
        10000,
        market_price=101.0,
        bid=100.95,
        ask=101.05,
        previous_close=100.0,
    )
    assert defaults["max_daily_loss_ticker"] == 0.0
    assert defaults["max_daily_loss_total"] == 0.0
    assert defaults["max_cycles_per_ticker_day"] == 0
    # The startup spread value is deterministic and must not follow quotes.
    assert defaults["max_spread_pct"] == 1.0
    # Previous-close gap guard is intentionally disabled by default.
    assert defaults["max_gap_from_prev_close_pct"] == 0.0


def test_hard_risk_defaults_ignore_low_price_wide_spread_data():
    defaults = suggested_hard_risk_defaults(
        10000,
        market_price=4.0,
        bid=3.90,
        ask=4.10,
        previous_close=4.0,
    )
    assert defaults["max_daily_loss_ticker"] == 0.0
    assert defaults["max_daily_loss_total"] == 0.0
    assert defaults["max_cycles_per_ticker_day"] == 0
    assert defaults["max_spread_pct"] == 1.0


def test_broker_timing_defaults_scale_for_larger_investments_and_market_move():
    defaults = suggested_broker_timing_defaults(
        50000,
        market_price=110.0,
        previous_close=100.0,
    )
    assert defaults["max_selected_price_age_seconds"] == 3.0
    assert defaults["max_bid_ask_age_seconds"] == 3.0
    assert defaults["max_rth_status_age_seconds"] == 60.0
    assert defaults["no_new_buy_first_minutes"] == 10
    assert defaults["no_new_buy_last_minutes"] == 20
    assert defaults["cancel_buy_before_close_minutes"] == 10
    assert defaults["max_recent_price_move_pct"] == 10.0


def test_windows_build_script_verifies_exe_and_uses_lean_pyinstaller_call():
    script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert "pyinstaller.exe" in script
    assert "PyInstaller" in script
    assert "IBKRTradingBot.exe" in script
    assert "if (!(Test-Path $exePath))" in script
    assert "PyInstaller completed but $exePath was not created" in script
    assert "--collect-all" not in script
    assert "--collect-submodules" not in script


def test_windows_build_script_uses_optional_runtests_and_fast_default():
    script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert "[switch]$RunTests" in script
    assert "[switch]$SkipTests" not in script
    assert "Compile app, tests, and main.py" not in script
    assert "-W error::ResourceWarning -m pytest -q" not in script
    assert "Run CSV simulation fixtures" in script
    assert "Skipping full tests for faster, more reliable packaging." in script


def test_windows_build_script_can_recreate_clean_virtual_environment():
    script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert "[switch]$CleanVenv" in script
    assert "Removing existing virtual environment" in script
