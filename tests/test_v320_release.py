"""v3.2.0 release metadata and documentation regressions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
ADAPTER = (ROOT / "app" / "ib_adapter.py").read_text(encoding="utf-8")
MODELS = (ROOT / "app" / "models.py").read_text(encoding="utf-8")
STORAGE = (ROOT / "app" / "storage.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
AUTOMATED_COVERAGE = (ROOT / "docs" / "AUTOMATED_TEST_COVERAGE.md").read_text(encoding="utf-8")
OFFLINE_TESTS = (ROOT / "docs" / "OFFLINE_BEHAVIOR_TESTS.md").read_text(encoding="utf-8")
TESTING_GUIDE = (ROOT / "docs" / "TESTING_AND_SIMULATION.md").read_text(encoding="utf-8")
CSV_MATRIX = (ROOT / "docs" / "CSV_SIMULATION_SCENARIO_MATRIX.md").read_text(encoding="utf-8")
LEGACY_INDEX = (ROOT / "docs" / "legacy" / "README.md").read_text(encoding="utf-8")
CURRENT_NOTE = ROOT / "docs" / "V3_2_0_EUR_SMART_AND_RECONNECT.md"
ARCHIVED_V312_NOTE = (
    ROOT / "docs" / "legacy" / "V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md"
)


def test_v320_release_metadata_is_consistent() -> None:
    assert "BouncyBot - IBKR Portable Trading Bot v3.2.0" in GUI
    assert "This is synthetic v3.2.0 paper-trading example data." in GUI
    assert "**Current release: v3.2.0**" in README
    assert 'version = "3.2.0"' in PYPROJECT
    assert '$version = "3.2.0"' in BUILD
    assert "## v3.2.0" in CHANGELOG
    assert "v3.2.0" in SECURITY
    assert "current v3.2.0 behavior" in DOCS_INDEX


def test_v320_release_notes_are_current_and_v312_is_archived() -> None:
    assert CURRENT_NOTE.is_file()
    assert ARCHIVED_V312_NOTE.is_file()
    assert not (
        ROOT / "docs" / "V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md"
    ).exists()
    assert "V3_2_0_EUR_SMART_AND_RECONNECT.md" in README
    assert "V3_2_0_EUR_SMART_AND_RECONNECT.md" in DOCS_INDEX
    assert "V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md" in LEGACY_INDEX


def test_v320_source_contains_exact_contract_currency_and_reconnect_boundaries() -> None:
    assert 'SUPPORTED_CONTRACT_CURRENCIES = frozenset({"USD", "EUR"})' in MODELS
    assert "requires_exact_contract_selection = True" in ADAPTER
    assert "Select an exact IBKR API contract result" in ADAPTER
    assert 'DATABASE_CONTRACT_CURRENCY_KEY = "database_contract_currency"' in STORAGE
    assert "RECONNECT_INTERVAL_SECONDS = 10.0" in CONTROLLER
    assert "retry every" in CONTROLLER
    assert "until connected or manually disconnected" in CONTROLLER
    assert "    utc_now_iso,\n)\n\n# Preserve the historical module-level datetime seam" in ADAPTER
    assert "    utc_now_iso,\n)\n# Preserve the historical module-level datetime seam" not in ADAPTER
    assert "    utc_now_iso,\n)\n\nDATABASE_CONTRACT_CURRENCY_KEY" in STORAGE
    assert "    utc_now_iso,\n)\n\n\nDATABASE_CONTRACT_CURRENCY_KEY" not in STORAGE


def test_v320_release_note_documents_scope_and_external_validation() -> None:
    text = CURRENT_NOTE.read_text(encoding="utf-8")
    assert "USD and EUR" in text
    assert "positive `conId`" in text
    assert "SMART" in text
    assert "one contract currency" in text
    assert "ten-second" in text
    assert "no maximum attempt count" in text
    assert "no FX conversion" in text
    assert "paper-account" in text


def test_v320_current_verification_documents_match_the_release() -> None:
    assert "verification scope for v3.2.0" in AUTOMATED_COVERAGE
    assert "v3.2.0 exact-contract, currency, and reconnect layer" in AUTOMATED_COVERAGE
    assert "current non-GUI, non-Windows, non-network test layer in v3.2.0" in OFFLINE_TESTS
    assert "v3.2.0 USD/EUR SMART and reconnect regressions" in TESTING_GUIDE
    assert "This v3.2.0 test-only matrix" in CSV_MATRIX
