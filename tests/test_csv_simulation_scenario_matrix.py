"""Parameterized regression gate for every deterministic CSV price path."""

from __future__ import annotations

import pytest

from tests.simulation_scenario_catalog import (
    CSV_SCENARIOS,
    assert_catalog_integrity,
    assert_scenario,
    run_scenario,
)


def test_csv_scenario_catalog_covers_every_fixture() -> None:
    assert_catalog_integrity()


@pytest.mark.parametrize("scenario", CSV_SCENARIOS, ids=lambda scenario: scenario.name)
def test_csv_trading_scenario(scenario) -> None:
    assert_scenario(scenario, run_scenario(scenario))
