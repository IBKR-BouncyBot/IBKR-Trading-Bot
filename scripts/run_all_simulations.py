"""Run and verify every deterministic CSV strategy scenario in one process.

Unlike the historical runner, this gate checks each CSV against an explicit
expected stage, event sequence, quantity, fill, P/L direction, and applicable
boundary values. It also applies common safety invariants to every scenario.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.simulation_scenario_catalog import (  # noqa: E402
    CSV_SCENARIOS,
    assert_catalog_integrity,
    assert_scenario,
    category_counts,
    run_scenario,
)


def main() -> int:
    assert_catalog_integrity()
    for scenario in CSV_SCENARIOS:
        assert_scenario(scenario, run_scenario(scenario))

    unique_files = len({scenario.csv_name for scenario in CSV_SCENARIOS})
    category_summary = ", ".join(
        f"{category}={count}" for category, count in category_counts().items()
    )
    print(
        f"{len(CSV_SCENARIOS)} validated CSV simulation scenarios passed "
        f"across {unique_files} price-path files"
    )
    print(f"Scenario coverage: {category_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
