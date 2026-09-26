"""Current release packaging and retained v4.1.0 GUI compatibility contract."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTE = 'V5_3_0_GUI_LAYOUT.md'


def test_metadata_agrees_with_gui_about_windows_and_package():
    gui = (ROOT / 'app/gui.py').read_text(encoding='utf-8')
    assert 'APP_VERSION = "5.3.0"' in gui
    assert 'BouncyBot - IBKR Portable Trading Bot v5.3.0' in gui
    assert 'version = "5.3.0"' in (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    assert '$version = "5.3.0"' in (ROOT / 'scripts/build_windows.ps1').read_text(encoding='utf-8')
    assert '**Current release: v5.3.0**' in (ROOT / 'README.md').read_text(encoding='utf-8')


def test_current_note_and_corrected_v400_history_are_retained():
    assert (ROOT / 'docs' / NOTE).is_file()
    assert len(list((ROOT / 'docs').glob('V[0-9]*.md'))) == 1
    old = 'V4_0_0_ATR_SESSION_MEMORY_AND_ORDER_EDITING.md'
    assert (ROOT / 'docs/legacy' / old).is_file()
    assert (ROOT / 'docs/legacy/V4_0_0_IMPLEMENTATION_TEST_REPORT.txt').is_file()
    assert (ROOT / 'docs/legacy/V4_1_0_GUI_METRICS_AND_AUDIT_LAYOUT.md').is_file()
    assert (ROOT / 'docs/legacy/V4_1_0_STATUS_ORDER_LINKED_CROSSHAIRS_IMPLEMENTATION_TEST_REPORT.txt').is_file()
    for path in ['README.md', 'docs/README.md']:
        text = (ROOT / path).read_text(encoding='utf-8')
        assert NOTE in text and old in text


def test_readonly_metrics_and_scope_are_documented():
    text = (ROOT / 'docs/legacy/V4_2_0_HEADER_AND_INDEPENDENT_TIMELINE.md').read_text(encoding='utf-8')
    for phrase in ['recorded BUY commission', 'remaining app-owned', 'not tax-lot',
                   'CSV export schemas are unchanged', 'three columns and four rows',
                   'no strategy or database migration', 'cursor synchronization is removed',
                   'last-five-minutes/session-close/orderly-close ATR saving policy is unchanged',
                   'Native Windows', 'real Qt/DPI']:
        assert phrase in text


def test_cost_helper_has_no_controller_storage_or_broker_dependency():
    tree = ast.parse((ROOT / 'app/gui.py').read_text(encoding='utf-8'))
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_cycle_purchase_cost')
    attributes = {node.attr for node in ast.walk(helper) if isinstance(node, ast.Attribute)}
    assert not attributes.intersection({'storage', 'adapter', 'controller', 'placeOrder', 'upsert_cycle'})
    assigned = {node.id for node in ast.walk(helper) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    assert 'cycle' not in assigned
    assert not any(isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) for node in ast.walk(helper))


def test_release_preserves_native_order_and_atr_safety_boundaries():
    controller = (ROOT / 'app/controller.py').read_text(encoding='utf-8')
    for token in ['STAGE3_SELL_CONFIRMATIONS_REQUIRED = 2', 'BUY_PARTIAL_FILL_GRACE_SECONDS = 3.0',
                  'SELL_MARKET_DATA_REVALIDATION_BLOCKED', 'PROTECTIVE_SELL_PARTIAL_TERMINAL',
                  'SELL_QUANTITY_MISMATCH']:
        assert token in controller
    source = (ROOT / 'app/atr_memory.py').read_text(encoding='utf-8')
    assert 'ATR_SEED_SAVE_WINDOW_SECONDS = 5 * 60.0' in source
    assert 'ATR_SEED_WRITE_INTERVAL_SECONDS = 60.0' in source
