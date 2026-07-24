from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
CONTROLLER = Path("app/controller.py").read_text(encoding="utf-8")
MODELS = Path("app/models.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def _recovery_block() -> str:
    return GUI[GUI.index("def _build_recovery") : GUI.index("def _recovery_export_bundle_clicked")]


def test_v3016_version_metadata_and_release_note_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.2" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.1.2"' in PYPROJECT
    assert Path("docs/legacy/V3_0_16_RECONCILIATION_REFRESH_WORKFLOW.md").exists()


def test_reconciliation_layout_is_refresh_compare_resolve_and_has_no_duplicate_cancel():
    block = _recovery_block()
    refresh_index = block.index('QLabel("1. Refresh current broker facts")')
    compare_index = block.index('QLabel("2. Compare SQLite with IBKR/TWS")')
    resolve_index = block.index('QLabel("3. Resolve the situation")')
    assert refresh_index < compare_index < resolve_index
    assert 'QPushButton("Refresh from IBKR/TWS")' in block
    assert 'QPushButton("Reconcile and resume")' in block
    assert 'QPushButton("Cancel visible app-owned orders")' in block
    assert "recovery_cancel_orders_btn" not in block
    assert 'QPushButton("Sell app-bought unsold position")' in block
    assert 'QPushButton("Leave orders working")' in block


def test_refresh_freshness_has_bounded_age_signature_and_last_successful_context():
    assert "RECOVERY_REFRESH_MAX_AGE_SECONDS = 60.0" in GUI
    assert "def _recovery_refresh_status(" in GUI
    assert 'broker.get("last_successful_checked_at")' in GUI
    assert 'broker.get("local_cycle_signature")' in GUI
    assert 'broker.get("order_state_updated_at")' in GUI
    assert 'broker.get("invalidated_at")' in GUI
    assert 'probe["invalidation_reason"]' in CONTROLLER
    assert "recovery_cycle_signature(cycle)" in GUI
    assert "def recovery_cycle_signature(" in MODELS
    assert 'probe["last_successful_checked_at"]' in CONTROLLER
    assert '"local_cycle_signature": recovery_cycle_signature(cycle)' in CONTROLLER


def test_broker_dependent_actions_are_gated_and_rechecked_on_click():
    assert "broker_refresh_current: bool" in GUI
    assert "can_resume = resume_supported and broker_refresh_current" in GUI
    assert "can_cancel_order = cancel_supported and broker_refresh_current" in GUI
    assert "can_market_close = market_close_supported and broker_refresh_current" in GUI
    assert "can_leave_orders = cancel_supported and broker_refresh_current" in GUI
    assert "def _recovery_refresh_is_current_or_warn(" in GUI
    assert 'self._recovery_refresh_is_current_or_warn("reconciling and resuming")' in GUI
    assert 'self._recovery_refresh_is_current_or_warn("cancelling app-owned orders")' in GUI
    assert 'self._recovery_refresh_is_current_or_warn("submitting a market SELL")' in GUI
    assert 'self._recovery_refresh_is_current_or_warn("leaving app-owned orders working")' in GUI
    assert "Manual override remains available without a current refresh" in GUI
