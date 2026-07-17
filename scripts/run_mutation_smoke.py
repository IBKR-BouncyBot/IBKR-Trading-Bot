#!/usr/bin/env python3
"""Run a small deterministic mutation gate for safety-critical contracts.

This is intentionally a smoke suite rather than a full mutation campaign. Each
mutant changes one financial or state-machine condition in a temporary copy of
``app``. A focused independent probe must pass against the original copy and
fail against the mutant. Production sources are never edited in place.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mutation:
    name: str
    relative_path: str
    original: str
    replacement: str
    probe: str
    occurrence: int = 1


MUTATIONS = (
    Mutation(
        name="buy slippage increases sizing price",
        relative_path="app/models.py",
        original="return 1.0 + pct / 100.0",
        replacement="return 1.0 - pct / 100.0",
        probe="""
from app.models import effective_buy_sizing_price, slippage_factor
assert slippage_factor(True, 2.0) == 1.02
assert effective_buy_sizing_price(100.0, True, 2.0) == 102.0
""",
    ),
    Mutation(
        name="BUY native trail triggers at exact stop",
        relative_path="app/simulation.py",
        original="return price >= self.stop_price",
        replacement="return price > self.stop_price",
        probe="""
from app.simulation import NativeTrailSimulator
trail = NativeTrailSimulator("BUY", 101.0, 1.0)
assert trail.update(101.0) is True
""",
    ),
    Mutation(
        name="SELL native trail triggers at exact stop",
        relative_path="app/simulation.py",
        original="return price <= self.stop_price",
        replacement="return price < self.stop_price",
        probe="""
from app.simulation import NativeTrailSimulator
trail = NativeTrailSimulator("SELL", 99.0, 1.0)
assert trail.update(99.0) is True
""",
    ),
    Mutation(
        name="initial drop triggers at configured boundary",
        relative_path="app/strategy.py",
        original="if last_price <= next_cycle.drop_trigger_price:",
        replacement="if last_price < next_cycle.drop_trigger_price:",
        probe="""
from app.models import StrategySettings
from app.strategy import StrategyEngine
settings = StrategySettings(
    ticker="AAPL",
    initial_drop_pct=2.0,
    atr_adaptive_enabled=False,
    atr_block_new_buy_until_ready=False,
)
cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
updated, actions = StrategyEngine.on_price_update(cycle, 98.0, is_rth=True)
assert [action.action_type for action in actions] == ["PLACE_BUY_TRAIL"]
""",
    ),
    Mutation(
        name="SELL PnL uses overlapping bought and sold quantity",
        relative_path="app/strategy.py",
        original="qty = min(next_cycle.buy_filled_qty, next_cycle.sell_filled_qty)",
        replacement="qty = max(next_cycle.buy_filled_qty, next_cycle.sell_filled_qty)",
        occurrence=2,
        probe="""
from app.models import StrategySettings
from app.strategy import StrategyEngine
cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
cycle.buy_filled_qty = 10
cycle.avg_buy_price = 100.0
completed = StrategyEngine.on_sell_fill(cycle, 12, 101.0, "Filled")
assert completed.gross_pnl == 10.0
""",
    ),
    Mutation(
        name="app-owned unsold quantity honors either completed exit leg",
        relative_path="app/storage.py",
        original="remaining = max(0, bought - max(final_sold, protective_sold))",
        replacement="remaining = max(0, bought - min(final_sold, protective_sold))",
        probe="""
import tempfile
from pathlib import Path
from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
with tempfile.TemporaryDirectory() as folder:
    storage = BotStorage(Path(folder) / "state.sqlite")
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.sell_filled_qty = 10
    cycle.protective_sell_filled_qty = 0
    storage.upsert_cycle(cycle)
    assert storage.get_app_owned_unsold_position("AAPL")["quantity"] == 0
""",
    ),
)


def _replace_occurrence(text: str, original: str, replacement: str, occurrence: int) -> str:
    positions: list[int] = []
    offset = 0
    while True:
        index = text.find(original, offset)
        if index < 0:
            break
        positions.append(index)
        offset = index + len(original)
    if len(positions) < occurrence:
        raise ValueError(
            f"Expected occurrence {occurrence} of mutation target, found {len(positions)}."
        )
    index = positions[occurrence - 1]
    return text[:index] + replacement + text[index + len(original) :]


def _run_probe(root: Path, probe: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_mutation(root: Path, mutation: Mutation) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="ibkr_mutation_") as folder:
        work = Path(folder)
        shutil.copytree(root / "app", work / "app")

        baseline = _run_probe(work, mutation.probe)
        if baseline.returncode != 0:
            details = baseline.stderr.strip() or baseline.stdout.strip()
            return False, f"baseline probe failed: {details}"

        target = work / mutation.relative_path
        text = target.read_text(encoding="utf-8")
        try:
            mutated = _replace_occurrence(
                text,
                mutation.original,
                mutation.replacement,
                mutation.occurrence,
            )
        except ValueError as exc:
            return False, str(exc)
        target.write_text(mutated, encoding="utf-8")

        result = _run_probe(work, mutation.probe)
        if result.returncode == 0:
            return False, "mutant survived its contract probe"
        return True, "killed"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for mutation in MUTATIONS:
        passed, detail = run_mutation(root, mutation)
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {mutation.name}: {detail}")
        if not passed:
            failures.append(mutation.name)
    if failures:
        print(f"MUTATION SMOKE FAILED: {len(failures)} mutant(s) survived or could not run.")
        return 1
    print(f"MUTATION SMOKE PASSED: {len(MUTATIONS)}/{len(MUTATIONS)} safety mutants killed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
